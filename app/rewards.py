"""Miles & points 里程/积分: loyalty programs (airline miles, hotel points, card points) with a balance in their own unit,
an expiry date and an optional value per point.

Like a credit, a program is a NOTE: it is never a transaction, so balances, net worth and every stat ignore it.
Fields: name (United MileagePlus …), kind (airline | hotel | card | other), unit (miles / points / …), member no.,
balance + as-of date, expiry date, value_per_point (in value_currency, e.g. 0.012 = 1.2¢; optional, gives an
estimated value), linked account (optional, e.g. the co-branded card), note. Every balance change is kept in
reward_history. Status: active | closed.
Reminders: every active program with points and an expiry date has one 'reward' reminder (reminders.reward_id,
unique), due on the expiry date and shown from `credit_expiry_lead_months` (same setting as credits, default 6) months
before. Moving the expiry date (activity usually extends it) moves the reminder and brings back one already done.
"""
from __future__ import annotations

from datetime import date

from .credits import _day, is_expired, is_expiring, lead_days_for, lead_months, today, window_start
from .db import Database, now_iso
from .ledger import ValidationError

KINDS = ("airline", "hotel", "card", "other")
KIND_ICONS = {"airline": "✈️", "hotel": "🏨", "card": "💳", "other": "⭐"}
DEFAULT_UNIT = {"airline": "miles", "hotel": "points", "card": "points", "other": "points"}
STATUSES = ("active", "closed")
FIELDS = ("name", "kind", "unit", "member_no", "balance", "balance_date", "expiry_date", "value_per_point",
          "value_currency", "account_id", "note")


class NotFound(KeyError):
    pass


def _num(v, what: str, allow_negative: bool = False) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be a number")
    if x < 0 and not allow_negative:
        raise ValidationError(f"{what} can't be negative")
    return x


# ------------------------------------------------------------------ reading
SELECT = """SELECT r.*, a.name account_name, a.last4 account_last4 FROM rewards r LEFT JOIN accounts a ON a.id=r.account_id"""


def _decorate(db: Database, r: dict, on: date, months: int) -> dict:
    r = dict(r)
    exp = r.get("expiry_date")
    has = (r.get("balance") or 0) > 0
    r["closed"] = r.get("status") == "closed"
    r["days_left"] = (date.fromisoformat(exp) - on).days if exp else None
    r["expired"] = bool(not r["closed"] and has and is_expired(exp, on))
    r["expiring_soon"] = bool(not r["closed"] and has and is_expiring(exp, on, months))
    r["window_start"] = window_start(exp, months).isoformat() if exp else None
    r["icon"] = KIND_ICONS.get(r.get("kind"), "⭐")
    vpp = r.get("value_per_point")
    r["est_value"] = round((r.get("balance") or 0) * vpp, 2) if vpp else None
    r["label"] = r.get("name") or "Rewards"
    return r


def get(db: Database, rid: int, with_history: bool = True) -> dict:
    r = db.one(SELECT + " WHERE r.id=?", (rid,))
    if not r:
        raise NotFound(rid)
    out = _decorate(db, r, today(db), lead_months(db))
    if with_history:
        out["history"] = db.all("SELECT * FROM reward_history WHERE reward_id=? ORDER BY date DESC, id DESC LIMIT 50", (rid,))
    return out


