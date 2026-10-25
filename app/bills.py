"""Email-driven bill schedules 账单待付: bill / payment-request emails become payment tasks with a reminder before
the due date, and payment-confirmation emails mark the matching task paid.

Flow (called from ingest.ingest_email for every new email, after the duplicate check):
* classify(): 'bill' (amount due / invoice / payment request / 账单 …), 'paid' (payment received / thank you for your
  payment / 还款成功 …) or None. A parsing rule of kind 'bill' or 'bill_paid' forces the type.
* bill  -> parse payee, amount, currency, due date and reference; create a `bills` row (status open) unless the same
           bill (payee + due date + amount) is already open, in which case the email is linked to it. No transaction is
           recorded: nothing has been paid yet. A 'bill' reminder is due on the due date and shows from
           `lead_days` (per bill) or the setting `bill_reminder_lead_days` (default 3) days before it.
* paid  -> match_payment() against open bills:
             payee   same biller (normalized names: equal, one contains the other, or most words shared; or the same
                     sender domain as the bill email)
             amount  within max(bill_match_tolerance (default 1.00), 1 %) of the bill amount; a bill without an amount
                     is never matched automatically
             date    paid between 3 days before the bill was issued and `bill_match_days` (default 10) days after the
                     due date (no due date: within 45 days of the issue date)
           exactly one candidate -> the bill is marked paid (paid_at, paid_message_id, paid_subject, matched='auto');
           none or several      -> a `bill_payments` row for review (status 'unmatched' / 'ambiguous' + candidate ids);
                                   nothing is guessed. The normal email flow then continues (as before this feature),
                                   so a card-payment email still lands in To review.
Bills, their reminders and unmatched payments are also managed by hand (API / Bills page).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from .db import Database, now_iso
from .ledger import ValidationError

STATUSES = ("open", "paid", "cancelled")
LEAD_DAYS_DEFAULT = 3
MATCH_TOL_ABS_DEFAULT, MATCH_TOL_PCT = 1.0, 0.01
MATCH_DAYS_DEFAULT = 10
NO_DUE_WINDOW_DAYS = 45
EPS = 0.005


class NotFound(KeyError):
    pass


# ------------------------------------------------------------------ classification
BILL_HINTS = [
    "amount due", "total due", "balance due", "payment due", "due date", "due on", "due by", "is due",
    "your bill is ready", "your bill is available", "new bill", "bill is ready", "e-bill", "ebill", "statement is ready",
    "statement is available", "invoice", "payment request", "requested a payment", "request for payment",
    "requests payment", "please pay", "pay now", "minimum payment", "账单", "应还", "还款日", "缴费通知", "待支付", "待缴",
    "付款通知", "缴费提醒",
]
PAID_HINTS = [
    "payment confirmation", "payment confirmed", "confirming your payment", "payment received", "we received your payment",
    "we've received your payment", "we have received your payment", "received your payment", "thank you for your payment",
    "thanks for your payment", "payment successful", "payment was successful", "payment is complete", "payment complete",
    "payment has been processed", "payment processed", "payment posted", "your payment of", "you paid", "you sent a payment",
    "payment sent", "receipt for your payment", "paid in full", "autopay payment", "还款成功", "已还款", "缴费成功", "支付成功",
    "付款成功", "已缴费",
]
# "thank you for your payment" also ends many bills ("if you've already paid, thank you") - so the subject decides first
_SPACE = re.compile(r"\s+")


def _low(s: str) -> str:
    return _SPACE.sub(" ", (s or "").lower())


# a bill hint alone ("invoice", "statement") is not enough: receipts say that too. A bill also needs a due date in the
# text or one of these explicit requests to pay.
REQUEST_HINTS = ["amount due", "total due", "balance due", "payment due", "please pay", "payment request",
                 "requested a payment", "request for payment", "requests payment", "pay now", "minimum payment", "应还",
                 "待支付", "待缴", "缴费通知", "付款通知"]
# confirmations that clearly settle a bill (as opposed to "支付成功" / "payment successful" on any purchase alert)
STRONG_PAID = re.compile(r"payment (?:confirmation|confirmed|received|has been processed|processed|posted)|received your payment|"
                         r"thank(?:s| you) for your payment|confirming your payment|receipt for your payment|bill payment|"
                         r"paid in full|还款成功|已还款|缴费成功|已缴费")


def _is_bill(text: str, low: str) -> bool:
    return any(h in low for h in REQUEST_HINTS) or _due_date(text, date.today()) is not None


def classify(subject: str, body: str) -> str | None:
    """'bill', 'paid' or None. The subject decides first; the body only when the subject is neutral."""
    s, b = _low(subject), _low(body)
    both = f"{subject}\n{body}"
    sp, sb = any(h in s for h in PAID_HINTS), any(h in s for h in BILL_HINTS)
    if sp and sb:  # e.g. "Payment received - amount due $0": the stronger phrase wins
        sp, sb = (True, False) if STRONG_PAID.search(s) else (False, True)
    if sp:
        return "paid"
    if sb:
        return "bill" if _is_bill(both, _low(both)) else None
    bp, bb = any(h in b for h in PAID_HINTS), any(h in b for h in BILL_HINTS)
    if bp and bb:  # both in the body ("if you've already paid, thank you for your payment" ends many bills)
        if re.search(r"we(?:'ve| have)? received your payment|payment (?:was )?(?:received|successful|confirmed|processed)|you paid", b) \
                and not _due_date(both, date.today()):
            return "paid"
        bp = False
    if bp:
        return "paid"
    if bb and _is_bill(both, _low(both)) and _amount(body)[0] is not None:
        return "bill"
    return None


# ------------------------------------------------------------------ extraction
_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_CUR = [("$", "USD"), ("US$", "USD"), ("€", "EUR"), ("£", "GBP"), ("¥", "CNY"), ("￥", "CNY")]
_NUM = r"([\d]{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
_MONEY = r"(?:(US\$|\$|€|£|¥|￥)\s?" + _NUM + r"|" + _NUM + r"\s?(USD|EUR|GBP|CNY|RMB|元))"
AMOUNT_LABELS = [
    r"amount due", r"total amount due", r"total due", r"balance due", r"new balance", r"statement balance",
    r"amount to pay", r"payment amount", r"amount paid", r"total paid", r"you paid", r"payment of", r"amount",
    r"total", r"本期应还(?:金额)?", r"应还(?:金额)?", r"应缴(?:金额)?", r"账单金额", r"还款金额", r"缴费金额", r"支付金额", r"金额",
]
DUE_LABELS = r"(?:payment due date|due date|due on|due by|pay by|is due(?: on)?|due|到期还款日|最后还款日|还款日|缴费截止日?|截止日期|到期日)"
PAID_DATE_LABELS = r"(?:payment date|paid on|date paid|date of payment|processed on|posted on|还款日期|支付时间|缴费时间)"


def _money(m: re.Match) -> tuple[float | None, str | None]:
    sym, n1, n2, code = m.group(1), m.group(2), m.group(3), m.group(4)
    num = n1 or n2
    try:
        v = round(float(num.replace(",", "")), 2)
    except (TypeError, ValueError, AttributeError):
        return None, None
    cur = dict(_CUR).get(sym) if sym else {"RMB": "CNY", "元": "CNY"}.get(code, code)
    return v, cur


def _amount(text: str) -> tuple[float | None, str | None]:
    """Labelled amount first (amount due, total due, payment amount, 应还金额 …), else the first money amount."""
    t = text or ""
    for lab in AMOUNT_LABELS:
        m = re.search(r"(?i)" + lab + r"\s*(?:is|was|of)?\s*[:：]?\s*(?:\(\w+\))?\s*" + _MONEY, t)
        if m:
            v, cur = _money_tail(m)
            if v is not None:
                return v, cur
    m = re.search(_MONEY, t)
    return _money(m) if m else (None, None)


def _money_tail(m: re.Match) -> tuple[float | None, str | None]:
    """The money groups are the last 4 groups of a labelled match."""
    g = m.groups()[-4:]
    sym, n1, n2, code = g
    num = n1 or n2
    try:
        v = round(float(num.replace(",", "")), 2)
    except (TypeError, ValueError, AttributeError):
        return None, None
    cur = dict(_CUR).get(sym) if sym else {"RMB": "CNY", "元": "CNY"}.get(code, code)
    return v, cur


def parse_date_text(s: str, ref: date) -> date | None:
    """A date written in an email: 2026-11-05, 11/05/2026, 11/05/26, 11/05 (next one after ref), November 5, 2026,
    Nov 5 (next one on/after ref - 7 days), 5 November 2026, 2026年11月5日, 11月5日."""
    s = (s or "").strip()
    try:
        m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})[日号]", s)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.match(r"(\d{1,2})月\s*(\d{1,2})[日号]", s)
        if m:
            return _roll(ref, int(m.group(1)), int(m.group(2)))
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
        if m:
            y = int(m.group(3)); y += 2000 if y < 100 else 0
            return date(y, int(m.group(1)), int(m.group(2)))
        m = re.match(r"(\d{1,2})/(\d{1,2})\b", s)
        if m:
            return _roll(ref, int(m.group(1)), int(m.group(2)))
        m = re.match(r"(?i)(?:(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", s)
        if m and m.group(1).lower() in _MONTHS:
            mo, d = _MONTHS[m.group(1).lower()], int(m.group(2))
            return date(int(m.group(3)), mo, d) if m.group(3) else _roll(ref, mo, d)
        m = re.match(r"(?i)(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3})[a-z]*\.?,?(?:\s+(\d{4}))?", s)
        if m and m.group(2).lower() in _MONTHS:
            mo, d = _MONTHS[m.group(2).lower()], int(m.group(1))
            return date(int(m.group(3)), mo, d) if m.group(3) else _roll(ref, mo, d)
    except ValueError:
        return None
    return None


def _roll(ref: date, mo: int, d: int) -> date:
    """A date without a year: this year, or next year when that is more than a week before ref (Dec email, Jan due)."""
    x = date(ref.year, mo, d)
    return date(ref.year + 1, mo, d) if x < ref - timedelta(days=7) else x


def _labelled_date(text: str, labels: str, ref: date) -> date | None:
    for m in re.finditer(r"(?i)" + labels + r"\s*(?:is|was|on|by|date)?\s*[:：]?\s*", text or ""):
        d = parse_date_text(text[m.end():m.end() + 40], ref)
        if d:
            return d
    return None


def _due_date(text: str, ref: date) -> date | None:
    return _labelled_date(text, DUE_LABELS, ref)


PAYEE_PATTERNS = [
    r"(?im)^\s*(?:payee|biller|pay to|paid to|merchant|company|vendor|from|收款方|收款单位|商户|缴费单位)\s*[:：]\s*(.+?)\s*$",
    r"(?i)\b(?:payment|bill payment)(?:\s+of\s+\S+(?:\s+(?:USD|EUR|GBP|CNY|RMB))?)?\s+to\s+([A-Z0-9][\w&.'\- ]{1,40}?)(?:\s+(?:of|for|was|has|is|on|in the amount)\b|[.,!\n]|$)",
    r"(?i)\byour\s+([A-Z0-9][\w&.'\- ]{1,40}?)\s+(?:bill|invoice|statement|e-bill|ebill)\b",
    r"(?i)\b(?:bill|invoice|statement|payment request)\s+from\s+([A-Z0-9][\w&.'\- ]{1,40}?)(?:\s+(?:is|was|has|for|of|due)\b|[.,!:\n]|$)",
    r"(?i)\b([A-Z0-9][\w&.'\- ]{1,40}?)\s+(?:has\s+)?(?:sent you|requested)\s+(?:a\s+)?(?:payment request|an invoice|a bill|payment)\b",
    r"(?i)\bthank you for (?:your )?pay(?:ment|ing)(?:\s+to)?\s+([A-Z0-9][\w&.'\- ]{1,40}?)(?:\s+(?:of|for|on)\b|[.,!\n]|$)",
]
_GENERIC_NAMES = {"your", "the", "a", "an", "this", "our", "new", "monthly", "latest", "recent", "online", "electronic", "e",
                  "payment", "bill", "account", "card", "credit card", "statement", "you", "us", "me", "noreply", "no reply",
                  "no-reply", "notification", "notifications", "alerts", "billing", "customer service", "service", "info"}


def _sender_name(sender: str) -> str | None:
    """Display name of 'PG&E <noreply@pge.com>', else the domain's main label (pge.com -> pge)."""
    s = (sender or "").strip()
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', s)
    if m and m.group(1).strip() and "@" not in m.group(1):
        name = re.sub(r"(?i)\b(?:billing|payments?|notifications?|alerts?|no-?reply|customer service|team)\b", " ", m.group(1))
        name = _SPACE.sub(" ", name).strip(" -|,")
        if name and name.lower() not in _GENERIC_NAMES:
            return name
    dom = sender_domain(s)
    return dom.split(".")[0] if dom else None


