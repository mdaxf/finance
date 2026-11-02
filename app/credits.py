"""Expiring credits 抵用金/余额券: credits held on an account that came from a refund, a cancellation or a bonus and
expire on a date (airline / travel credits, store credit, sign-up or promo bonuses, …).

Ledger (per credit, `ledger`, on by default for new credits): the credit's value sits in a holding account (the
credit's own account, else the system 'Credits 抵用金' account) and every step is a transaction linked to the credit:
  issue     income (bonus, category default Cashback & Rewards) or refund (refund / cancel, the original spending's
            category) for the amount. issue_mode: post (created and kept in step: issue_tx_id, issue_auto=1) | link (a
            transaction already recorded, e.g. the reimbursement of a cancelled flight) | balance (already in the
            account's balance, e.g. its opening balance) | none
  use       an expense from the holding account (created: credit_uses.auto_tx=1), or a link to a transaction already
            recorded on it (a card statement credit offsetting a charge on that card)
  close     what is forfeited comes off the issue transaction (post), else a write-off expense (writeoff_tx_id)
With `ledger` off a credit is a NOTE, like a card hold: never a transaction, so balances and stats ignore it.
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
ISSUE_MODES = ("post", "link", "balance", "none")
CREDITS_NAME = "Credits 抵用金"
BONUS_CATEGORY = "Cashback & Rewards"
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
SELECT = """SELECT c.*, a.name account_name, a.type account_type, a.last4 account_last4, a.currency account_currency,
  cat.name category_name, wc.name writeoff_category_name
  FROM credits c LEFT JOIN accounts a ON a.id=c.account_id LEFT JOIN categories cat ON cat.id=c.category_id
  LEFT JOIN categories wc ON wc.id=c.writeoff_category_id"""


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
    c["ledger"] = 1 if c.get("ledger") else 0
    return c


def get(db: Database, cid: int, with_uses: bool = True) -> dict:
    c = db.one(SELECT + " WHERE c.id=?", (cid,))
    if not c:
        raise NotFound(cid)
    out = _decorate(db, c, today(db), lead_months(db))
    if with_uses:
        out["uses"] = db.all("""SELECT u.*, t.date tx_date, t.amount tx_amount, t.currency tx_currency, t.kind tx_kind,
            a.name tx_account_name, cat.name tx_category_name FROM credit_uses u LEFT JOIN transactions t ON t.id=u.transaction_id
            LEFT JOIN accounts a ON a.id=t.account_id LEFT JOIN categories cat ON cat.id=t.category_id
            WHERE u.credit_id=? ORDER BY u.date, u.id""", (cid,))
        txs = {}
        for k in ("issue_tx_id", "writeoff_tx_id"):
            if out.get(k):
                txs[k[:-3]] = db.one("""SELECT t.id, t.kind, t.date, t.amount, t.currency, a.name account_name, cat.name category_name
                    FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id LEFT JOIN categories cat ON cat.id=t.category_id
                    WHERE t.id=?""", (out[k],))
        out["issue_tx"], out["writeoff_tx"] = txs.get("issue_tx"), txs.get("writeoff_tx")
        if out["ledger"]:
            h = hold_account(db, out, create=False)
            out["hold_account_id"], out["hold_account_name"] = (h["id"], h["name"]) if h else (None, CREDITS_NAME)
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
FIELDS = ("account_id", "source", "amount", "remaining", "currency", "issued_date", "expiry_date", "title", "reference", "note",
          "ledger", "category_id", "issue_mode", "issue_tx_id", "writeoff_category_id")


def _flag(v) -> int:
    return 1 if v not in (None, "", 0, "0", False) and str(v).strip().lower() not in ("false", "no", "off") else 0


def _id(v, what: str) -> int | None:
    if v in (None, "", 0, "0"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be a number")


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
    # ledger 记账: new credits default to on
    d["ledger"] = _flag(d.get("ledger", 1 if ex is None else 0))
    mode = str(d.get("issue_mode") or "post").strip().lower()
    if mode not in ISSUE_MODES:
        raise ValidationError(f"issue_mode must be one of {', '.join(ISSUE_MODES)}")
    d["issue_mode"] = mode
    want = "income" if src == "bonus" else "expense"
    for k, kind, what in (("category_id", want, "Category"), ("writeoff_category_id", "expense", "Write-off category")):
        d[k] = _id(d.get(k), what)
        if d[k]:
            row = db.one("SELECT kind FROM categories WHERE id=?", (d[k],))
            if not row or row["kind"] != kind:
                if k in body:
                    raise ValidationError(f"{what} must be an {kind} category" + (" (a bonus credit is income)" if k == "category_id" and src == "bonus" else ""))
                d[k] = None  # the source changed: the old category no longer fits
    d["issue_tx_id"] = _id(d.get("issue_tx_id"), "Transaction") if mode == "link" else None
    if d["ledger"]:
        if mode == "link":
            if not d["issue_tx_id"]:
                raise ValidationError("Choose the transaction this credit came from (or let it record a new one)")
            t = db.one("SELECT id, account_id, to_account_id FROM transactions WHERE id=?", (d["issue_tx_id"],))
            if not t:
                raise ValidationError(f"Transaction #{d['issue_tx_id']} not found")
            if acct and acct["id"] not in (t["account_id"], t["to_account_id"]):
                raise ValidationError(f"Transaction #{t['id']} is not on this credit's account: link one on that account, or record a new one")
            if not acct:
                raise ValidationError("Linking a recorded transaction needs the credit's account: choose it first")
            other = db.one("SELECT id FROM credits WHERE issue_tx_id=? AND id<>?", (t["id"], (ex or {}).get("id") or 0))
            if other:
                raise ValidationError(f"Transaction #{t['id']} is already linked to credit #{other['id']}")
        if mode == "balance" and not acct:
            raise ValidationError("'Already in the balance' needs the credit's account")
    return {k: d[k] for k in FIELDS}


# ------------------------------------------------------------------ ledger 记账
def hold_account(db_or_con, c: dict, create: bool = True):
    """Where the credit's value sits: its own account, else the system 'Credits 抵用金' account in its currency."""
    con = db_or_con.connect() if isinstance(db_or_con, Database) else db_or_con
    try:
        if c.get("account_id"):
            return con.execute("SELECT * FROM accounts WHERE id=?", (c["account_id"],)).fetchone()
        cur = c.get("currency") or "USD"
        demo = 1 if c.get("is_demo") else 0
        row = con.execute("SELECT * FROM accounts WHERE system_role='credits' AND currency=? AND COALESCE(is_demo,0)=? ORDER BY id LIMIT 1",
                          (cur, demo)).fetchone()
        if row or not create:
            if row and row["archived"]:
                con.execute("UPDATE accounts SET archived=0 WHERE id=?", (row["id"],))
            return row
        from .db import ACCOUNT_TYPES
        base = (con.execute("SELECT value FROM settings WHERE key='base_currency'").fetchone() or ["USD"])[0] or "USD"
        grp = con.execute("SELECT id FROM account_groups WHERE name=?", (ACCOUNT_TYPES["other_asset"][1],)).fetchone()
        name = ("[DEMO] " if demo else "") + CREDITS_NAME + ("" if cur == base else f" ({cur})")
        aid = con.execute("""INSERT INTO accounts(group_id, name, type, currency, opening_balance, note, system_role, is_demo)
            VALUES(?,?,?,?,0,?,'credits',?)""", (grp[0] if grp else None, name, "other_asset", cur,
            "System account: unused credits not tied to one of your accounts (Credits 抵用金). Created automatically.", demo)).lastrowid
        return con.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    finally:
        if isinstance(db_or_con, Database):
            con.close()


