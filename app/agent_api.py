"""POST /api/agent/transactions: one forgiving endpoint for AI agents / scripts / Shortcuts.

Accepts names instead of ids (account by id, name, alias or last-4 digits; category by id or name, "Parent > Child";
project / plan line / person by name), optional tax & tip (amount or "18%"), and an optional receipt photo
(multipart file or base64). Returns {ok, id, message, transaction, warnings}. Nothing is invented: missing tax/tip
stay null; with a receipt and no amount, the OCR draft fills only what it actually read."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from . import ingest, ledger, receipts
from .db import Database
from .parser import ParseResult, apply_tax_tip

ALIASES = {"tax_amount": "tax", "tip_amount": "tip", "member": "person", "who": "person", "payee": "merchant",
           "description": "note", "memo": "note", "type": "kind", "plan_line_id": "plan_line", "category_id": "category",
           "account_id": "account", "project_id": "project", "image": "receipt", "photo": "receipt", "file": "receipt",
           "reimburses": "expense_ids", "link_expenses": "expense_ids", "reimb_payer": "payer"}
KINDS = ("expense", "income", "refund", "transfer", "reimbursement")


class AgentError(ValueError):
    pass


def _zh() -> bool:
    return ingest.msg_zh()


def _num(v, what: str) -> float | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").replace("¥", "").strip())
    except ValueError:
        raise AgentError(f"{what} must be a number (got {v!r})")


def _pct(v) -> tuple[float, bool] | None:
    """'18%' → (18, True); '2.50' / 2.5 → (2.5, False); blank → None."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    s = str(v).strip()
    if s.endswith("%"):
        return (_num(s[:-1], "percentage"), True)
    return (_num(s, "amount"), False)


def resolve_account(db: Database, v, last4=None) -> dict:
    accts = db.all("SELECT * FROM accounts WHERE archived=0 ORDER BY id")
    if v is None and not last4:
        default = db.get_setting("default_account_id", None)
        a = next((a for a in accts if str(a["id"]) == str(default)), None)
        if a:
            return a
        raise AgentError("account is required (id, name, alias or last 4 digits)")
    s = str(v if v is not None else last4).strip()
    if isinstance(v, int) or (s.isdigit() and len(s) != 4):
        a = next((a for a in accts if a["id"] == int(s)), None)
        if a:
            return a
    low = s.lower()
    exact = [a for a in accts if a["name"].lower() == low
             or low in [x.strip().lower() for x in (a.get("aliases") or "").split(",") if x.strip()]]
    if len(exact) == 1:
        return exact[0]
    digits = re.sub(r"\D", "", s)
    if len(digits) == 4:
        m = [a for a in accts if (a.get("last4") or "") == digits]
        if len(m) == 1:
            return m[0]
        if len(m) > 1:
            raise AgentError(f"Several accounts end in {digits}: " + ", ".join(a["name"] for a in m))
    part = [a for a in accts if low in a["name"].lower()]
    if len(part) == 1:
        return part[0]
    if len(part) > 1:
        raise AgentError(f"Account '{s}' is ambiguous: " + ", ".join(a["name"] for a in part[:8]))
    raise AgentError(f"Account '{s}' not found. Accounts: " + ", ".join(a["name"] for a in accts[:20]))


def resolve_category(db: Database, v, kind: str) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip()
    cats = db.all("SELECT * FROM categories")
    if s.isdigit():
        if any(c["id"] == int(s) for c in cats):
            return int(s)
        raise AgentError(f"Category id {s} not found")
    ckind = "income" if kind == "income" else "expense"
    pool = [c for c in cats if (c.get("kind") or ckind) == ckind] or cats
    by_id = {c["id"]: c for c in cats}
    names = lambda c: {x.lower() for x in (c["name"], c.get("name_zh") or "") if x}  # noqa: E731
    parts = [p.strip().lower() for p in re.split(r"\s*(?:>|›|/|::)\s*", s) if p.strip()]
    if len(parts) == 2:
        for c in pool:
            p = by_id.get(c.get("parent_id"))
            if p and parts[0] in names(p) and parts[1] in names(c):
                return c["id"]
    hits = [c for c in pool if s.lower() in names(c)]
    if hits:
        hits.sort(key=lambda c: c.get("parent_id") is None)  # a sub-category beats a same-named top level
        return hits[0]["id"]
    low = s.lower()
    part = [c for c in pool if any(low in n or (len(n) >= 3 and n in low) for n in names(c))]  # "Dining" → "Food & Dining"
    if part:
        part.sort(key=lambda c: (c.get("parent_id") is None, len(c["name"])))
        return part[0]["id"]
    from .parser import parse_quick_add  # keyword engine: "dinner" / "uber" / "午饭" → its category
    res = parse_quick_add(f"{s} 1", ledger.parse_ctx(db))
    if res.category_id:
        return res.category_id
    raise AgentError(f"Category '{s}' not found")


