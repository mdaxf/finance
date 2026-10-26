"""Expiring credits 抵用金/余额券: credits held on an account that came from a refund, a cancellation or a bonus and
expire on a date (airline / travel credits, store credit, sign-up or promo bonuses, …).

A credit is a NOTE, like a card hold: it is never a transaction, so balances, net worth and every stat ignore it.
Fields: account (optional: issuer-only credits), source (refund | cancel | bonus), amount, remaining, currency, issued date, expiry date, issuer /
title, reference, note. Using it ("mark used" / "partially used") lowers `remaining` and is logged in credit_uses.

Stored status: open | used | void (closed by hand: forfeited, written off). Derived `state`:
  used     remaining is 0 (or status used)
  void     closed by hand
  expired  still has money left but the expiry date has passed (flagged everywhere: list, account, reminders)
  partial  some of it used
  active   untouched
Reminders: every open credit with money left and an expiry date has one 'credit' reminder (reminders.credit_id,
unique), due on the expiry date. It shows from `credit_expiry_lead_months` (setting, default 6) months before it
(lead window), is overdue (= expired) after it, and completes by itself once the credit is used up or voided.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .db import Database, now_iso
from .ledger import ValidationError

SOURCES = ("refund", "cancel", "bonus")
SOURCE_LABELS = {"refund": ("Refund", "退款"), "cancel": ("Cancellation", "取消"), "bonus": ("Bonus", "奖励")}
STATUSES = ("open", "used", "void")
STATES = ("active", "partial", "expired", "used", "void")
LEAD_MONTHS_DEFAULT = 6
EPS = 0.005


class NotFound(KeyError):
    pass


# ------------------------------------------------------------------ helpers
def _r2(v) -> float:
    return round(v or 0.0, 2) + 0.0


def _num(v, what: str, positive: bool = True, allow_zero: bool = False) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = round(float(v), 2)
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be a number")
    if positive and (x < 0 or (x == 0 and not allow_zero)):
        raise ValidationError(f"{what} must be greater than zero")
    return x


def _day(v, what: str = "date") -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, date):
        return v.isoformat()
    try:
        return datetime.strptime(str(v).strip().replace("T", " ")[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValidationError(f"{what} must be YYYY-MM-DD")


def today(db: Database) -> date:
    from .schedules import local_today
    return local_today(db)


def add_months(d: date, n: int) -> date:
    """d shifted by n months, clamped to the month end (Aug 31 - 6 months -> Feb 28/29)."""
    k = d.year * 12 + (d.month - 1) + n
    y, m = k // 12, k % 12 + 1
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return date(y, m, min(d.day, (nxt - timedelta(days=1)).day))


def lead_months(db: Database) -> int:
    try:
        return max(0, min(60, int(db.get_setting("credit_expiry_lead_months") or LEAD_MONTHS_DEFAULT)))
    except (TypeError, ValueError):
        return LEAD_MONTHS_DEFAULT


def window_start(expiry: date | str, months: int) -> date:
    """First day a credit expiring on `expiry` is 'expiring soon' (reminder window of `months` months)."""
    exp = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
    return add_months(exp, -int(months))


def lead_days_for(expiry: date | str, months: int) -> int:
    """The reminder lead window in days for this expiry (so the Reminder Center's lead logic applies unchanged)."""
    exp = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
    return (exp - window_start(exp, months)).days


def is_expiring(expiry, on: date, months: int) -> bool:
    """Inside the window and not expired yet (the expiry day itself still counts as usable)."""
    if not expiry:
        return False
    exp = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
    return window_start(exp, months) <= on <= exp


def is_expired(expiry, on: date) -> bool:
    if not expiry:
        return False
    exp = expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)[:10])
    return on > exp


def state_of(c: dict, on: date) -> str:
    if c.get("status") == "void":
        return "void"
    if c.get("status") == "used" or (c.get("remaining") or 0) < EPS:
        return "used"
    if is_expired(c.get("expiry_date"), on):
        return "expired"
    if (c.get("remaining") or 0) < (c.get("amount") or 0) - EPS:
        return "partial"
    return "active"