def sender_domain(sender: str) -> str | None:
    """Registrable domain of the sender (mail.pge.com -> pge.com)."""
    m = re.search(r"@([\w.-]+)", sender or "")
    if not m:
        return None
    parts = m.group(1).lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else parts[0]


def _clean_payee(p: str | None) -> str | None:
    if not p:
        return None
    p = re.sub(r"<[^>]*>", "", p)
    p = _SPACE.sub(" ", p).strip(" .,:;-|\"'")
    p = re.sub(r"(?i)^(?:the|your)\s+", "", p)
    if not p or p.lower() in _GENERIC_NAMES or len(p) < 2:
        return None
    return p[:80]


def _payee(sender: str, subject: str, body: str) -> str | None:
    for text in (subject or "", body or ""):
        for rx in PAYEE_PATTERNS:
            m = re.search(rx, text)
            if m:
                p = _clean_payee(m.group(1))
                if p:
                    return p
    return _clean_payee(_sender_name(sender))


REF_RX = r"(?i)(?:invoice|bill|statement|reference|ref|confirmation|account)\s*(?:number|no\.?|num|#)\s*[:#]?\s*([A-Z0-9][A-Z0-9-]{3,})"


def parse_bill_email(sender: str, subject: str, body: str, received: date | str | None = None,
                     kind: str | None = None) -> dict:
    """{type: bill|paid|None, payee, amount, currency, due_date, paid_date, reference, sender_domain}. No writes.
    kind forces the type ('bill' / 'paid', from a parsing rule)."""
    from .email_rules import strip_html
    text = strip_html(body or "")
    ref = received if isinstance(received, date) else (_as_date(received) or date.today())
    typ = kind or classify(subject, text)
    amount, cur = _amount(f"{subject}\n{text}")
    out = {"type": typ, "payee": _payee(sender, subject or "", text), "amount": amount, "currency": cur,
           "due_date": None, "paid_date": None, "reference": None, "sender_domain": sender_domain(sender),
           "email_date": ref.isoformat()}
    m = re.search(REF_RX, f"{subject}\n{text}")
    if m:
        out["reference"] = m.group(1)
    if typ == "bill":
        d = _due_date(f"{subject}\n{text}", ref)
        out["due_date"] = d.isoformat() if d else None
    elif typ == "paid":
        d = _labelled_date(f"{subject}\n{text}", PAID_DATE_LABELS, ref)
        out["paid_date"] = (d or ref).isoformat()
    return out