def resolve_plan_line(db: Database, v, project_id) -> int | None:
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip()
    if not project_id:
        raise AgentError("plan_line needs a project")
    rows = db.all("SELECT id, name FROM project_plan_lines WHERE project_id=?", (project_id,))
    hit = next((r for r in rows if str(r["id"]) == s or r["name"].lower() == s.lower()), None)
    if not hit:
        raise AgentError(f"Plan line '{s}' not found in this project")
    return hit["id"]


def _hhmm(v) -> str | None:
    """'13:35' / '1:35 pm' / '13:35:10' -> '13:35'; anything else -> None."""
    m = receipts.TIME_RX.search(str(v or ""))
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ap:
        h = h % 12 + (12 if ap == "pm" else 0)
    return f"{h:02d}:{mi:02d}"


def tx_when(date, time=None, receipt_date: str | None = None, warnings: list[str] | None = None) -> str:
    """The transaction's date + time. A date without a time takes, in order: the `time` field, the time printed on
    the receipt (same day), the current time when the date is today; else 12:00 with a warning."""
    now = datetime.now()
    if not date:
        date = receipt_date or now.strftime("%Y-%m-%d %H:%M")
    s = str(date).strip().replace("T", " ")
    day = s[:10]
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return s  # let the ledger report the bad date
    rest = s[10:].strip()
    hm = _hhmm(rest) if rest else None
    if hm and not (hm == "00:00" and not time):
        return f"{day} {hm}"
    if time:
        hm = _hhmm(time)
        if not hm:
            raise AgentError(f"time must look like 13:35 or 1:35 pm (got {time!r})")
        return f"{day} {hm}"
    if receipt_date and receipt_date[:10] == day and len(receipt_date) > 10:
        return receipt_date[:16]
    if day == now.strftime("%Y-%m-%d"):
        return f"{day} {now.strftime('%H:%M')}"
    if warnings is not None:
        warnings.append("no time given: 12:00 used (send 'time' or a full date-time)")
    return f"{day} 12:00"


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on", "是")