# ------------------------------------------------------------------ reading
SELECT = """SELECT c.*, a.name account_name, a.type account_type, a.last4 account_last4, a.currency account_currency
  FROM credits c LEFT JOIN accounts a ON a.id=c.account_id"""


def _decorate(db: Database, c: dict, on: date, months: int) -> dict:
    c = dict(c)
    c["state"] = state_of(c, on)
    c["used_amount"] = _r2((c.get("amount") or 0) - (c.get("remaining") or 0))
    c["expired"] = c["state"] == "expired"
    c["closed"] = c["state"] in ("used", "void")
    exp = c.get("expiry_date")
    c["days_left"] = (date.fromisoformat(exp) - on).days if exp else None
    c["expiring_soon"] = bool(not c["closed"] and is_expiring(exp, on, months))
    c["window_start"] = window_start(exp, months).isoformat() if exp else None
    lab = SOURCE_LABELS.get(c.get("source"), (c.get("source") or "", ""))
    c["source_label"], c["source_label_zh"] = lab
    c["label"] = (c.get("title") or "").strip() or f"{lab[0]} credit" + (f" · {c['account_name']}" if c.get("account_name") else "")
    return c


def get(db: Database, cid: int, with_uses: bool = True) -> dict:
    c = db.one(SELECT + " WHERE c.id=?", (cid,))
    if not c:
        raise NotFound(cid)
    out = _decorate(db, c, today(db), lead_months(db))
    if with_uses:
        out["uses"] = db.all("SELECT * FROM credit_uses WHERE credit_id=? ORDER BY date, id", (cid,))
    return out


def list_credits(db: Database, q: dict | None = None, on: date | None = None) -> dict:
    """q: state=active,partial,expired (default = everything still holding money) | used | void | all,
    account_id=, source=, expiring=1 (only inside the reminder window), months= (window override)."""
    q = q or {}
    on = on or today(db)
    months = int(q["months"]) if str(q.get("months") or "").isdigit() else lead_months(db)
    rows = db.all(SELECT + " ORDER BY c.expiry_date IS NULL, c.expiry_date, c.id")
    items = [_decorate(db, r, on, months) for r in rows]
    counts = {k: sum(1 for c in items if c["state"] == k) for k in STATES}
    want = str(q.get("state") or "active,partial,expired")
    if want != "all":
        ws = {s.strip() for s in want.split(",") if s.strip()}
        items = [c for c in items if c["state"] in ws]
    if q.get("account_id"):
        items = [c for c in items if str(c.get("account_id")) == str(q["account_id"])]
    if q.get("source"):
        items = [c for c in items if c.get("source") == q["source"]]
    if str(q.get("expiring") or "") in ("1", "true"):
        items = [c for c in items if c["expiring_soon"]]
    totals: dict[str, float] = {}
    for c in items:
        if not c["closed"] and not c["expired"]:
            totals[c["currency"]] = _r2(totals.get(c["currency"], 0) + c["remaining"])
    return {"items": items, "state_counts": counts, "lead_months": months, "today": on.isoformat(),
            "available": [{"currency": k, "amount": v} for k, v in sorted(totals.items())],
            "expiring_count": sum(1 for c in items if c["expiring_soon"]),
            "expired_count": sum(1 for c in items if c["expired"])}


def expiring(db: Database, months: int | None = None, on: date | None = None) -> list[dict]:
    """Open credits with money left that expire within `months` (default: the setting), soonest first."""
    q = {"state": "active,partial", "expiring": "1"}
    if months is not None:
        q["months"] = str(int(months))
    return list_credits(db, q, on)["items"]


# ------------------------------------------------------------------ writing
FIELDS = ("account_id", "source", "amount", "remaining", "currency", "issued_date", "expiry_date", "title", "reference", "note")


