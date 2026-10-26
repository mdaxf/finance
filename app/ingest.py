"""Quick-add, email ingestion and IMAP mailbox polling."""
from __future__ import annotations

import email
import email.header
import email.utils
import hashlib
import imaplib
import logging
import os
import re
from contextvars import ContextVar
from datetime import datetime, timedelta
from email.message import Message
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import ledger
from .db import Database, now_iso
from .email_rules import parse_email
from .parser import find_accounts, parse_quick_add
from .parser import normalize as parser_normalize

log = logging.getLogger("finance.ingest")


# ---------------------------------------------------------------- secrets
class SecretBox:
    """Fernet encryption with a key stored in data/secret.key (chmod 600)."""

    def __init__(self, key_path: str | os.PathLike):
        self.key_path = Path(key_path)
        if not self.key_path.exists():
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            self.key_path.write_bytes(Fernet.generate_key())
            from .config import private_file
            private_file(self.key_path)
        self.f = Fernet(self.key_path.read_bytes().strip())

    def encrypt(self, s: str) -> str:
        return self.f.encrypt(s.encode()).decode()

    def decrypt(self, s: str) -> str:
        try:
            return self.f.decrypt(s.encode()).decode()
        except (InvalidToken, ValueError):
            raise ValueError("Stored password cannot be decrypted (secret.key changed?). Re-enter the password.")


# ---------------------------------------------------------------- quick add
BALANCE_RX = re.compile(r"^\s*(?:调整余额|设置余额|余额(?!宝)|set\s+balance\b|balance\b|bal\b)[\s:：=]*", re.I)


class QuickAddError(ValueError):
    pass


REASON_WORDS = [
    ("unrecorded_spending", r"(?:unrecorded\s+spending|未记支出)"),
    ("unrecorded_income", r"(?:unrecorded\s+income|未记收入)"),
    ("investment_gain", r"(?<![A-Za-z])(?:gain|loss|returns?|投资收益|收益|亏损)(?![A-Za-z])"),
    ("interest", r"(?<![A-Za-z])(?:interest|利息)(?![A-Za-z])"),
    ("fees", r"(?<![A-Za-z])(?:fees?|费用|手续费)(?![A-Za-z])"),
    ("correction", r"(?<![A-Za-z])(?:correction|fix|校正|其他)(?![A-Za-z])"),
]


RECONCILE_RX = re.compile(r"^\s*(?:对账|reconcile\b|recon\b)[\s:：=]*", re.I)


def quick_reconcile(db: Database, text: str, ledger_id: int | None = None, dry_run: bool = False) -> dict:
    """'reconcile chase 1234.56' / '对账 支付宝 1000': mark the account reconciled at that balance (now, or a named date).
    Only when the app agrees with the statement; otherwise use the Reconcile dialog (or 'balance …') first."""
    from . import reconcile
    m = RECONCILE_RX.match(text)
    rest = text[m.end():]
    res = parse_quick_add(rest, ledger.parse_ctx(db))
    if not res.account_id or not any("account matched" in r for r in res.reasons):
        raise QuickAddError("Reconcile: name the account, e.g. 'reconcile chase 1234.56' or '对账 支付宝 1000'")
    if res.amount is None:
        raise QuickAddError("Reconcile: no statement balance found, e.g. 'reconcile chase 1234.56'")
    amount = -res.amount if re.search(r"(?<![\w.])-\s*[$¥￥]?\s*\d", rest) else res.amount
    from .parser import parse_date
    when = parse_date(rest, datetime.now())[0]
    try:
        p = reconcile.preview(db, res.account_id, when, amount)
    except reconcile.ReconcileError as e:
        raise QuickAddError(str(e))
    out = {"parsed": {**res.to_dict(), "kind": "reconcile", "amount": amount}, "confidence": 1.0, "action": "reconcile",
           "difference": p["difference_shown"]}
    if abs(p["difference"]) >= 0.005:
        raise QuickAddError(f"Reconcile: {p['account_name']} is {p['computed']:,.2f} in the app vs {p['statement']:,.2f} on the "
                            f"statement (difference {p['difference_shown']:+,.2f}). Open Accounts → Reconcile to record the "
                            f"difference, or use 'balance …' first.")
    if dry_run:
        out.update(status="preview", detail=f"Would mark {p['account_name']} reconciled at {p['statement']:,.2f} {p['currency']} (matches)")
        out["message"] = short_message(db, out, text, dry_run=True)
        return out
    r = reconcile.create(db, res.account_id, amount, when)
    out.update(status="posted", detail=r["message"], reconciliation=r["reconciliation"])
    out["message"] = short_message(db, out, text)
    return out


def quick_set_balance(db: Database, text: str, ledger_id: int | None = None, dry_run: bool = False) -> dict:
    """'余额 支付宝 1234.5' / 'balance checking 5230' / 'balance amex 820 yesterday' (liabilities: amount owed)."""
    m = BALANCE_RX.match(text)
    rest = text[m.end():]
    reason = None  # optional reason word: 'balance brokerage 52000 gain' / '余额 余额宝 1000 利息'
    for code, rx in REASON_WORDS:
        mm = re.search(rx, rest, re.I)
        if mm:
            reason = code
            rest = (rest[:mm.start()] + " " + rest[mm.end():]).strip()
            break
    res = parse_quick_add(rest, ledger.parse_ctx(db))
    explicit = any("account matched" in r for r in res.reasons)
    if not res.account_id or not explicit:
        raise QuickAddError("Set balance: name the account, e.g. '余额 支付宝 1234.5' or 'balance checking 5230'")
    if res.amount is None:
        raise QuickAddError("Set balance: no amount found, e.g. 'balance checking 5230'")
    amount = -res.amount if re.search(r"(?<![\w.])-\s*[$¥￥]?\s*\d", rest) else res.amount
    from .parser import parse_date
    when = parse_date(rest, datetime.now())[0]  # None unless the text names a date ('yesterday', '10/3', '2026-10-01')
    acct = db.one("SELECT name, currency FROM accounts WHERE id=?", (res.account_id,))
    a_type = (db.one("SELECT type FROM accounts WHERE id=?", (res.account_id,)) or {}).get("type")
    reason = reason or ledger.default_reason(a_type)
    out = {"parsed": {**res.to_dict(), "kind": "adjustment", "amount": amount, "adj_reason": reason},
           "confidence": 1.0, "action": "set_balance", "reason": reason, "reason_label": ledger.reason_label(reason)}
    if dry_run:
        before = ledger.account_balance_at(db, res.account_id, when)
        out.update(status="preview", detail=f"Would set the balance of {acct['name']} to {amount:,.2f} {acct['currency']}"
                                            f" · reason: {ledger.reason_label(reason)}")
        out["previous_signed"] = before
        out["message"] = short_message(db, out, text, dry_run=True)
        return out
    r = ledger.set_balance(db, res.account_id, amount, when, None, ledger_id=ledger_id, reason=reason)
    out.update(status="posted" if r["created"] else "unchanged", balance_set={k: v for k, v in r.items() if k != "transaction"},
               transaction=r["transaction"], detail=r["message"])
    out["message"] = short_message(db, out, text)
    return out