def _as_date(v) -> date | None:
    if not v:
        return None
    if isinstance(v, date):
        return v
    s = str(v)
    try:
        return datetime.strptime(s.strip().replace("T", " ")[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    try:
        import email.utils
        return email.utils.parsedate_to_datetime(s).date()
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ payee matching
_STOP = {"inc", "llc", "ltd", "co", "corp", "corporation", "company", "the", "of", "and", "payments", "payment", "billing",
         "bill", "services", "service", "online", "com", "net", "org", "us", "usa", "na", "group", "customer"}


def norm_payee(p: str | None) -> str:
    s = re.sub(r"[^\w\u3400-\u9fff]+", " ", (p or "").lower().replace("&", " and "))
    return " ".join(w for w in s.split() if w not in _STOP)


def payee_matches(a: str | None, b: str | None) -> bool:
    na, nb = norm_payee(a), norm_payee(b)
    if not na or not nb:
        return False
    if na == nb or na.replace(" ", "") == nb.replace(" ", ""):
        return True
    ca, cb = na.replace(" ", ""), nb.replace(" ", "")
    if min(len(ca), len(cb)) >= 3 and (ca in cb or cb in ca):
        return True
    wa, wb = set(na.split()), set(nb.split())
    return bool(wa & wb) and len(wa & wb) / len(wa | wb) >= 0.5


# ------------------------------------------------------------------ settings
def _int(db: Database, key: str, default: int, hi: int = 365) -> int:
    try:
        return max(0, min(hi, int(db.get_setting(key) or default)))
    except (TypeError, ValueError):
        return default


def lead_days(db: Database) -> int:
    return _int(db, "bill_reminder_lead_days", LEAD_DAYS_DEFAULT)


def match_days(db: Database) -> int:
    return _int(db, "bill_match_days", MATCH_DAYS_DEFAULT)


def match_tolerance(db: Database) -> float:
    try:
        return max(0.0, float(db.get_setting("bill_match_tolerance") or MATCH_TOL_ABS_DEFAULT))
    except (TypeError, ValueError):
        return MATCH_TOL_ABS_DEFAULT


def tolerance_for(amount: float, abs_tol: float) -> float:
    return max(abs_tol, round(abs(amount) * MATCH_TOL_PCT, 2))


# ------------------------------------------------------------------ storage helpers
def _num(v, what: str) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = round(float(v), 2)
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be a number")
    if x <= 0:
        raise ValidationError(f"{what} must be greater than zero")
    return x


def _day(v, what: str) -> str | None:
    if v in (None, ""):
        return None
    d = _as_date(v)
    if not d:
        raise ValidationError(f"{what} must be YYYY-MM-DD")
    return d.isoformat()


def today(db: Database) -> date:
    from .schedules import local_today
    return local_today(db)


SELECT = "SELECT b.*, a.name account_name FROM bills b LEFT JOIN accounts a ON a.id=b.account_id"


def _decorate(b: dict, on: date) -> dict:
    b = dict(b)
    due = _as_date(b.get("due_date"))
    b["days_left"] = (due - on).days if due else None
    b["overdue"] = bool(b["status"] == "open" and due and due < on)
    b["label"] = b.get("payee") or b.get("subject") or f"Bill #{b['id']}"
    return b


def get(db: Database, bid: int) -> dict:
    b = db.one(SELECT + " WHERE b.id=?", (bid,))
    if not b:
        raise NotFound(bid)
    return _decorate(b, today(db))


def list_bills(db: Database, q: dict | None = None) -> dict:
    """?status=open (default) | paid | cancelled | all (comma list ok)."""
    q = q or {}
    on = today(db)
    rows = [_decorate(b, on) for b in db.all(SELECT + " ORDER BY b.due_date IS NULL, b.due_date, b.id")]
    counts = {s: sum(1 for b in rows if b["status"] == s) for s in STATUSES}
    want = str(q.get("status") or "open")
    if want != "all":
        ws = {s.strip() for s in want.split(",")}
        rows = [b for b in rows if b["status"] in ws]
    return {"items": rows, "status_counts": counts, "review_count": review_count(db), "lead_days": lead_days(db),
            "today": on.isoformat()}


FIELDS = ("payee", "amount", "currency", "due_date", "issued_date", "account_id", "reference", "note", "lead_days")


def _clean(db: Database, body: dict, ex: dict | None = None) -> dict:
    d = dict(ex or {})
    d.update({k: body[k] for k in FIELDS if k in body})
    d["payee"] = str(d.get("payee") or "").strip()[:120]
    if not d["payee"]:
        raise ValidationError("Payee is required")
    d["amount"] = _num(d.get("amount"), "Amount")
    d["currency"] = str(d.get("currency") or "").strip().upper() or db.get_setting("base_currency", "USD")
    d["due_date"] = _day(d.get("due_date"), "Due date")
    d["issued_date"] = _day(d.get("issued_date"), "Issued date") or today(db).isoformat()
    if d.get("account_id") in (None, ""):
        d["account_id"] = None
    else:
        try:
            d["account_id"] = int(d["account_id"])
        except (TypeError, ValueError):
            raise ValidationError("account_id must be a number")
        if not db.one("SELECT 1 x FROM accounts WHERE id=?", (d["account_id"],)):
            raise ValidationError("Unknown account")
    if d.get("lead_days") in (None, ""):
        d["lead_days"] = None
    else:
        try:
            d["lead_days"] = int(d["lead_days"])
        except (TypeError, ValueError):
            raise ValidationError("lead_days must be a whole number")
        if not 0 <= d["lead_days"] <= 365:
            raise ValidationError("lead_days must be between 0 and 365")
    for k in ("reference", "note"):
        d[k] = str(d.get(k) or "").strip()[:500]
    return {k: d[k] for k in FIELDS}


def create(db: Database, body: dict, extra: dict | None = None) -> dict:
    row = _clean(db, body or {})
    row.update({"status": "open", "source": (extra or {}).get("source", "web"), "created_at": now_iso(), "updated_at": now_iso()})
    for k in ("message_id", "sender", "sender_domain", "subject", "raw_text"):
        if (extra or {}).get(k) is not None:
            row[k] = extra[k]
    with db.tx() as con:
        bid = con.execute(f"INSERT INTO bills({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values())).lastrowid
    _sync(db)
    return get(db, bid)


def update(db: Database, bid: int, body: dict) -> dict:
    ex = db.one("SELECT * FROM bills WHERE id=?", (bid,))
    if not ex:
        raise NotFound(bid)
    row = _clean(db, body or {}, ex)
    row["updated_at"] = now_iso()
    with db.tx() as con:
        con.execute(f"UPDATE bills SET {','.join(k + '=?' for k in row)} WHERE id=?", list(row.values()) + [bid])
    _sync(db)
    return get(db, bid)


def delete(db: Database, bid: int) -> dict:
    if not db.one("SELECT 1 x FROM bills WHERE id=?", (bid,)):
        raise NotFound(bid)
    with db.tx() as con:
        con.execute("DELETE FROM bills WHERE id=?", (bid,))
        con.execute("UPDATE bill_payments SET bill_id=NULL, status='unmatched' WHERE bill_id=?", (bid,))
    _sync(db)
    return {"ok": True}


def mark_paid(db: Database, bid: int, body: dict | None = None) -> dict:
    """Mark paid by hand: {date?, create_payment?, from_account_id?, amount?, category_id?}. create_payment records an
    expense to the payee from the chosen account (default: the bill's account)."""
    from . import ledger
    body = body or {}
    b = get(db, bid)
    if b["status"] == "paid":
        raise ValidationError("This bill is already paid")
    when = _day(body.get("date"), "Date") or today(db).isoformat()
    tx = None
    if body.get("create_payment"):
        aid = body.get("from_account_id") or b.get("account_id")
        if not aid:
            raise ValidationError("Choose the account the payment comes from")
        amount = _num(body.get("amount"), "Amount") or b.get("amount")
        if not amount:
            raise ValidationError("Enter the amount paid")
        tx = ledger.create_tx(db, {"kind": "expense", "account_id": int(aid), "amount": amount, "currency": b["currency"],
                                   "date": when, "merchant": b["payee"], "category_id": body.get("category_id"),
                                   "note": f"Bill 账单 · {b['payee']}" + (f" · {b['reference']}" if b.get("reference") else ""),
                                   "source": "bill"})
    with db.tx() as con:
        con.execute("UPDATE bills SET status='paid', paid_at=?, paid_amount=COALESCE(?, amount), matched=?, transaction_id=COALESCE(?, transaction_id), "
                    "updated_at=? WHERE id=?", (when, _num(body.get("amount"), "Amount") if body.get("amount") not in (None, "") else None,
                                                "manual", tx["id"] if tx else None, now_iso(), bid))
    _sync(db)
    out = get(db, bid)
    out["transaction"] = tx
    return out


def cancel(db: Database, bid: int) -> dict:
    get(db, bid)
    with db.tx() as con:
        con.execute("UPDATE bills SET status='cancelled', updated_at=? WHERE id=?", (now_iso(), bid))
    _sync(db)
    return get(db, bid)


def reopen(db: Database, bid: int) -> dict:
    get(db, bid)
    with db.tx() as con:
        con.execute("UPDATE bills SET status='open', paid_at=NULL, paid_amount=NULL, paid_message_id=NULL, paid_subject=NULL, "
                    "matched=NULL, updated_at=? WHERE id=?", (now_iso(), bid))
        con.execute("UPDATE bill_payments SET status='unmatched', bill_id=NULL WHERE bill_id=? AND status='matched'", (bid,))
    _sync(db)
    return get(db, bid)


def _sync(db: Database) -> None:
    from . import reminders
    reminders.sync_bills(db)


# ------------------------------------------------------------------ matching
def candidates(db: Database, payee: str | None, amount: float | None, paid_date: date | str | None,
               sender_dom: str | None = None, currency: str | None = None) -> list[dict]:
    """Open bills this payment could be for: payee (or sender domain) AND amount within tolerance AND date window.
    Bills without an amount never qualify (they can still be matched by hand)."""
    pd = _as_date(paid_date)
    if amount is None or pd is None:
        return []
    abs_tol, days = match_tolerance(db), match_days(db)
    out = []
    for b in db.all("SELECT * FROM bills WHERE status='open'"):
        same_payee = payee_matches(payee, b["payee"]) or bool(sender_dom and b.get("sender_domain") and sender_dom == b["sender_domain"])
        if not same_payee or b["amount"] is None:
            continue
        if currency and b.get("currency") and currency != b["currency"]:
            continue
        if abs(b["amount"] - amount) > tolerance_for(b["amount"], abs_tol) + 1e-9:
            continue
        issued, due = _as_date(b.get("issued_date")), _as_date(b.get("due_date"))
        lo = (issued or (due - timedelta(days=30) if due else pd)) - timedelta(days=3)
        hi = (due + timedelta(days=days)) if due else ((issued or pd) + timedelta(days=NO_DUE_WINDOW_DAYS))
        if not lo <= pd <= hi:
            continue
        out.append(dict(b, amount_diff=round(abs(b["amount"] - amount), 2),
                        days_from_due=(pd - due).days if due else None))
    return out


def match_payment(db: Database, payment: dict) -> dict:
    """{status: matched|unmatched|ambiguous, bill?, candidates}. Only a single candidate is matched (never a guess)."""
    if not payment.get("payee") and not payment.get("sender_domain"):
        return {"status": "unmatched", "reason": "no payee found", "candidates": []}
    if payment.get("amount") is None:
        return {"status": "unmatched", "reason": "no amount found", "candidates": []}
    c = candidates(db, payment.get("payee"), payment.get("amount"), payment.get("paid_date"),
                   payment.get("sender_domain"), payment.get("currency"))
    if len(c) == 1:
        return {"status": "matched", "bill": c[0], "candidates": c}
    if len(c) > 1:
        return {"status": "ambiguous", "reason": f"{len(c)} open bills fit", "candidates": c}
    return {"status": "unmatched", "reason": "no open bill with this payee, amount and date", "candidates": []}


def _apply_match(con, bill_id: int, pay: dict, how: str) -> None:
    con.execute("""UPDATE bills SET status='paid', paid_at=?, paid_amount=?, paid_message_id=?, paid_subject=?, matched=?,
        updated_at=? WHERE id=?""", (pay.get("paid_date"), pay.get("amount"), pay.get("message_id"),
                                     (pay.get("subject") or "")[:300], how, now_iso(), bill_id))


# ------------------------------------------------------------------ email entry point
def handle_email(db: Database, sender: str, subject: str, body: str, received: str | None, message_id: str,
                 rule_kind: str | None = None) -> dict | None:
    """Called by ingest for each new email. Returns None when the email is neither a bill nor a payment confirmation;
    otherwise {type, action, stop, ...}. stop=True: the email is fully handled (no transaction should be made)."""
    kind = {"bill": "bill", "bill_paid": "paid"}.get(rule_kind or "")
    p = parse_bill_email(sender, subject, body, received, kind)
    if p["type"] == "bill":
        return _handle_bill(db, p, sender, subject, body, message_id)
    if p["type"] == "paid":
        return _handle_paid(db, p, subject, message_id, forced=bool(kind))
    return None


def _handle_bill(db: Database, p: dict, sender: str, subject: str, body: str, mid: str) -> dict:
    if not p["payee"]:
        return {"type": "bill", "action": "skipped", "stop": False, "parsed": p, "reason": "no payee found"}
    if p["amount"] is None and not p["due_date"]:
        return {"type": "bill", "action": "skipped", "stop": False, "parsed": p, "reason": "no amount or due date found"}
    # the same bill again (a reminder email for a bill we already have): link instead of duplicating
    for b in db.all("SELECT * FROM bills WHERE status='open'"):
        if payee_matches(p["payee"], b["payee"]) and (b["due_date"] or None) == p["due_date"] and \
                (b["amount"] is None or p["amount"] is None or abs(b["amount"] - p["amount"]) < EPS):
            if b["amount"] is None and p["amount"] is not None:
                with db.tx() as con:
                    con.execute("UPDATE bills SET amount=?, currency=COALESCE(currency, ?), updated_at=? WHERE id=?",
                                (p["amount"], p["currency"], now_iso(), b["id"]))
                _sync(db)
            return {"type": "bill", "action": "existing", "stop": True, "parsed": p, "bill": get(db, b["id"])}
    bill = create(db, {"payee": p["payee"], "amount": p["amount"], "currency": p["currency"], "due_date": p["due_date"],
                       "issued_date": p["email_date"], "reference": p["reference"]},
                  {"source": "email", "message_id": mid, "sender": (sender or "")[:200], "sender_domain": p["sender_domain"],
                   "subject": (subject or "")[:300], "raw_text": (body or "")[:4000]})
    return {"type": "bill", "action": "created", "stop": True, "parsed": p, "bill": bill}


def related_open_bills(db: Database, payee: str | None, sender_dom: str | None) -> list[dict]:
    return [b for b in db.all("SELECT * FROM bills WHERE status='open'")
            if payee_matches(payee, b["payee"]) or (sender_dom and b.get("sender_domain") == sender_dom)]


def _handle_paid(db: Database, p: dict, subject: str, mid: str, forced: bool = False) -> dict | None:
    pay = dict(p, message_id=mid, subject=subject)
    m = match_payment(db, pay)
    if m["status"] == "unmatched" and not forced and not related_open_bills(db, p["payee"], p["sender_domain"]):
        # a confirmation with no open bill from this payee at all (a card payment, any purchase alert): not a bill
        # matter - nothing to review here, the normal email flow handles it
        return None
    with db.tx() as con:
        if m["status"] == "matched":
            _apply_match(con, m["bill"]["id"], pay, "auto")
        pid = con.execute("""INSERT INTO bill_payments(message_id, subject, payee, amount, currency, paid_date, sender_domain,
            reference, status, bill_id, candidates, reason, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, (subject or "")[:300], p["payee"], p["amount"], p["currency"], p["paid_date"], p["sender_domain"],
             p["reference"], m["status"], m["bill"]["id"] if m["status"] == "matched" else None,
             json.dumps([c["id"] for c in m["candidates"]]), m.get("reason"), now_iso(), now_iso())).lastrowid
    _sync(db)
    out = {"type": "paid", "action": m["status"], "stop": m["status"] == "matched", "parsed": p, "payment_id": pid,
           "candidates": [c["id"] for c in m["candidates"]], "reason": m.get("reason")}
    if m["status"] == "matched":
        out["bill"] = get(db, m["bill"]["id"])
    return out


# ------------------------------------------------------------------ payments review
def review_count(db: Database) -> int:
    return db.one("SELECT COUNT(*) n FROM bill_payments WHERE status IN ('unmatched','ambiguous')")["n"]


def list_payments(db: Database, status: str = "review") -> list[dict]:
    """status=review (unmatched + ambiguous, default) | matched | dismissed | all."""
    rows = db.all("SELECT * FROM bill_payments ORDER BY id DESC LIMIT 500")
    if status == "review":
        rows = [r for r in rows if r["status"] in ("unmatched", "ambiguous")]
    elif status != "all":
        rows = [r for r in rows if r["status"] == status]
    for r in rows:
        try:
            r["candidates"] = json.loads(r.get("candidates") or "[]")
        except ValueError:
            r["candidates"] = []
        # what could it be? open bills from the same payee (any amount / date), for a manual match
        r["suggestions"] = [{"id": b["id"], "payee": b["payee"], "amount": b["amount"], "currency": b["currency"],
                             "due_date": b["due_date"]}
                            for b in db.all("SELECT * FROM bills WHERE status='open' ORDER BY due_date")
                            if b["id"] in r["candidates"] or payee_matches(r.get("payee"), b["payee"])
                            or (r.get("sender_domain") and r["sender_domain"] == b.get("sender_domain"))]
    return rows


def match_manual(db: Database, pid: int, bill_id) -> dict:
    p = db.one("SELECT * FROM bill_payments WHERE id=?", (pid,))
    if not p:
        raise NotFound(pid)
    if p["status"] == "matched":
        raise ValidationError("This payment is already matched")
    try:
        bill_id = int(bill_id)
    except (TypeError, ValueError):
        raise ValidationError("Choose the bill this payment is for")
    b = db.one("SELECT * FROM bills WHERE id=?", (bill_id,))
    if not b or b["status"] != "open":
        raise ValidationError("Choose an open bill")
    with db.tx() as con:
        _apply_match(con, bill_id, p, "manual")
        con.execute("UPDATE bill_payments SET status='matched', bill_id=?, updated_at=? WHERE id=?", (bill_id, now_iso(), pid))
    _sync(db)
    return get(db, bill_id)


def dismiss_payment(db: Database, pid: int) -> dict:
    if not db.one("SELECT 1 x FROM bill_payments WHERE id=?", (pid,)):
        raise NotFound(pid)
    with db.tx() as con:
        con.execute("UPDATE bill_payments SET status='dismissed', updated_at=? WHERE id=?", (now_iso(), pid))
    return {"ok": True}