def _label(c: dict) -> str:
    return (c.get("title") or "").strip() or f"{SOURCE_LABELS.get(c.get('source'), ('',))[0]} credit"


def _insert_tx(con, data: dict, rates) -> int:
    from .ledger import TX_FIELDS, normalize_tx
    tx = normalize_tx(con, data, rates)
    tx["updated_at"] = tx["created_at"] = now_iso()
    cols = [k for k in TX_FIELDS + ["updated_at", "created_at"] if k in tx]
    return con.execute(f"INSERT INTO transactions({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [tx[k] for k in cols]).lastrowid


def _update_tx(con, tid: int, data: dict, rates) -> bool:
    from .ledger import TX_FIELDS, normalize_tx
    ex = con.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()
    if not ex:
        return False
    ex = dict(ex)
    if "currency" in data:
        ex["orig_amount"] = ex["orig_currency"] = None
    tx = normalize_tx(con, data, rates, existing=ex)
    tx["updated_at"] = now_iso()
    cols = [k for k in TX_FIELDS + ["updated_at"] if k in tx]
    con.execute(f"UPDATE transactions SET {','.join(k + '=?' for k in cols)} WHERE id=?", [tx[k] for k in cols] + [tid])
    return True


def _drop_tx(con, tid: int | None) -> None:
    """Delete a transaction this module created (callers detach it first, so the delete trigger leaves the credit alone)."""
    if tid:
        con.execute("DELETE FROM transactions WHERE id=?", (tid,))


def _bonus_category(con) -> int | None:
    r = con.execute("SELECT id FROM categories WHERE kind='income' AND name=? ORDER BY id LIMIT 1", (BONUS_CATEGORY,)).fetchone()
    return r[0] if r else None


def _sync_ledger(con, cid: int, rates) -> None:
    """Bring the credit's transactions in step with it (idempotent): create / update / remove what it owns."""
    c = dict(con.execute("SELECT * FROM credits WHERE id=?", (cid,)).fetchone())
    uses = [dict(u) for u in con.execute("SELECT * FROM credit_uses WHERE credit_id=? ORDER BY id", (cid,))]
    if not c.get("ledger"):
        if c.get("issue_auto") and c.get("issue_tx_id"):
            con.execute("UPDATE credits SET issue_tx_id=NULL, issue_auto=0 WHERE id=?", (cid,))
            _drop_tx(con, c["issue_tx_id"])
        for u in uses:
            if u.get("auto_tx") and u.get("transaction_id"):
                con.execute("UPDATE credit_uses SET transaction_id=NULL WHERE id=?", (u["id"],))  # stays auto: back if ledger is on again
                _drop_tx(con, u["transaction_id"])
        if c.get("writeoff_tx_id"):
            con.execute("UPDATE credits SET writeoff_tx_id=NULL WHERE id=?", (cid,))
            _drop_tx(con, c["writeoff_tx_id"])
        return
    hold = hold_account(con, c)
    label = _label(c)
    forfeited = _r2(c["remaining"]) if c["status"] == "void" else 0.0
    # issue
    if c["issue_mode"] != "post" and c.get("issue_auto") and c.get("issue_tx_id"):
        con.execute("UPDATE credits SET issue_tx_id=?, issue_auto=0 WHERE id=?",
                    (None if c["issue_mode"] != "link" else c["issue_tx_id"], cid))
        _drop_tx(con, c["issue_tx_id"])
    if c["issue_mode"] == "post":
        amt = _r2(c["amount"] - forfeited)
        bonus = c["source"] == "bonus"
        data = {"kind": "income" if bonus else "refund", "date": c["issued_date"], "amount": amt, "currency": c["currency"],
                "account_id": hold["id"], "category_id": c.get("category_id") or (_bonus_category(con) if bonus else None),
                "note": f"Credit: {label}" + (f" ({forfeited:,.2f} forfeited)" if forfeited else ""), "source": "credit",
                "is_demo": c.get("is_demo") or 0}
        if (c.get("title") or "").strip():
            data["merchant"] = c["title"].strip()[:120]
        live = c.get("issue_auto") and c.get("issue_tx_id") and con.execute("SELECT 1 FROM transactions WHERE id=?", (c["issue_tx_id"],)).fetchone()
        if amt < EPS:  # all of it forfeited: nothing was really received
            if live:
                con.execute("UPDATE credits SET issue_tx_id=NULL, issue_auto=0 WHERE id=?", (cid,))
                _drop_tx(con, c["issue_tx_id"])
        elif live:
            _update_tx(con, c["issue_tx_id"], data, rates)
        else:
            tid = _insert_tx(con, data, rates)
            con.execute("UPDATE credits SET issue_tx_id=?, issue_auto=1 WHERE id=?", (tid, cid))
    # uses: each one is spending from the holding account
    for u in uses:
        if not u.get("transaction_id"):
            tid = _insert_tx(con, {"kind": "expense", "date": u["date"], "amount": u["amount"], "currency": c["currency"],
                                   "account_id": hold["id"], "note": u.get("note") or f"Paid with credit: {label}",
                                   "source": "credit", "is_demo": c.get("is_demo") or 0}, rates)
            con.execute("UPDATE credit_uses SET transaction_id=?, auto_tx=1 WHERE id=?", (tid, u["id"]))
        elif u.get("auto_tx"):
            t = con.execute("SELECT account_id FROM transactions WHERE id=?", (u["transaction_id"],)).fetchone()
            if t and t["account_id"] != hold["id"]:
                _update_tx(con, u["transaction_id"], {"account_id": hold["id"], "amount": u["amount"], "currency": c["currency"]}, rates)
    # write-off: only when the issue is not ours to shrink (linked / already in the balance)
    need = forfeited if c["issue_mode"] != "post" and forfeited >= EPS else 0.0
    if need:
        data = {"kind": "expense", "date": (c.get("closed_at") or now_iso())[:10], "amount": need, "currency": c["currency"],
                "account_id": hold["id"], "category_id": c.get("writeoff_category_id") or (c.get("category_id") if c["source"] != "bonus" else None),
                "note": f"Credit forfeited: {label}", "source": "credit", "is_demo": c.get("is_demo") or 0}
        if c.get("writeoff_tx_id") and _update_tx(con, c["writeoff_tx_id"], data, rates):
            pass
        else:
            con.execute("UPDATE credits SET writeoff_tx_id=? WHERE id=?", (_insert_tx(con, data, rates), cid))
    elif c.get("writeoff_tx_id"):
        con.execute("UPDATE credits SET writeoff_tx_id=NULL WHERE id=?", (cid,))
        _drop_tx(con, c["writeoff_tx_id"])


def _rates(db: Database):
    from .ledger import fx_rates
    return fx_rates(db)


def create(db: Database, body: dict) -> dict:
    row = _clean(db, body or {})
    row["status"] = "used" if row["remaining"] < EPS else "open"
    row["created_at"] = row["updated_at"] = now_iso()
    rates = _rates(db)
    with db.tx() as con:
        cid = con.execute(f"INSERT INTO credits({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values())).lastrowid
        _sync_ledger(con, cid, rates)
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
    rates = _rates(db)
    with db.tx() as con:
        if ex.get("issue_auto") and ex.get("issue_tx_id"):
            if row["issue_mode"] == "link":  # replace the one we recorded with the linked one
                con.execute("UPDATE credits SET issue_tx_id=NULL, issue_auto=0 WHERE id=?", (cid,))
                _drop_tx(con, ex["issue_tx_id"])
            else:
                row.pop("issue_tx_id")  # ours: the sync updates or removes it
        con.execute(f"UPDATE credits SET {','.join(k + '=?' for k in row)} WHERE id=?", list(row.values()) + [cid])
        _sync_ledger(con, cid, rates)
    _sync_reminders(db)
    return get(db, cid)


def delete(db: Database, cid: int) -> dict:
    if not db.one("SELECT 1 x FROM credits WHERE id=?", (cid,)):
        raise NotFound(cid)
    rates = _rates(db)
    with db.tx() as con:
        con.execute("UPDATE credits SET ledger=0 WHERE id=?", (cid,))
        _sync_ledger(con, cid, rates)  # removes the transactions it created (linked ones stay)
        con.execute("DELETE FROM credit_uses WHERE credit_id=?", (cid,))
        con.execute("DELETE FROM credits WHERE id=?", (cid,))
    _sync_reminders(db)
    return {"ok": True}


def use(db: Database, cid: int, body: dict | None = None) -> dict:
    """Mark used: {amount?} (blank = everything left -> used; less = partially used), {date?, note?, transaction_id?}.
    In the ledger: without transaction_id an expense is recorded from the holding account ({category_id?, merchant?});
    with one, that transaction (on the holding account) is linked instead.
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
    tid = _id(body.get("transaction_id"), "Transaction")
    note = str(body.get("note") or "").strip()[:500]
    if tid:
        t = db.one("SELECT id, account_id FROM transactions WHERE id=?", (tid,))
        if not t:
            raise ValidationError(f"Transaction #{tid} not found")
        if c["ledger"]:
            h = hold_account(db, c, create=False)
            if h and t["account_id"] != h["id"]:
                raise ValidationError(f"Transaction #{tid} is not on {h['name']}, where this credit's money is: "
                                      "link a transaction on that account, or let it record the spending")
    rates = _rates(db)
    with db.tx() as con:
        auto = 0
        if not tid and c["ledger"]:
            hold = hold_account(con, c)
            data = {"kind": "expense", "date": when, "amount": amt, "currency": c["currency"], "account_id": hold["id"],
                    "category_id": _id(body.get("category_id"), "Category"), "note": note or f"Paid with credit: {_label(c)}",
                    "source": "credit", "is_demo": c.get("is_demo") or 0}
            if str(body.get("merchant") or "").strip():
                data["merchant"] = str(body["merchant"]).strip()[:120]
            if data["category_id"] and not con.execute("SELECT 1 FROM categories WHERE id=? AND kind='expense'", (data["category_id"],)).fetchone():
                raise ValidationError("Category must be an expense category")
            tid, auto = _insert_tx(con, data, rates), 1
        con.execute("INSERT INTO credit_uses(credit_id, amount, date, note, transaction_id, auto_tx, created_at) VALUES(?,?,?,?,?,?,?)",
                    (cid, amt, when, note, tid, auto, now_iso()))
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
        if u.get("auto_tx"):
            _drop_tx(con, u.get("transaction_id"))  # the spending it recorded
        con.execute("UPDATE credits SET remaining=?, status=CASE WHEN status='void' THEN 'void' ELSE 'open' END, updated_at=? WHERE id=?",
                    (rem, now_iso(), cid))
    _sync_reminders(db)
    return get(db, cid)


def close(db: Database, cid: int, note: str | None = None, writeoff_category_id=None) -> dict:
    """Void / write off what is left (forfeited, expired and given up, not wanted). In the ledger the forfeited part
    comes off the issue transaction, or (linked / already in the balance) is a write-off expense."""
    c = db.one("SELECT * FROM credits WHERE id=?", (cid,))
    if not c:
        raise NotFound(cid)
    wc = _id(writeoff_category_id, "Write-off category")
    if wc and not db.one("SELECT 1 x FROM categories WHERE id=? AND kind='expense'", (wc,)):
        raise ValidationError("Write-off category must be an expense category")
    rates = _rates(db)
    with db.tx() as con:
        if wc:
            con.execute("UPDATE credits SET writeoff_category_id=? WHERE id=?", (wc, cid))
        con.execute("UPDATE credits SET status='void', closed_at=?, note=CASE WHEN ?<>'' THEN TRIM(COALESCE(note,'') || ' ' || ?) ELSE note END, "
                    "updated_at=? WHERE id=?", (now_iso(), note or "", note or "", now_iso(), cid))
        _sync_ledger(con, cid, rates)
    _sync_reminders(db)
    return get(db, cid)


def reopen(db: Database, cid: int) -> dict:
    c = db.one("SELECT * FROM credits WHERE id=?", (cid,))
    if not c:
        raise NotFound(cid)
    rates = _rates(db)
    with db.tx() as con:
        con.execute("UPDATE credits SET status=?, closed_at=NULL, updated_at=? WHERE id=?",
                    ("used" if (c["remaining"] or 0) < EPS else "open", now_iso(), cid))
        _sync_ledger(con, cid, rates)
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


def annotate(db: Database, rows: list[dict]) -> list[dict]:
    """Transaction rows get `credits`: [{id, title, role}] (role issue | use | writeoff) when a credit links them."""
    ids = [r["id"] for r in rows if r.get("id")]
    if not ids:
        return rows
    q = ",".join("?" * len(ids))
    found: dict[int, list] = {}
    for k in db.all(f"""SELECT c.id, c.title, c.source, c.issue_tx_id, c.writeoff_tx_id FROM credits c
                        WHERE c.issue_tx_id IN ({q}) OR c.writeoff_tx_id IN ({q})""", ids + ids):
        for role in ("issue", "writeoff"):
            if k[f"{role}_tx_id"] in ids:
                found.setdefault(k[f"{role}_tx_id"], []).append({"id": k["id"], "title": _label(k), "role": role})
    for k in db.all(f"""SELECT u.transaction_id, u.amount, c.id, c.title, c.source FROM credit_uses u JOIN credits c ON c.id=u.credit_id
                        WHERE u.transaction_id IN ({q})""", ids):
        found.setdefault(k["transaction_id"], []).append({"id": k["id"], "title": _label(k), "role": "use", "amount": k["amount"]})
    for r in rows:
        if r.get("id") in found:
            r["credits"] = found[r["id"]]
    return rows