# ---------------------------------------------------------------- short, Siri-friendly result messages
CJK_RX = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
CUR_SYMBOL = {"USD": "$", "CNY": "¥", "RMB": "¥", "EUR": "€", "GBP": "£", "JPY": "JP¥", "HKD": "HK$", "TWD": "NT$",
              "CAD": "CA$", "AUD": "A$", "SGD": "S$", "KRW": "₩", "INR": "₹"}


# Language of reply messages for the current request: set per request by main.py from the ?lang= param, else
# Accept-Language, else English. Plain function calls (email ingest, scheduler) get English.
MSG_LANG: ContextVar[str] = ContextVar("msg_lang", default="en")
MSG_LANGS = ("en", "zh")


def resolve_lang(param: str | None = None, accept: str | None = None) -> str:
    """'zh' or 'en'. param ('zh', 'zh-CN', 'zh-TW', 'cn', 'en', ...) wins unless empty/'auto'; then the best
    supported Accept-Language entry by q-value; otherwise English."""
    def norm(code: str) -> str | None:
        c = (code or "").strip().lower().replace("_", "-")
        if c in ("cn", "chinese") or c.startswith("zh"):
            return "zh"
        if c == "en" or c.startswith("en-"):
            return "en"
        return None
    if param and param.strip().lower() not in ("", "auto"):
        return norm(param) or "en"
    best, bq = None, -1.0
    for i, part in enumerate((accept or "").split(",")):
        bits = part.strip().split(";")
        q = 1.0
        for b in bits[1:]:
            if b.strip().startswith("q="):
                try:
                    q = float(b.strip()[2:])
                except ValueError:
                    q = 0.0
        code = norm(bits[0])
        if code and q > 0 and q > bq:
            best, bq = code, q
    return best or "en"


def msg_zh() -> bool:
    return MSG_LANG.get() == "zh"


def is_chinese(text: str | None) -> bool:
    return bool(CJK_RX.search(text or ""))


def fmt_money(v, cur: str | None) -> str:
    if v is None:
        return ""
    cur = (cur or "").upper()
    neg = v < 0
    body = f"{abs(float(v)):,.2f}"
    if body.endswith(".00"):  # whole amounts read better aloud: "$300", "¥35"
        body = body[:-3]
    sym = CUR_SYMBOL.get(cur)
    s = f"{sym}{body}" if sym else f"{body} {cur}".strip()
    return ("-" if neg else "") + s


def _acct_name(db: Database, aid) -> str | None:
    if not aid:
        return None
    a = db.one("SELECT name FROM accounts WHERE id=?", (aid,))
    return a["name"] if a else None