def list_rewards(db: Database, q: dict | None = None, on: date | None = None) -> dict:
    """q: status=active (default) | closed | all, kind=."""
    q = q or {}
    on = on or today(db)
    months = lead_months(db)
    rows = db.all(SELECT + " ORDER BY r.status, r.expiry_date IS NULL, r.expiry_date, r.name")
    items = [_decorate(db, r, on, months) for r in rows]
    counts = {"active": sum(1 for r in items if not r["closed"]), "closed": sum(1 for r in items if r["closed"])}
    want = str(q.get("status") or "active")
    if want != "all":
        items = [r for r in items if (r["closed"] if want == "closed" else not r["closed"])]
    if q.get("kind"):
        items = [r for r in items if r.get("kind") == q["kind"]]
    value: dict[str, float] = {}
    for r in items:
        if r["est_value"] and not r["closed"] and not r["expired"]:
            cur = r.get("value_currency") or db.get_setting("base_currency", "USD")
            value[cur] = round(value.get(cur, 0) + r["est_value"], 2)
    return {"items": items, "status_counts": counts, "lead_months": months, "today": on.isoformat(),
            "est_value": [{"currency": k, "amount": v} for k, v in sorted(value.items())],
            "expiring_count": sum(1 for r in items if r["expiring_soon"]),
            "expired_count": sum(1 for r in items if r["expired"])}


def attention(db: Database) -> tuple[int, int]:
    """(expiring soon, expired with points left) over active programs: for the menu badge."""
    d = list_rewards(db, {"status": "active"})
    return d["expiring_count"], d["expired_count"]


# ------------------------------------------------------------------ writing
def _clean(db: Database, body: dict, ex: dict | None = None) -> dict:
    d = dict(ex or {})
    d.update({k: body[k] for k in FIELDS if k in body})
    d["name"] = str(d.get("name") or "").strip()[:120]
    if not d["name"]:
        raise ValidationError("Enter the program name (e.g. United MileagePlus)")
    kind = str(d.get("kind") or "airline").strip().lower()
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {', '.join(KINDS)}")
    d["kind"] = kind
    d["unit"] = str(d.get("unit") or "").strip()[:20] or DEFAULT_UNIT[kind]
    d["member_no"] = str(d.get("member_no") or "").strip()[:60]
    bal = _num(d.get("balance"), "Balance")
    d["balance"] = bal or 0.0
    d["balance_date"] = _day(d.get("balance_date"), "Balance date") or today(db).isoformat()
    d["expiry_date"] = _day(d.get("expiry_date"), "Expiry date")
    if "value_cents" in body:  # the form takes cents per point (1.2 = 1.2¢); the API also accepts value_per_point
        v = _num(body["value_cents"], "Value per point")
        d["value_per_point"] = round(v / 100, 6) if v else None
    else:
        d["value_per_point"] = _num(d.get("value_per_point"), "Value per point") or None
    d["value_currency"] = (str(d.get("value_currency") or "").strip().upper()[:3]
                           or db.get_setting("base_currency", "USD") or "USD") if d["value_per_point"] else None
    if d.get("account_id") in (None, "", 0, "0"):
        d["account_id"] = None
    else:
        try:
            d["account_id"] = int(d["account_id"])
        except (TypeError, ValueError):
            raise ValidationError("account_id must be a number")
        if not db.one("SELECT 1 x FROM accounts WHERE id=?", (d["account_id"],)):
            raise ValidationError("Unknown account")
    d["note"] = str(d.get("note") or "").strip()[:500]
    return {k: d[k] for k in FIELDS}


def _history(con, rid: int, when: str, bal: float, change: float | None, exp: str | None, note: str = "") -> None:
    con.execute("INSERT INTO reward_history(reward_id, date, balance, change, expiry_date, note, created_at) VALUES(?,?,?,?,?,?,?)",
                (rid, when, bal, change, exp, note[:500], now_iso()))