def _clean(db: Database, body: dict, ex: dict | None = None) -> dict:
    d = dict(ex or {})
    d.update({k: body[k] for k in FIELDS if k in body})
    acct = None  # optional: a credit can belong to the issuer only (airline / hotel / store credit not tied to a card)
    if d.get("account_id") in (None, "", 0, "0"):
        d["account_id"] = None
    else:
        try:
            d["account_id"] = int(d["account_id"])
        except (TypeError, ValueError):
            raise ValidationError("account_id must be a number")
        acct = db.one("SELECT id, currency FROM accounts WHERE id=?", (d["account_id"],))
        if not acct:
            raise ValidationError("Unknown account")
    if not acct and not str(d.get("title") or "").strip():
        raise ValidationError("Enter who issued the credit (e.g. Delta travel credit), or choose an account")
    src = str(d.get("source") or "").strip().lower()
    src = {"cancellation": "cancel", "cancelled": "cancel", "canceled": "cancel", "promo": "bonus"}.get(src, src)
    if src not in SOURCES:
        raise ValidationError(f"source must be one of {', '.join(SOURCES)}")
    d["source"] = src
    amt = _num(d.get("amount"), "Amount")
    if amt is None:
        raise ValidationError("Amount is required")
    d["amount"] = amt
    if "remaining" in body or ex is None:
        rem = _num(d.get("remaining"), "Remaining balance", allow_zero=True)
        d["remaining"] = amt if rem is None else rem
    elif ex and "amount" in body and ex.get("remaining") is not None:
        # the amount was corrected: keep what was already used
        d["remaining"] = _r2(max(amt - ((ex.get("amount") or 0) - (ex.get("remaining") or 0)), 0))
    if d["remaining"] > amt + EPS:
        raise ValidationError("Remaining balance can't be more than the credit amount")
    d["currency"] = (str(d.get("currency") or "").strip().upper()
                     or (acct["currency"] if acct else db.get_setting("base_currency", "USD") or "USD"))
    d["issued_date"] = _day(d.get("issued_date"), "Issued date") or today(db).isoformat()
    d["expiry_date"] = _day(d.get("expiry_date"), "Expiry date")
    if not d["expiry_date"]:
        raise ValidationError("Expiry date is required")
    if d["expiry_date"] < d["issued_date"]:
        raise ValidationError("Expiry date must be on or after the issued date")
    for k in ("title", "reference", "note"):
        d[k] = str(d.get(k) or "").strip()[:500]
    return {k: d[k] for k in FIELDS}