def _what(db: Database, text: str, p: dict, zh: bool) -> str:
    """A short description for the message: merchant, else the words of the input minus amount/account words,
    else the category name."""
    if p.get("merchant"):
        return str(p["merchant"])[:30]
    t = parser_normalize(text or "")
    t = re.sub(r"(?i)(?:[$¥￥€£]\s*)?[-+]?\d[\d,]*(?:\.\d+)?\s*(?:usd|cny|rmb|eur|gbp|jpy|hkd|元|块|刀|美元|美金|人民币|dollars?|bucks?)?", " ", t)
    for aid in (p.get("account_id"), p.get("to_account_id")):
        if not aid:
            continue
        a = db.one("SELECT name, aliases FROM accounts WHERE id=?", (aid,)) or {}
        names = [a.get("name") or ""] + [x.strip() for x in (a.get("aliases") or "").split(",")]
        for n in sorted({n for n in names if n and len(n) >= 2}, key=len, reverse=True):
            t = re.sub(re.escape(n), " ", t, flags=re.I)
    t = re.sub(r"(?i)(?<![a-z])(?:paid|pay|with|via|using|on|at|from|to|for|by|today|yesterday|用|从|到|付|花了|花)(?![a-z])", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.;:-·")
    if t:
        return t[:30]
    if p.get("category_id"):
        c = db.one("SELECT name, name_zh FROM categories WHERE id=?", (p["category_id"],)) or {}
        return (c.get("name_zh") if zh and c.get("name_zh") else c.get("name")) or ""
    return ""


def _review_reason(out: dict, p: dict, zh: bool, force_review: bool) -> str:
    if p.get("amount") in (None, 0):
        return "没找到金额" if zh else "amount not found"
    if not p.get("account_id"):
        return "没找到账户" if zh else "account not found"
    if p.get("kind") == "transfer" and not p.get("to_account_id"):
        return "没找到转入账户" if zh else "destination account not found"
    if out.get("warning"):
        return str(out["warning"])[:80]
    if force_review:
        return "已放入待确认" if zh else "sent to review"
    pct = round((out.get("confidence") or 0) * 100)
    return f"把握度 {pct}%" if zh else f"low confidence {pct}%"


def short_message(db: Database, out: dict, text: str, dry_run: bool = False, force_review: bool = False) -> str:
    """One line for Siri / Shortcuts / Apple Watch, in the request's language (MSG_LANG; default English)."""
    zh = msg_zh()
    sep = ": "
    action = out.get("action")
    p = out.get("parsed") or {}
    if action in ("set_balance", "reconcile"):
        aid = p.get("account_id")
        a = db.one("SELECT name, currency, type FROM accounts WHERE id=?", (aid,)) or {}
        liab = ledger.is_liability(a.get("type") or "")
        amt = fmt_money(p.get("amount"), a.get("currency"))
        owed = ("欠款 " if zh else "owed ") if liab else ""
        name = a.get("name") or "?"
        if action == "reconcile":
            if dry_run:
                return f"预览{sep}对账 {name} {owed}{amt} (一致)" if zh else f"Preview{sep}reconcile {name} {owed}{amt} (matches)"
            return f"已对账{sep}{name} {owed}{amt}" if zh else f"Reconciled{sep}{name} {owed}{amt}"
        if dry_run:
            return f"预览{sep}余额 {name} → {owed}{amt}" if zh else f"Preview{sep}balance {name} → {owed}{amt}"
        if out.get("status") == "unchanged":
            return f"余额无变化{sep}{name} {owed}{amt}" if zh else f"Balance unchanged{sep}{name} {owed}{amt}"
        return f"余额已设为{sep}{name} {owed}{amt}" if zh else f"Balance set{sep}{name} {owed}{amt}"

    t = out.get("transaction") or {}
    if t:  # what was actually saved
        p = {**p, "kind": t.get("kind"), "account_id": t.get("account_id"), "to_account_id": t.get("to_account_id"),
             "category_id": t.get("category_id") or p.get("category_id")}
        amount = t.get("orig_amount") if t.get("orig_currency") else t.get("amount")
        cur = t.get("orig_currency") or t.get("currency")
    else:
        amount = p.get("amount")
        cur = p.get("currency") or (db.one("SELECT currency FROM accounts WHERE id=?", (p.get("account_id"),)) or {}).get("currency") \
            if p.get("account_id") else (p.get("currency") or db.get_setting("base_currency", "USD"))
    amt = fmt_money(amount, cur) if amount else ""
    kind = p.get("kind") or "expense"
    if amt and kind in ("income", "refund", "dividend", "sell"):
        amt = "+" + amt
    acct, to = _acct_name(db, p.get("account_id")), _acct_name(db, p.get("to_account_id"))
    status = out.get("status")
    if status == "review":
        what = _what(db, text, p, zh) or (text or "").strip()[:30]
        body = " ".join(x for x in (what, amt) if x)
        reason = _review_reason(out, p, zh, force_review)
        head = ("预览（需确认）" if zh else "Preview (needs review)") if dry_run else ("待确认" if zh else "Needs review")
        return f"{head}{sep}{body} ({reason})"
    if kind == "transfer":
        core = f"{amt} {acct or '?'} → {to or '?'}"
        if dry_run:
            return f"预览{sep}转账 {core}" if zh else f"Preview{sep}transfer {core}"
        return f"已转账{sep}{core}" if zh else f"Transferred {core}"
    what = _what(db, text, p, zh)
    if what and not zh:
        what = what[:1].upper() + what[1:]
    core = " ".join(x for x in (what, amt) if x) + (f" · {acct}" if acct else "")
    if dry_run:
        return f"预览{sep}{core}" if zh else f"Preview{sep}{core}"
    return f"已记{sep}{core}" if zh else f"Saved{sep}{core}"


def error_message(detail: str, text: str | None) -> str:
    zh = msg_zh()
    detail = re.split(r"(?<=[.!?。！？])\s", (detail or "").strip(), maxsplit=1)[0] or "unknown error"
    return f"没记上: {detail}" if zh else f"Couldn't save: {detail}"


def prefill_from_parse(db: Database, res, text: str) -> dict:
    """Form pre-fill for 'Open in form': every field the parser found, plus the fields it was unsure about."""
    acct = db.one("SELECT currency FROM accounts WHERE id=?", (res.account_id,)) if res.account_id else None
    reasons = " | ".join(res.reasons)
    unsure = []
    if res.amount is None or "several numbers" in reasons:
        unsure.append("amount")
    if not res.account_id or "used default account" in reasons or "several accounts" in reasons:
        unsure.append("account_id")
    if res.kind == "transfer" and not res.to_account_id:
        unsure.append("to_account_id")
    if res.kind in ("expense", "income", "refund") and not res.category_id:
        unsure.append("category_id")
    if res.kind not in ("expense",) and res.confidence < 0.75:
        unsure.append("kind")
    prefill = {"kind": res.kind, "amount": res.amount,
               "currency": res.currency or (acct["currency"] if acct else None) or db.get_setting("base_currency", "USD"),
               "account_id": res.account_id, "to_account_id": res.to_account_id, "category_id": res.category_id,
               "merchant": res.merchant, "member_id": res.member_id, "project_id": res.project_id, "plan_line_id": res.plan_line_id, "tax_amount": res.tax_amount, "tip_amount": res.tip_amount, "tax_rate": res.tax_rate,
               "tags": ",".join(res.tags), "date": res.date, "note": (text or "").strip(), "raw_text": text}
    return {"prefill": prefill, "uncertain": unsure}


# reimbursable 可报销 flag in quick-add: a leading "报销"/"可报销"/"reimb"/"reimbursable" word, or a tag
# #报销 / #可报销 / #reimb / #reimbursable, optionally with the payer: "#报销:Acme" / "#reimb:Acme"
REIMB_LEAD_RX = re.compile(r"^\s*(?:可?报销|reimb(?:ursable)?)(?:[:：]|\s+|(?=[\u3400-\u9fff]))", re.I)
REIMB_TAG_RX = re.compile(r"(?<!\S)#(?:可?报销|reimb(?:ursable)?)(?:[:：]([^\s#]+))?(?=\s|$)", re.I)


def split_reimb(text: str) -> tuple[str, bool, str | None]:
    """(text without the flag, reimbursable?, payer)."""
    t, payer, flag = text or "", None, False
    m = REIMB_TAG_RX.search(t)
    if m:
        flag, payer = True, (m.group(1) or None)
        t = (t[:m.start()] + t[m.end():]).strip()
    m = REIMB_LEAD_RX.match(t)
    if m and t[m.end():].strip():
        flag, t = True, t[m.end():].strip()
    return t, flag, payer


def _reimb_msg(msg: str) -> str:
    for a, b in (("Saved: ", "Saved (reimbursable): "), ("已记: ", "已记(可报销): "), ("Preview: ", "Preview (reimbursable): "),
                 ("预览: ", "预览(可报销): "), ("Needs review: ", "Needs review (reimbursable): "), ("待确认: ", "待确认(可报销): ")):
        if msg.startswith(a):
            return b + msg[len(a):]
    return msg


def quick_add(db: Database, text: str, source: str = "api", ledger_id: int | None = None,
              dry_run: bool = False, force_review: bool = False, threshold: float | None = None,
              external_id: str | None = None, allow_balance: bool = True) -> dict:
    if allow_balance and RECONCILE_RX.match(text or ""):
        return quick_reconcile(db, text, ledger_id, dry_run)
    if allow_balance and BALANCE_RX.match(text or ""):
        return quick_set_balance(db, text, ledger_id, dry_run)
    ptext, reimb, payer = split_reimb(text)
    res = parse_quick_add(ptext if reimb else text, ledger.parse_ctx(db))
    reimb = reimb and res.kind == "expense"
    th = float(threshold if threshold is not None else db.get_setting("review_threshold", "0.75"))
    status = "posted" if (res.confidence >= th and not force_review and res.amount and res.account_id) else "review"
    out = {"parsed": res.to_dict(), "confidence": res.confidence, "status": status, "threshold": th}
    if reimb:
        out["parsed"].update(reimbursable=True, reimb_payer=payer)
        out["reimbursable"] = True
    if dry_run:
        out.update(prefill_from_parse(db, res, ptext if reimb else text))
        if reimb:
            out["prefill"].update(reimbursable=True, reimb_payer=payer)
        msg = short_message(db, out, ptext if reimb else text, dry_run=True, force_review=force_review)
        out["message"] = _project_note(_reimb_msg(msg) if reimb else msg, res)
        return out
    data = {"kind": res.kind, "date": res.date, "amount": res.amount or 0, "currency": res.currency,
            "account_id": res.account_id, "to_account_id": res.to_account_id, "category_id": res.category_id,
            "merchant": res.merchant, "member_id": res.member_id, "project_id": res.project_id, "plan_line_id": res.plan_line_id, "tax_amount": res.tax_amount, "tip_amount": res.tip_amount, "tax_rate": res.tax_rate, "tags": res.tags,
            "note": text.strip(), "source": source, "status": status, "confidence": res.confidence,
            "raw_text": text, "ledger_id": ledger_id or int(db.get_setting("default_ledger_id", "1") or 1),
            "external_id": external_id}
    if reimb:
        data.update(reimbursable=1, reimb_payer=payer)
    try:
        out["transaction"] = ledger.create_tx(db, data)
    except ledger.ValidationError as e:
        data["status"] = "review"
        out["status"] = "review"
        out["warning"] = str(e)
        out["transaction"] = ledger.create_tx(db, data)
    msg = short_message(db, out, ptext if reimb else text, force_review=force_review)
    out["message"] = _project_note(_reimb_msg(msg) if reimb else msg, res)
    return out


def _project_note(msg: str, res) -> str:
    if getattr(res, "plan_line_unknown", None):
        if msg_zh():
            msg += f" · 未找到预算明细“{res.plan_line_unknown}”"
        else:
            msg += f" · plan line “{res.plan_line_unknown}” not found"
    if getattr(res, "project_unknown", None):
        if msg_zh():
            return msg + f" · 未找到项目“{res.project_unknown}”（或已归档）"
        return msg + f" · project “{res.project_unknown}” not found (or archived)"
    return msg


# ---------------------------------------------------------------- email ingest
def _dedupe_id(msg: dict) -> str:
    mid = (msg.get("message_id") or "").strip()
    if mid:
        return mid
    h = hashlib.sha1(f"{msg.get('from')}|{msg.get('subject')}|{msg.get('received_at')}|{(msg.get('body') or '')[:500]}".encode())
    return "sha1:" + h.hexdigest()


def _log(db: Database, **kw):
    kw.setdefault("time", now_iso())
    with db.tx() as con:
        cols = list(kw)
        con.execute(f"INSERT INTO ingest_log({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [kw[c] for c in cols])
        # keep the log bounded
        con.execute("DELETE FROM ingest_log WHERE id NOT IN (SELECT id FROM ingest_log ORDER BY id DESC LIMIT 2000)")


def _ingest_bill(db: Database, ex: dict, sender: str, subject: str, body: str, msg: dict, mid: str, logbase: dict) -> dict | None:
    """Bill / payment-confirmation emails (see app/bills.py). None = not handled here, continue the normal flow."""
    from . import bills
    rule_kind = ex.get("kind") if ex.get("kind") in ("bill", "bill_paid") else None
    if not rule_kind and ex.get("kind") == "hold":
        return None
    from .email_actions import KINDS as ACTION_KINDS
    if ex.get("kind") in ACTION_KINDS:  # a rule chose another action (schedule / loan / credit / points)
        return None
    try:
        res = bills.handle_email(db, sender, subject, body, _email_date(msg), mid, rule_kind, rule_overrides(ex))
    except Exception as e:  # noqa: BLE001 - a bill-parsing problem must never lose the email
        log.warning("bill email handling failed: %s", e)
        return None
    if not res:
        return None
    p = {k: v for k, v in res["parsed"].items() if k not in ("text",)}
    if res["type"] == "bill":
        if res["action"] == "skipped":
            if rule_kind:
                _log(db, **logbase, result="skipped", detail="bill: " + res["reason"], rule_name=ex.get("rule_name"))
                return {"created": False, "result": "skipped", "reason": "bill: " + res["reason"], "message_id": mid}
            return None
        b = res["bill"]
        due = f", due {b['due_date']}" if b.get("due_date") else ", no due date found"
        amt = f" {fmt_money(b['amount'], b['currency'])}" if b.get("amount") is not None else " (amount not found)"
        what = "bill task created" if res["action"] == "created" else "bill already scheduled"
        _log(db, **logbase, result="bill", detail=f"{what}: {b['payee']}{amt}{due}", rule_name=ex.get("rule_name"),
             ref_kind="bill", ref_id=b["id"])
        return {"created": res["action"] == "created", "result": "bill", "action": res["action"], "bill": b, "extracted": p}
    # payment confirmation
    if res["action"] == "matched":
        b = res["bill"]
        _log(db, **logbase, result="bill_paid", detail=f"bill #{b['id']} {b['payee']} marked paid", rule_name=ex.get("rule_name"),
             ref_kind="bill", ref_id=b["id"])
        return {"created": False, "result": "bill_paid", "bill": b, "extracted": p}
    why = f"payment {res['action']} ({res.get('reason') or ''}) → Bills → Payments to review"
    if rule_kind:
        _log(db, **logbase, result="review", detail=why, rule_name=ex.get("rule_name"))
        return {"created": False, "result": "bill_review", "action": res["action"], "payment_id": res["payment_id"],
                "candidates": res["candidates"], "extracted": p}
    ex["bill_note"] = why
    return None


def rule_overrides(ex: dict) -> dict:
    """The parsing rule's fixed bill fields (kind bill / bill_paid): payee, date, account; the rule's own amount."""
    if "rule_id" not in ex:
        return {}
    return {"payee": ex.get("payee"), "date_text": ex.get("date_text"), "account_id": ex.get("rule_account_id"),
            "amount": ex.get("rule_amount"), "currency": ex.get("rule_currency")}


def _ingest_action(db: Database, ex: dict, sender: str, subject: str, msg: dict, mid: str, logbase: dict,
                   mailbox: dict | None) -> dict | None:
    """Rule kinds schedule_post / schedule_new / loan_payment / credit / reward_balance (see app/email_actions.py).
    None = continue the normal flow (with the reason noted when the rule's action could not be done)."""
    from . import email_actions
    if ex.get("kind") not in email_actions.KINDS:
        return None
    try:
        res = email_actions.handle(db, ex, sender, subject, _email_date(msg), mid, mailbox)
    except Exception as e:  # noqa: BLE001 - an action problem must never lose the email
        log.warning("email rule action failed: %s", e)
        res = {"action": "skipped", "stop": False, "detail": f"{ex.get('kind')}: {type(e).__name__}: {e}"}
    if not res:
        return None
    if not res.get("stop"):
        ex["action_note"] = res.get("detail")
        return None
    _log(db, **logbase, result=res["result"], detail=res["detail"][:500], rule_name=ex.get("rule_name"),
         transaction_id=res.get("transaction_id"), ref_kind=res.get("ref_kind"), ref_id=res.get("ref_id"))
    ex.pop("text", None)
    out = {"created": res["action"] in ("created", "posted", "recorded", "updated"), "result": res["result"],
           "action": res["action"], "detail": res["detail"], "ref_kind": res.get("ref_kind"), "ref_id": res.get("ref_id"),
           "extracted": ex}
    if res.get("transaction_id"):
        out["transaction"] = ledger.get_tx(db, res["transaction_id"])
    return out


def _email_date(msg: dict) -> str | None:
    if not msg.get("received_at"):
        return None
    try:
        return ledger._norm_date(msg["received_at"])
    except ledger.ValidationError:
        try:
            return email.utils.parsedate_to_datetime(msg["received_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return None


def _account_by_last4(db: Database, last4) -> dict | None:
    if not last4:
        return None
    l4 = str(last4)
    for a in db.all("SELECT * FROM accounts WHERE archived=0 ORDER BY id"):
        if a.get("last4") and (l4.endswith(str(a["last4"])) or str(a["last4"]).endswith(l4)):
            return a
    return None


def balance_plan(db: Database, ex: dict, date_s: str | None = None) -> dict:
    """What a 'balance' rule would do (no writes): the account (by last 4 digits), its balance on the email date,
    the statement value, the difference and the adjustment reason. Respects reconciliation checkpoints: an email
    dated on/before the account's latest reconciliation goes to review instead of being posted."""
    plan = {"kind": "balance", "statement_balance": ex.get("amount"), "last4": ex.get("last4"), "date": date_s}
    if ex.get("amount") is None:
        return plan | {"ok": False, "problem": "No statement balance found (amount regex)"}
    acct = _account_by_last4(db, ex.get("last4"))
    if not acct:
        return plan | {"ok": False, "problem": f"No account with last digits {ex.get('last4')}" if ex.get("last4")
                       else "No account number found (last-4 regex)"}
    reason = ex.get("adjust_reason") or ledger.default_reason(acct["type"])
    try:
        r = ledger.set_balance(db, acct["id"], ex["amount"], date_s, reason=reason, dry_run=True)
    except ledger.ValidationError as e:
        return plan | {"ok": False, "problem": str(e), "account_id": acct["id"], "account_name": acct["name"]}
    from . import reconcile
    cp = reconcile.latest(db, acct["id"])
    locked = bool(cp and r["date"] <= str(cp["as_of"])[:16])
    return plan | {"ok": True, "account_id": acct["id"], "account_name": acct["name"], "currency": acct["currency"],
                   "date": r["date"], "previous": r["previous"], "difference": r["difference"], "reason": reason,
                   "reason_label": r["reason_label"], "message": r["message"],
                   "reconciled_at": str(cp["as_of"]) if cp else None, "before_reconciliation": locked}


def _ingest_balance(db: Database, ex: dict, msg: dict, mid: str, logbase: dict, mailbox: dict | None,
                    auto_post: bool | None, source: str) -> dict:
    plan = balance_plan(db, ex, _email_date(msg))
    ex.pop("text", None)
    if not plan["ok"]:
        _log(db, **logbase, result="skipped", detail="Balance: " + plan["problem"], rule_name=ex.get("rule_name"))
        return {"created": False, "result": "skipped", "reason": plan["problem"], "extracted": ex, "balance": plan, "message_id": mid}
    if abs(plan["difference"]) < 0.005:
        _log(db, **logbase, result="unchanged", detail=f"Balance of {plan['account_name']} already {plan['statement_balance']:,.2f}; nothing recorded",
             rule_name=ex.get("rule_name"), confidence=1.0)
        return {"created": False, "result": "unchanged", "extracted": ex, "balance": plan, "message_id": mid}
    conf, reasons = 0.8, [f"statement balance {plan['statement_balance']}", f"rule {ex.get('rule_name')}",
                         f"account by last digits {plan['last4']}", f"difference {plan['difference']:+,.2f}"]
    if not plan["date"] or not _email_date(msg):
        conf -= 0.2; reasons.append("no email date: dated now")
    if plan["before_reconciliation"]:
        conf = min(conf, 0.5); reasons.append(f"on/before the reconciliation of {plan['reconciled_at'][:10]}: review it")
    th = float(db.get_setting("review_threshold", "0.75"))
    if auto_post is None:
        auto_post = bool((mailbox or {}).get("auto_post"))
    status = "posted" if (auto_post and conf >= th) else "review"
    subject = (msg.get("subject") or "").strip()[:150]
    r = ledger.set_balance(db, plan["account_id"], plan["statement_balance"], plan["date"],
                           note=f"Statement 对账单: {subject}" if subject else "Statement 对账单", source=source,
                           reason=plan["reason"], status=status,
                           extra={"confidence": round(conf, 2), "external_id": mid, "raw_text": (msg.get("body") or "")[:4000]})
    tx = r["transaction"]
    _log(db, **logbase, result=status, detail="; ".join(reasons), rule_name=ex.get("rule_name"), confidence=round(conf, 2),
         transaction_id=tx["id"] if tx else None)
    return {"created": bool(tx), "result": status, "confidence": round(conf, 2), "reasons": reasons, "extracted": ex,
            "balance": plan, "transaction": tx}


def ingest_email(db: Database, msg: dict, mailbox: dict | None = None, auto_post: bool | None = None,
                 source: str = "email", bills_only: bool = False) -> dict:
    """msg: {from, subject, body, received_at, message_id?}. Returns result dict.
    bills_only: the email came in outside the mailbox's filter only because it looks like a bill (watch_rules): it
    becomes a bill / payment match or nothing - never a transaction."""
    sender, subject, body = msg.get("from") or "", msg.get("subject") or "", msg.get("body") or ""
    mid = _dedupe_id(msg)
    mbox_id = mailbox["id"] if mailbox else None
    logbase = dict(mailbox_id=mbox_id, source="imap" if mailbox else "api", sender=sender[:200], subject=subject[:300], message_id=mid[:300])
    if db.one("SELECT 1 x FROM email_seen WHERE message_id=?", (mid,)):
        _log(db, **logbase, result="duplicate", detail="Message already processed")
        return {"created": False, "result": "duplicate", "message_id": mid}
    with db.tx() as con:
        con.execute("INSERT OR IGNORE INTO email_seen(message_id, mailbox_id) VALUES(?,?)", (mid, mbox_id))

    rules = db.all("SELECT * FROM email_rules WHERE enabled=1")
    ex = parse_email(sender, subject, body, rules)
    if ex.get("kind") == "balance":  # statement balance -> set-balance adjustment
        return _ingest_balance(db, ex, msg, mid, logbase, mailbox, auto_post, source)
    # bills 账单待付: a bill / payment request -> a payment task + reminder (no transaction); a payment confirmation ->
    # the matching open bill is marked paid. Unmatched / ambiguous confirmations are kept for review and then go on
    # through the normal flow below (e.g. a card payment still lands in To review as a transfer).
    bill_res = _ingest_bill(db, ex, sender, subject, body, msg, mid, logbase)
    if bill_res is not None:
        return bill_res
    if bills_only:
        _log(db, **logbase, result="skipped", detail="outside the mailbox filter and not a bill")
        return {"created": False, "result": "skipped", "reason": "not a bill", "message_id": mid}
    act_res = _ingest_action(db, ex, sender, subject, msg, mid, logbase, mailbox)
    if act_res is not None:
        return act_res
    from .email_actions import KINDS as ACTION_KINDS
    if ex.get("kind") in ACTION_KINDS:  # the rule's action could not be done: a transaction to review instead
        from .email_rules import detect_kind
        ex["kind"] = "income" if detect_kind(ex.get("text") or "") == "income" else "expense"
        auto_post = False
    if ex.get("kind") in ("bill", "bill_paid"):
        ex["kind"] = "payment" if ex["kind"] == "bill_paid" else "expense"
    text = ex["text"]
    if ex.get("amount") is None:
        # maybe a plain "lunch 25 alipay" note mailed to the inbox
        first = (subject + " " + body.strip().split("\n")[0]) if body.strip() else subject
        qa = quick_add(db, first[:300], dry_run=True, allow_balance=False)
        if qa["parsed"]["amount"]:
            res = quick_add(db, first[:300], allow_balance=False, source=source, force_review=not auto_post, external_id=mid)
            tid = res["transaction"]["id"]
            _log(db, **logbase, result=res["status"], detail="parsed as quick-add text", confidence=res["confidence"], transaction_id=tid)
            return {"created": True, "result": res["status"], "transaction": res["transaction"], "extracted": ex | {"text": None}}
        _log(db, **logbase, result="skipped", detail="No amount found", rule_name=ex.get("rule_name"))
        return {"created": False, "result": "skipped", "reason": "No amount found", "message_id": mid}

    conf = 0.4
    reasons = [f"amount {ex['amount']}"]
    if ex.get("rule_name"):
        conf += 0.1; reasons.append(f"rule {ex['rule_name']}")
    if ex.get("bill_note"):
        reasons.append(ex["bill_note"])
    if ex.get("action_note"):
        reasons.append(ex["action_note"] + " → a transaction to review instead")
    accounts = db.all("SELECT * FROM accounts WHERE archived=0")
    account_id = None
    if ex.get("last4"):
        l4 = str(ex["last4"])
        for a in accounts:
            if a.get("last4") and (l4.endswith(str(a["last4"])) or str(a["last4"]).endswith(l4)):
                account_id = a["id"]; conf += 0.3; reasons.append(f"account by last digits {l4}"); break
    if not account_id:
        hits = find_accounts(text[:1000], accounts)
        if len({h["account_id"] for h in hits}) == 1:
            account_id = hits[0]["account_id"]; conf += 0.2; reasons.append("account by name")
    if not account_id:
        dflt = (mailbox or {}).get("default_account_id") or db.get_setting("default_account_id")
        if dflt:
            account_id = int(dflt); conf += 0.1; reasons.append("default account")
    kind = ex.get("kind") or "expense"
    to_account_id = None
    if kind == "hold":  # card authorization hold 预授权 -> a hold Reserve to review (never a transaction)
        date_h = None
        if msg.get("received_at"):
            try:
                date_h = ledger._norm_date(msg["received_at"])
            except ledger.ValidationError:
                date_h = None
        if account_id:
            from . import reserves
            try:
                r = reserves.create_from_email(db, ex, account_id, date_h, mid, subject)
                reasons.append("card hold → Reserves (review)")
                _log(db, **logbase, result="review", detail="; ".join(reasons), rule_name=ex.get("rule_name"), confidence=round(min(conf, 1.0), 2))
                ex.pop("text", None)
                return {"created": True, "result": "review", "kind": "hold", "reasons": reasons, "extracted": ex, "reserve": r}
            except ledger.ValidationError as e:
                reasons.append(str(e))
        kind = "expense"  # no card found: an expense to review (it can be converted to a reserve there)
        conf = min(conf, 0.5)
        reasons.append("card hold: choose the card, then convert it to a reserve")
    if kind == "payment":  # credit card payment received -> transfer needs a source; leave for review
        kind = "transfer"
        to_account_id, account_id = account_id, None
        conf = min(conf, 0.5)
        reasons.append("card payment: choose the paying account")
    cats = db.all("SELECT * FROM categories")
    cat_id = None
    if kind in ("expense", "refund", "income"):
        from .parser import categorize
        cat_id, ckind, kw = categorize(f"{ex.get('merchant') or ''} {subject}", cats,
                                       kind="income" if kind == "income" else "expense")
        if cat_id:
            conf += 0.2; reasons.append(f"category by '{kw}'")
    conf = round(min(conf, 1.0), 2)
    th = float(db.get_setting("review_threshold", "0.75"))
    if auto_post is None:
        auto_post = bool((mailbox or {}).get("auto_post"))
    status = "posted" if (auto_post and conf >= th and account_id and kind != "transfer") else "review"
    date_s = None
    if msg.get("received_at"):
        try:
            date_s = ledger._norm_date(msg["received_at"])
        except ledger.ValidationError:
            try:
                date_s = email.utils.parsedate_to_datetime(msg["received_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_s = None
    data = {"kind": kind, "date": date_s, "amount": ex["amount"], "currency": ex.get("currency"),
            "account_id": account_id, "to_account_id": to_account_id, "category_id": cat_id,
            "merchant": ex.get("merchant"), "note": f"{subject}".strip()[:200], "source": source,
            "status": status, "confidence": conf, "raw_text": text[:4000], "external_id": mid,
            "ledger_id": int(db.get_setting("default_ledger_id", "1") or 1)}
    try:
        tx = ledger.create_tx(db, data)
    except ledger.ValidationError as e:
        data["status"] = status = "review"
        tx = ledger.create_tx(db, data)
        reasons.append(str(e))
    _log(db, **logbase, result=status, detail="; ".join(reasons), rule_name=ex.get("rule_name"), confidence=conf,
         transaction_id=tx["id"])
    ex.pop("text", None)
    return {"created": True, "result": status, "confidence": conf, "reasons": reasons, "extracted": ex, "transaction": tx}


# ---------------------------------------------------------------- IMAP
def _decode(h: str | None) -> str:
    if not h:
        return ""
    parts = []
    for txt, enc in email.header.decode_header(h):
        if isinstance(txt, bytes):
            try:
                parts.append(txt.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                parts.append(txt.decode("utf-8", errors="replace"))
        else:
            parts.append(txt)
    return "".join(parts).strip()


def message_body(m: Message) -> str:
    plain, html = [], []
    for part in (m.walk() if m.is_multipart() else [m]):
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            txt = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except (LookupError, AttributeError):
            continue
        (html if part.get_content_type() == "text/html" else plain).append(txt)
    return "\n".join(plain) if plain else "\n".join(html)


def _split(s: str | None) -> list[str]:
    return [x.strip().lower() for x in re.split(r"[,;\n]", s or "") if x.strip()]


def imap_connect(mb: dict, password: str) -> imaplib.IMAP4:
    if mb.get("use_ssl", 1):
        conn = imaplib.IMAP4_SSL(mb["host"], int(mb.get("port") or 993), timeout=30)
    else:
        conn = imaplib.IMAP4(mb["host"], int(mb.get("port") or 143), timeout=30)
        try:
            conn.starttls()
        except Exception:
            pass
    conn.login(mb["username"], password)
    return conn


def _quote_folder(f: str) -> str:
    f = f or "INBOX"
    return f'"{f}"' if (" " in f or "[" in f) and not f.startswith('"') else f


def test_mailbox(mb: dict, password: str) -> dict:
    try:
        conn = imap_connect(mb, password)
        typ, data = conn.select(_quote_folder(mb.get("folder")), readonly=True)
        if typ != "OK":
            conn.logout()
            return {"ok": False, "error": f"Login OK but folder '{mb.get('folder')}' not found"}
        n = int(data[0] or 0)
        typ, folders = conn.list()
        conn.logout()
        names = []
        for f in folders or []:
            mm = re.search(rb'"([^"]+)"\s*$|(\S+)\s*$', f)
            if mm:
                names.append((mm.group(1) or mm.group(2)).decode(errors="replace"))
        return {"ok": True, "messages_in_folder": n, "folders": names[:100]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def poll_mailbox(db: Database, mb: dict, secret: SecretBox, max_messages: int = 200) -> dict:
    stats = {"checked": 0, "matched": 0, "created": 0, "duplicates": 0, "skipped": 0, "errors": 0}
    status = "ok"
    try:
        password = secret.decrypt(mb["password_enc"] or "")
        conn = imap_connect(mb, password)
        typ, _ = conn.select(_quote_folder(mb.get("folder")), readonly=False)
        if typ != "OK":
            raise RuntimeError(f"Folder {mb.get('folder')} not found")
        since = (datetime.now() - timedelta(days=int(mb.get("lookback_days") or 7))).strftime("%d-%b-%Y")
        typ, data = conn.uid("SEARCH", None, "SINCE", since)
        uids = (data[0] or b"").split()[-max_messages:]
        allow, keywords = _split(mb.get("sender_allowlist")), _split(mb.get("subject_keywords"))
        rules = db.all("SELECT * FROM email_rules WHERE enabled=1") if mb.get("watch_rules") else []
        for uid in uids:
            stats["checked"] += 1
            typ, hd = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])")
            if typ != "OK" or not hd or not isinstance(hd[0], tuple):
                continue
            h = email.message_from_bytes(hd[0][1])
            mid = (h.get("Message-ID") or "").strip() or f"uid:{mb['id']}:{uid.decode()}"
            sender, subject = _decode(h.get("From")), _decode(h.get("Subject"))
            if db.one("SELECT 1 x FROM email_seen WHERE message_id=?", (mid,)):
                stats["duplicates"] += 1
                continue
            bills_only = False
            if (allow and not any(a in sender.lower() for a in allow)) or \
                    (keywords and not any(k in subject.lower() for k in keywords)):
                # outside the filter: with watch_rules, still take what an action rule targets or what looks like a bill
                why = extra_reason(rules, sender, subject) if mb.get("watch_rules") else None
                if not why:
                    continue
                bills_only = why == "bill"
            stats["matched"] += 1
            typ, full = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not full or not isinstance(full[0], tuple):
                stats["errors"] += 1
                continue
            m = email.message_from_bytes(full[0][1])
            res = ingest_email(db, {"from": sender, "subject": subject, "body": message_body(m),
                                    "received_at": h.get("Date"), "message_id": mid}, mailbox=mb, bills_only=bills_only)
            if res.get("created"):
                stats["created"] += 1
            else:
                stats["skipped"] += 1
            action = mb.get("after_action") or "none"
            if action == "mark_read":
                conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            elif action == "move" and mb.get("move_folder"):
                dest = _quote_folder(mb["move_folder"])
                try:
                    typ, _ = conn.uid("MOVE", uid, dest)
                except imaplib.IMAP4.error:
                    typ = "NO"
                if typ != "OK":
                    if conn.uid("COPY", uid, dest)[0] == "OK":
                        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
                        conn.expunge()
        conn.logout()
    except Exception as e:  # noqa: BLE001
        status = f"error: {type(e).__name__}: {e}"
        stats["errors"] += 1
        _log(db, mailbox_id=mb["id"], source="imap", sender="", subject="", result="error", detail=status[:500])
    summary = f"{status} | checked {stats['checked']}, matched {stats['matched']}, created {stats['created']}, dup {stats['duplicates']}"
    with db.tx() as con:
        con.execute("UPDATE mailboxes SET last_checked_at=?, last_status=? WHERE id=?", (now_iso(), summary[:500], mb["id"]))
    return {"status": status, **stats}


def extra_reason(rules: list[dict], sender: str, subject: str) -> str | None:
    """Why an email outside a mailbox's sender / subject filter is still taken (mailbox option watch_rules):
    'rule' = an enabled rule of an action kind (bill, schedule, loan, credit, points) matches its sender / subject;
    'bill' = its subject reads like a bill or a payment confirmation (then only the bill flow runs); None = skip."""
    from .bills import BILL_HINTS, PAID_HINTS, _low
    from .email_rules import ACTION_KINDS, rule_matches
    if any(r.get("kind") in ACTION_KINDS and rule_matches(r, sender, subject) for r in rules):
        return "rule"
    s = _low(subject)
    return "bill" if any(h in s for h in BILL_HINTS + PAID_HINTS) else None


def due_mailboxes(db: Database, now: datetime | None = None) -> list[dict]:
    """Enabled mailboxes due for a check: interval mode = last check older than interval_minutes; fixed mode = the
    latest scheduled time (app time zone) hasn't run yet (catches up once after downtime). Fixed-time slots are
    claimed atomically here, so a slot is polled at most once."""
    from . import mail_schedule as ms
    out = []
    for mb in db.all("SELECT * FROM mailboxes WHERE enabled=1"):
        try:
            due, key = ms.is_due(db, mb, now)
        except ms.ScheduleError:
            continue
        if not due:
            continue
        if key and not ms.claim(db, mb["id"], key):
            continue
        out.append(mb)
    return out


# ---------------------------------------------------------------- receipt photos 小票
def receipt_draft(db: Database, parsed: dict, account_id=None) -> dict:
    """Draft transaction from parsed receipt fields; category/account guessed by the quick-add parser (merchant
    memory, keywords, account names in the text). Nothing is saved."""
    merchant, total = parsed.get("merchant"), parsed.get("total")
    probe = " ".join(x for x in (merchant, f"{total:.2f}" if total else None) if x) or "receipt"
    res = parse_quick_add(probe, ledger.parse_ctx(db))
    draft = {"kind": "expense", "amount": total, "currency": parsed.get("currency"), "merchant": merchant,
             "date": parsed.get("date"), "category_id": res.category_id,
             "account_id": int(account_id) if account_id else res.account_id, "note": "Receipt 小票"}
    # tax/tip only when printed on the receipt (None otherwise: never invented). A service charge counts as tip.
    tip = None if parsed.get("tip") is None and parsed.get("service") is None else round((parsed.get("tip") or 0) + (parsed.get("service") or 0), 2)
    if total is not None and (parsed.get("tax") or 0) + (tip or 0) <= total + 0.001:
        draft["tax_amount"], draft["tip_amount"] = parsed.get("tax"), tip
    if draft["currency"] and draft["account_id"]:
        acur = (db.one("SELECT currency FROM accounts WHERE id=?", (draft["account_id"],)) or {}).get("currency")
        if acur == draft["currency"]:
            draft["currency"] = None  # same as the account: no conversion
    uncertain = [k for k in ("amount", "merchant", "date", "category_id", "account_id") if not draft.get(k if k != "amount" else "amount")]
    conf = parsed.get("confidence") or 0.0
    if res.category_id:
        conf += 0.05
    if draft["account_id"]:
        conf += 0.05
    return {"draft": draft, "confidence": round(min(conf, 1.0), 2), "uncertain": uncertain}


def receipt_message(out: dict, zh: bool = False) -> str:
    d = out.get("draft") or {}
    amt = fmt_money(d.get("amount"), d.get("currency") or "") if d.get("amount") is not None else None
    what = " ".join(x for x in (d.get("merchant"), amt) if x) or ("小票" if zh else "receipt")
    if out.get("transaction"):
        st = out["transaction"]["status"]
        return (f"已记: {what}" if st == "posted" else f"待确认: {what}") if zh else (f"Saved: {what}" if st == "posted" else f"To review: {what}")
    if not out.get("ocr", {}).get("available"):
        return "已保存照片 (无 OCR): 请手动填写" if zh else "Photo saved (no OCR on server): fill in the details"
    if d.get("amount") is None:
        return "未识别出金额: 请手动填写" if zh else f"Couldn't read the total: check {what}"
    return f"识别: {what}" if zh else f"Read: {what}"