def create(db: Database, body: dict) -> dict:
    row = _clean(db, body or {})
    row["status"] = "active"
    row["created_at"] = row["updated_at"] = now_iso()
    with db.tx() as con:
        rid = con.execute(f"INSERT INTO rewards({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values())).lastrowid
        _history(con, rid, row["balance_date"], row["balance"], None, row["expiry_date"], "Added")
    sync_reminders(db)
    return get(db, rid)


def update(db: Database, rid: int, body: dict) -> dict:
    ex = db.one("SELECT * FROM rewards WHERE id=?", (rid,))
    if not ex:
        raise NotFound(rid)
    row = _clean(db, body or {}, ex)
    row["updated_at"] = now_iso()
    with db.tx() as con:
        con.execute(f"UPDATE rewards SET {','.join(k + '=?' for k in row)} WHERE id=?", list(row.values()) + [rid])
        if abs((row["balance"] or 0) - (ex["balance"] or 0)) > 1e-9 or row["expiry_date"] != ex["expiry_date"]:
            _history(con, rid, row["balance_date"], row["balance"], round(row["balance"] - (ex["balance"] or 0), 4),
                     row["expiry_date"], "Edited")
    sync_reminders(db)
    return get(db, rid)


def set_balance(db: Database, rid: int, body: dict) -> dict:
    """Update the balance: {balance} (new total) or {change} (+earned / -used), date?, expiry_date? (blank = keep),
    note?. Logged in reward_history."""
    ex = db.one("SELECT * FROM rewards WHERE id=?", (rid,))
    if not ex:
        raise NotFound(rid)
    body = body or {}
    bal, chg = _num(body.get("balance"), "Balance"), _num(body.get("change"), "Change", allow_negative=True)
    if bal is None and chg is None:
        raise ValidationError("Enter the new balance or the change")
    new = bal if bal is not None else (ex["balance"] or 0) + chg
    if new < 0:
        raise ValidationError(f"That would leave {new:,.0f} {ex['unit']}: the balance can't go below zero")
    when = _day(body.get("date"), "Date") or today(db).isoformat()
    exp = _day(body.get("expiry_date"), "Expiry date") if body.get("expiry_date") not in (None, "") else ex["expiry_date"]
    with db.tx() as con:
        con.execute("UPDATE rewards SET balance=?, balance_date=?, expiry_date=?, updated_at=? WHERE id=?",
                    (new, when, exp, now_iso(), rid))
        _history(con, rid, when, new, round(new - (ex["balance"] or 0), 4), exp, str(body.get("note") or "").strip())
    sync_reminders(db)
    return get(db, rid)


def undo_history(db: Database, rid: int, hid: int) -> dict:
    """Remove the latest balance update and go back to the one before it."""
    rows = db.all("SELECT * FROM reward_history WHERE reward_id=? ORDER BY date DESC, id DESC LIMIT 2", (rid,))
    if not rows or rows[0]["id"] != hid:
        raise ValidationError("Only the latest balance update can be undone")
    if len(rows) < 2:
        raise ValidationError("This is the first balance: edit the program instead")
    prev = rows[1]
    with db.tx() as con:
        con.execute("DELETE FROM reward_history WHERE id=?", (hid,))
        con.execute("UPDATE rewards SET balance=?, balance_date=?, expiry_date=?, updated_at=? WHERE id=?",
                    (prev["balance"], prev["date"], prev["expiry_date"], now_iso(), rid))
    sync_reminders(db)
    return get(db, rid)


def delete(db: Database, rid: int) -> dict:
    if not db.one("SELECT 1 x FROM rewards WHERE id=?", (rid,)):
        raise NotFound(rid)
    with db.tx() as con:
        con.execute("DELETE FROM reward_history WHERE reward_id=?", (rid,))
        con.execute("DELETE FROM rewards WHERE id=?", (rid,))
    sync_reminders(db)
    return {"ok": True}


def set_status(db: Database, rid: int, status: str) -> dict:
    if status not in STATUSES:
        raise ValidationError(f"status must be one of {', '.join(STATUSES)}")
    if not db.one("SELECT 1 x FROM rewards WHERE id=?", (rid,)):
        raise NotFound(rid)
    with db.tx() as con:
        con.execute("UPDATE rewards SET status=?, updated_at=? WHERE id=?", (status, now_iso(), rid))
    sync_reminders(db)
    return get(db, rid)


# ------------------------------------------------------------------ reminders (kind 'reward')
def _live(r: dict | None) -> bool:
    return bool(r and r["status"] == "active" and (r["balance"] or 0) > 0 and r["expiry_date"])


def sync_reminders(db: Database, on: date | None = None) -> int:
    """One 'reward' reminder per live program (active, points left, expiry date), due on the expiry date.
    Closes it when the program is closed / emptied / has no expiry, dismisses it when the program is deleted, and
    re-opens it (with the new date) when the expiry date moves, e.g. after activity extended it."""
    n = 0
    progs = {r["id"]: r for r in db.all("SELECT * FROM rewards")}
    rems = {r["reward_id"]: r for r in db.all("SELECT * FROM reminders WHERE kind='reward'")}
    with db.tx() as con:
        for rid, rem in rems.items():
            p = progs.get(rid)
            if not p and rem["status"] == "open":
                con.execute("UPDATE reminders SET status='dismissed', done_reason='reward_deleted', done_at=?, updated_at=? WHERE id=?",
                            (now_iso(), now_iso(), rem["id"])); n += 1
            elif p and not _live(p) and rem["status"] == "open":
                con.execute("UPDATE reminders SET status='done', done_reason='reward_closed', done_at=?, updated_at=? WHERE id=?",
                            (now_iso(), now_iso(), rem["id"])); n += 1
        for rid, p in progs.items():
            if not _live(p):
                continue
            rem = rems.get(rid)
            if not rem:
                con.execute("""INSERT INTO reminders(kind, reward_id, account_id, title, due_date, amount, currency, status,
                    created_at, updated_at) VALUES('reward',?,?,?,?,?,NULL,'open',?,?)""",
                            (rid, p["account_id"], p["name"], p["expiry_date"], p["balance"], now_iso(), now_iso())); n += 1
                continue
            moved = rem["due_date"] != p["expiry_date"]
            if rem["status"] == "open":
                if moved or rem["title"] != p["name"] or rem["amount"] != p["balance"]:
                    con.execute("UPDATE reminders SET due_date=?, title=?, amount=?, account_id=?, notified_at=CASE WHEN ? THEN NULL ELSE notified_at END, "
                                "updated_at=? WHERE id=?", (p["expiry_date"], p["name"], p["balance"], p["account_id"], moved, now_iso(), rem["id"]))
            elif moved or (rem["done_reason"] or "").startswith("reward_"):
                con.execute("UPDATE reminders SET status='open', done_reason=NULL, done_at=NULL, notified_at=NULL, due_date=?, title=?, "
                            "amount=?, account_id=?, updated_at=? WHERE id=?",
                            (p["expiry_date"], p["name"], p["balance"], p["account_id"], now_iso(), rem["id"])); n += 1
    return n


def reminder_info(db: Database, r: dict) -> dict:
    p = db.one("SELECT * FROM rewards WHERE id=?", (r.get("reward_id"),))
    if not p:
        return {"reward_state": "deleted"}
    d = _decorate(db, p, today(db), lead_months(db))
    return {"reward_state": "expired" if d["expired"] else "closed" if d["closed"] else "active", "reward_balance": p["balance"],
            "reward_unit": p["unit"], "reward_kind": p["kind"], "reward_icon": d["icon"], "member_no": p["member_no"],
            "est_value": d["est_value"], "value_currency": p["value_currency"], "title": p["name"],
            "statement_amount": None, "remaining": None, "paid": None}


def reminder_lead_days(db: Database, r: dict) -> int:
    return lead_days_for(r["due_date"], lead_months(db)) if r.get("due_date") else 0


def reminder_text(r: dict, zh: bool, tw: bool = False) -> str:
    """One line for a reward reminder (Telegram / email / iPhone notify)."""
    bal = f"{(r.get('reward_balance') or 0):,.0f} {r.get('reward_unit') or ''}".strip()
    past = r.get("state") == "overdue" or r.get("reward_state") == "expired"
    if zh:
        s = f"{r.get('title') or ''} · {bal} {'已于' if past else '将于'} {r.get('due_date')} 过期"
        return s.replace("已于", "已於").replace("将于", "將於").replace("过期", "過期") if tw else s
    return f"{r.get('title') or ''} · {bal} {'expired' if past else 'expire'} {r.get('due_date')}"