def create(db: Database, body: dict, files: list[tuple[bytes, str]] | None = None) -> dict:
    b = {}
    for k, v in (body or {}).items():
        k = str(k).strip().lower()
        b[ALIASES.get(k, k)] = v
    warnings: list[str] = []
    kind = str(b.get("kind") or "expense").strip().lower()
    if kind not in KINDS:
        raise AgentError(f"kind must be one of {', '.join(KINDS)}")
    # receipt: multipart files, base64 field(s), or ids of receipts already uploaded
    files = list(files or [])
    rc = b.get("receipt")
    for x in (rc if isinstance(rc, list) else [rc] if rc else []):
        if isinstance(x, (bytes, str)) and not str(x).isdigit():
            try:
                files.append((receipts.decode_b64(re.sub(r"\s+", "", x if isinstance(x, str) else x.decode())), b.get("filename") or ""))
            except (receipts.ReceiptError, UnicodeDecodeError):
                raise AgentError("receipt must be a base64 image (data:image/...;base64,... or plain base64)")
    rids = [int(i) for i in (b.get("receipt_ids") or [])]
    saved = [receipts.save_upload(db, data, name) for data, name in files]
    parsed = (saved[0].get("parsed") or {}) if saved else {}
    try:
        acct = resolve_account(db, b.get("account"), b.get("last4") or b.get("account_last4"))
        amount = _num(b.get("amount"), "amount")
        if amount is None and parsed.get("total") is not None:
            amount = parsed["total"]; warnings.append("amount read from the receipt")
        if amount is None:
            raise AgentError("amount is required (or send a readable receipt)")
        if amount < 0:
            amount = -amount  # agents often send spending as negative
        tx: dict[str, Any] = {"kind": kind, "amount": amount, "account_id": acct["id"], "source": "agent",
                              "date": tx_when(b.get("date"), b.get("time"), parsed.get("date") if saved else None, warnings),
                              "note": b.get("note") or "", "status": "review" if _truthy(b.get("review")) else "posted"}
        if b.get("currency"):
            tx["currency"] = str(b["currency"]).upper()
        elif saved and parsed.get("currency") and parsed["currency"] != acct["currency"]:
            tx["currency"] = parsed["currency"]
        merchant = b.get("merchant") or (parsed.get("merchant") if saved and b.get("amount") is None else None)
        if merchant:
            tx["merchant"] = merchant
        if kind == "reimbursement" and b.get("payer"):
            tx["reimb_payer"] = str(b["payer"]).strip()
        if kind == "transfer":
            to = b.get("to_account")
            if to is None:
                raise AgentError("transfer needs to_account")
            tx["to_account_id"] = resolve_account(db, to)["id"]
        elif kind != "reimbursement":
            try:
                tx["category_id"] = resolve_category(db, b.get("category"), kind)
            except AgentError as e:
                warnings.append(str(e) + ": left uncategorized")
        if b.get("project") not in (None, ""):
            pv = str(b["project"]).strip()
            row = db.one("SELECT id FROM projects WHERE id=? OR name=? COLLATE NOCASE", (int(pv) if pv.isdigit() else -1, pv))
            if row:
                tx["project_id"] = row["id"]
            else:
                tx["project"] = pv; warnings.append(f"new project '{pv}' created")
        if b.get("plan_line") not in (None, ""):
            pid = tx.get("project_id")
            tx["plan_line_id"] = resolve_plan_line(db, b["plan_line"], pid)
        if b.get("person"):
            tx["member"] = str(b["person"]).strip()
        if b.get("tags"):
            tx["tags"] = b["tags"]
        if kind == "expense" and b.get("reimbursable") is not None:
            tx["reimbursable"] = 1 if (b["reimbursable"] is True or _truthy(b["reimbursable"])) else 0
        # tax / tip: only when given (or printed on the receipt); "18%" means a percentage of the subtotal
        tax, tip = _pct(b.get("tax")), _pct(b.get("tip"))
        if tax is None and tip is None and saved and b.get("amount") is None:
            d = ingest.receipt_draft(db, parsed, acct["id"])["draft"]
            tx["tax_amount"], tx["tip_amount"] = d.get("tax_amount"), d.get("tip_amount")
        elif kind != "transfer":
            res = ParseResult(text=""); res.amount = amount
            apply_tax_tip(res, {k: v for k, v in (("tax", tax), ("tip", tip)) if v is not None})
            tx["tax_amount"], tx["tip_amount"], tx["tax_rate"] = res.tax_amount, res.tip_amount, res.tax_rate
            if b.get("tax_rate") not in (None, ""):
                tx["tax_rate"] = _num(str(b["tax_rate"]).rstrip("%"), "tax_rate")
        t = ledger.create_tx(db, tx)
    except (AgentError, ledger.ValidationError):
        for r in saved:
            receipts.delete(db, r["id"])
        raise
    linked = rids + [r["id"] for r in saved]
    if linked:
        t["receipts"] = receipts.link(db, linked, t["id"])
    # a reimbursement can pay back reimbursable expenses right away: expense_ids = [12, 15] or "12,15" (each link gets
    # the default amount: the smaller of the expense's outstanding and what is still unallocated)
    eids = b.get("expense_ids")
    if kind == "reimbursement" and eids not in (None, "", []):
        from . import reimburse
        ids = eids if isinstance(eids, list) else [x for x in re.split(r"[,\s]+", str(eids)) if x]
        for x in ids:
            try:
                reimburse.link(db, {"reimbursement_id": t["id"], "expense_id": int(str(x).lstrip("#"))})
            except (ValueError, ledger.ValidationError) as e:
                warnings.append(f"not linked to expense {x}: {e}")
        t = ledger.get_tx(db, t["id"])
    return {"ok": True, "id": t["id"], "message": message(t, acct), "transaction": t, "warnings": warnings}


def message(t: dict, acct: dict) -> str:
    amt = ingest.fmt_money(t.get("orig_amount") or t["amount"], t.get("orig_currency") or t.get("currency") or acct["currency"])
    extra = []
    if t.get("tax_amount") is not None:
        extra.append(("税 " if _zh() else "tax ") + ingest.fmt_money(t["tax_amount"], t.get("currency") or acct["currency"]))
    if t.get("tip_amount") is not None:
        extra.append(("小费 " if _zh() else "tip ") + ingest.fmt_money(t["tip_amount"], t.get("currency") or acct["currency"]))
    who = t.get("merchant_name") or ""
    tail = (" (" + ", ".join(extra) + ")") if extra else ""
    review = t.get("status") == "review"
    if _zh():
        return f"{'待确认' if review else '已记'}: {who + ' ' if who else ''}{amt}{tail} · {acct['name']} (#{t['id']})"
    return f"{'To review' if review else 'Recorded'} {t['kind']}: {who + ' ' if who else ''}{amt}{tail} · {acct['name']} (#{t['id']})"