def create(db: Database, body: dict) -> dict:
    row = _clean(db, body or {})
    row["status"] = "used" if row["remaining"] < EPS else "open"
    row["created_at"] = row["updated_at"] = now_iso()
    with db.tx() as con:
        cid = con.execute(f"INSERT INTO credits({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values())).lastrowid
    _sync_reminders(db)
    return get(db, cid)


def update(db: Database, cid: int, body: dict) -> dict:
    ex = db.one("SELECT * FROM credits WHERE id=?", (cid,))
    if not ex:
        raise NotFound(cid)
    row = _clean(db, body or {}, ex)
    if ex["status"] != "void":
        row["status"] = "used" if row["remaining"] < EPS else "open"
    row["updated_at"] = now_iso()
    with db.tx() as con:
        con.execute(f"UPDATE credits SET {','.join(k + '=?' for k in row)} WHERE id=?", list(row.values()) + [cid])
    _sync_reminders(db)
    return get(db, cid)


def delete(db: Database, cid: int) -> dict:
    if not db.one("SELECT 1 x FROM credits WHERE id=?", (cid,)):
        raise NotFound(cid)
    with db.tx() as con:
        con.execute("DELETE FROM credit_uses WHERE credit_id=?", (cid,))
        con.execute("DELETE FROM credits WHERE id=?", (cid,))
    _sync_reminders(db)
    return {"ok": True}


def use(db: Database, cid: int, body: dict | None = None) -> dict:
    """Mark used: {amount?} (blank = everything left -> used; less = partially used), {date?, note?, transaction_id?}.
    Using an expired credit is refused unless {force: true} (some merchants honour it anyway)."""
    body = body or {}
    c = get(db, cid, with_uses=False)
    if c["state"] == "void":
        raise ValidationError("This credit was closed. Reopen it first")
    if c["state"] == "used":
        raise ValidationError("Nothing left on this credit")
    if c["state"] == "expired" and not body.get("force"):
        raise ValidationError(f"This credit expired on {c['expiry_date']}. Send force=true to record a use anyway")
    amt = _num(body.get("amount"), "Amount used")
    amt = c["remaining"] if amt is None else amt
    if amt > c["remaining"] + EPS:
        raise ValidationError(f"Only {c['remaining']:,.2f} {c['currency']} is left on this credit")
    rem = _r2(max(c["remaining"] - amt, 0))
    when = _day(body.get("date"), "Date") or today(db).isoformat()
    tid = body.get("transaction_id")
    with db.tx() as con:
        con.execute("INSERT INTO credit_uses(credit_id, amount, date, note, transaction_id, created_at) VALUES(?,?,?,?,?,?)",
                    (cid, amt, when, str(body.get("note") or "").strip()[:500], int(tid) if tid else None, now_iso()))
        con.execute("UPDATE credits SET remaining=?, status=?, updated_at=? WHERE id=?",
                    (rem, "used" if rem < EPS else "open", now_iso(), cid))
    _sync_reminders(db)
    return get(db, cid)


def undo_use(db: Database, cid: int, use_id: int) -> dict:
    u = db.one("SELECT * FROM credit_uses WHERE id=? AND credit_id=?", (use_id, cid))
    if not u:
        raise NotFound(use_id)
    c = db.one("SELECT * FROM credits WHERE id=?", (cid,))
    rem = _r2(min((c["remaining"] or 0) + u["amount"], c["amount"]))
    with db.tx() as con:
        con.execute("DELETE FROM credit_uses WHERE id=?", (use_id,))
        con.execute("UPDATE credits SET remaining=?, status=CASE WHEN status='void' THEN 'void' ELSE 'open' END, updated_at=? WHERE id=?",
                    (rem, now_iso(), cid))
    _sync_reminders(db)
    return get(db, cid)


def close(db: Database, cid: int, note: str | None = None) -> dict:
    """Void / write off what is left (forfeited, expired and given up, not wanted)."""
    c = db.one("SELECT * FROM credits WHERE id=?", (cid,))
    if not c:
        raise NotFound(cid)
    with db.tx() as con:
        con.execute("UPDATE credits SET status='void', closed_at=?, note=CASE WHEN ?<>'' THEN TRIM(COALESCE(note,'') || ' ' || ?) ELSE note END, "
                    "updated_at=? WHERE id=?", (now_iso(), note or "", note or "", now_iso(), cid))
    _sync_reminders(db)
    return get(db, cid)


def reopen(db: Database, cid: int) -> dict:
    c = db.one("SELECT * FROM credits WHERE id=?", (cid,))
    if not c:
        raise NotFound(cid)
    with db.tx() as con:
        con.execute("UPDATE credits SET status=?, closed_at=NULL, updated_at=? WHERE id=?",
                    ("used" if (c["remaining"] or 0) < EPS else "open", now_iso(), cid))
    _sync_reminders(db)
    return get(db, cid)


def _sync_reminders(db: Database) -> None:
    from . import reminders
    reminders.sync_credits(db)


def account_summary(db: Database, on: date | None = None) -> dict[int, dict]:
    """Per account: open credit left (not expired), how many expire soon and how many are expired with money left."""
    on = on or today(db)
    months = lead_months(db)
    out: dict[int, dict] = {}
    for r in db.all("SELECT * FROM credits WHERE status='open' AND remaining>0"):
        if r["account_id"] is None:
            continue
        c = _decorate(db, r, on, months)
        s = out.setdefault(r["account_id"], {"credits_available": {}, "credits_expiring": 0, "credits_expired": 0})
        if c["expired"]:
            s["credits_expired"] += 1
            continue
        s["credits_available"][c["currency"]] = _r2(s["credits_available"].get(c["currency"], 0) + c["remaining"])
        s["credits_expiring"] += 1 if c["expiring_soon"] else 0
    return out
