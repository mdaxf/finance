"""Reserves 押金/预授权: hotel / rental deposits and card holds, and everything charged, refunded or kept against them.

Two methods:
* hold 预授权 — a card pre-authorization. Memo only: it is NEVER a transaction, so balances, net worth and every
  stat/KPI ignore it. The card shows "On hold" and a lower available credit while it is open.
* deposit 押金 — money that left an account and is expected back. It is a TRANSFER from the paying account into the
  system receivable account "Deposits 押金" (accounts.system_role='deposits', one per currency, created on first
  use), so net worth is unchanged and nothing counts as an expense. The part applied to the bill or kept by the
  merchant becomes an EXPENSE from that Deposits account; the refund is a transfer back.

reserve_links(reserve_id, transaction_id, role, amount, alloc_amount, final): `amount` is in the transaction's
currency, `alloc_amount` in the reserve's currency (the same number when both match). Roles:
  deposit (transfer → Deposits) · charge (an expense against the reserve: final bill, minibar, extra charge)
  refund (transfer Deposits → an account) · apply / forfeit (expense from Deposits) · fx (FX adjustment on Deposits)
Hold:    captured = Σ charge; open → partial (some captured) → settled (final bill linked, or captured ≥ hold;
         the rest is released, no entry) | released (ended with no charge) | cancelled.
Deposit: outstanding = Σ deposit − Σ refund − Σ apply − Σ forfeit − Σ fx; open → partial → settled (outstanding 0).
A foreign-currency refund that leaves a small residual (≤ 3 % or ≤ 1 unit) posts an FX adjustment (reason
Correction, excluded from reports) so the Deposits account returns to exactly 0.
Linking never changes how a transaction counts in stats: there is no reserve stats rule.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .db import ACCOUNT_TYPES, Database, now_iso
from .ledger import TX_FIELDS, ValidationError, _norm_date, convert, fx_rates, get_or_create, normalize_tx

METHODS = ("hold", "deposit")
ROLES = ("deposit", "charge", "refund", "apply", "forfeit", "fx")
STATES = ("open", "partial", "settled", "released", "cancelled")
CLOSED = ("settled", "released", "cancelled")
EPS = 0.005
FX_TOL_PCT, FX_TOL_ABS = 0.03, 1.0
RELEASE_DAYS_DEFAULT, HOLD_DAYS_DEFAULT = 7, 30
DEPOSITS_NAME = "Deposits 押金"


class NotFound(KeyError):
    pass


def _r2(v) -> float:
    return round(v or 0.0, 2) + 0.0


def _num(v, what: str, positive: bool = True) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = round(float(v), 2)
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be a number")
    if positive and x <= 0:
        raise ValidationError(f"{what} must be greater than zero")
    return x


def _day(v, what: str = "date") -> str | None:
    if v in (None, ""):
        return None
    try:
        return datetime.strptime(str(v).strip().replace("T", " ")[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValidationError(f"{what} must be YYYY-MM-DD")


def _int_setting(db: Database, key: str, default: int) -> int:
    try:
        return max(0, min(365, int(db.get_setting(key) or default)))
    except (TypeError, ValueError):
        return default


def release_days(db: Database) -> int:
    return _int_setting(db, "reserve_release_days", RELEASE_DAYS_DEFAULT)


def hold_days(db: Database) -> int:
    return _int_setting(db, "reserve_hold_days", HOLD_DAYS_DEFAULT)


def release_due(db: Database, r: dict) -> str:
    """When an unreleased hold / unrefunded deposit is chased: check-out + N days (default 7), or check-in + 30
    days when there is no check-out date."""
    if r.get("end_date"):
        return (date.fromisoformat(r["end_date"][:10]) + timedelta(days=release_days(db))).isoformat()
    return (date.fromisoformat(r["start_date"][:10]) + timedelta(days=hold_days(db))).isoformat()


# ------------------------------------------------------------------ Deposits account
def deposits_account(con, currency: str, base: str, demo: bool = False, create: bool = True):
    """The system 'Deposits 押金' receivable account for `currency` (demo reserves use their own [DEMO] one)."""
    row = con.execute("SELECT * FROM accounts WHERE system_role='deposits' AND currency=? AND COALESCE(is_demo,0)=? "
                      "ORDER BY id LIMIT 1", (currency, 1 if demo else 0)).fetchone()
    if row:
        if row["archived"]:
            con.execute("UPDATE accounts SET archived=0 WHERE id=?", (row["id"],))
        return row
    if not create:
        return None
    grp = con.execute("SELECT id FROM account_groups WHERE name=?", (ACCOUNT_TYPES["receivable"][1],)).fetchone()
    name = ("[DEMO] " if demo else "") + DEPOSITS_NAME + ("" if currency == base else f" ({currency})")
    aid = con.execute("""INSERT INTO accounts(group_id, name, type, currency, opening_balance, note, system_role, is_demo)
        VALUES(?,?,?,?,0,?,'deposits',?)""", (grp[0] if grp else None, name, "receivable", currency,
        "System account: refundable deposits held by merchants (Reserves 押金). Created automatically.", 1 if demo else 0)).lastrowid
    return con.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()


def _insert_tx(con, data: dict, rates) -> int:
    tx = normalize_tx(con, data, rates)
    tx["updated_at"] = tx["created_at"] = now_iso()
    cols = [k for k in TX_FIELDS + ["updated_at", "created_at"] if k in tx]
    return con.execute(f"INSERT INTO transactions({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                       [tx[k] for k in cols]).lastrowid


# ------------------------------------------------------------------ reading
SELECT = """SELECT r.*, m.name merchant_name, a.name account_name, a.currency account_currency, a.type account_type,
  a.last4 account_last4, da.name deposit_account_name, c.name category_name, c.name_zh category_name_zh,
  p.name project_name, mb.name member_name
  FROM reserves r LEFT JOIN merchants m ON m.id=r.merchant_id LEFT JOIN accounts a ON a.id=r.account_id
  LEFT JOIN accounts da ON da.id=r.deposit_account_id LEFT JOIN categories c ON c.id=r.category_id
  LEFT JOIN projects p ON p.id=r.project_id LEFT JOIN members mb ON mb.id=r.member_id"""

LINK_SELECT = """SELECT l.*, t.kind tx_kind, t.date tx_date, t.amount tx_amount, t.currency tx_currency, t.note tx_note,
  t.status tx_status, t.account_id tx_account_id, a.name tx_account_name, t.to_account_id tx_to_account_id,
  ta.name tx_to_account_name, t.to_amount tx_to_amount, c.name tx_category_name, c.name_zh tx_category_name_zh,
  m.name tx_merchant_name
  FROM reserve_links l JOIN transactions t ON t.id=l.transaction_id LEFT JOIN accounts a ON a.id=t.account_id
  LEFT JOIN accounts ta ON ta.id=t.to_account_id LEFT JOIN categories c ON c.id=t.category_id
  LEFT JOIN merchants m ON m.id=t.merchant_id"""


def _links(db_or_con, ids: list[int]) -> dict[int, list[dict]]:
    if not ids:
        return {}
    q = f"{LINK_SELECT} WHERE l.reserve_id IN ({','.join('?' * len(ids))}) ORDER BY t.date, l.id"
    rows = db_or_con.all(q, ids) if isinstance(db_or_con, Database) else [dict(r) for r in db_or_con.execute(q, ids)]
    out: dict[int, list] = defaultdict(list)
    for l in rows:
        l["alloc_amount"] = l["alloc_amount"] if l["alloc_amount"] is not None else l["amount"]
        out[l["reserve_id"]].append(l)
    return out


def compute(r: dict, links: list[dict]) -> dict:
    """State and amounts of one reserve from its links (all sums in the reserve's currency)."""
    s = defaultdict(float)
    for l in links:
        s[l["role"]] += l["alloc_amount"] or 0
    final = any(l.get("final") for l in links)
    amt = r["amount"] or 0
    out = {"links": links, "link_count": len(links)}
    if r["method"] == "hold":
        captured = s["charge"]
        if r.get("closed_state"):
            state = r["closed_state"]
        elif final or (amt > EPS and captured >= amt - EPS):
            state = "settled"
        elif captured > EPS:
            state = "partial"
        else:
            state = "open"
        closed = state in CLOSED
        out.update(captured=_r2(captured), over_hold=_r2(max(captured - amt, 0)),
                   released=_r2(max(amt - captured, 0)) if closed else 0.0,
                   outstanding=0.0 if closed else _r2(max(amt - captured, 0)), on_hold=0.0 if closed else _r2(max(amt - captured, 0)))
    else:
        dep, used = s["deposit"], s["refund"] + s["apply"] + s["forfeit"] + s["fx"]
        outstanding = dep - used
        if r.get("closed_state"):
            state = r["closed_state"]
        elif dep > EPS and abs(outstanding) <= EPS:
            state = "settled"
        elif used > EPS:
            state = "partial"
        else:
            state = "open"
        out.update(deposited=_r2(dep), refunded=_r2(s["refund"]), applied=_r2(s["apply"]), forfeited=_r2(s["forfeit"]),
                   fx_adjusted=_r2(s["fx"]), charges=_r2(s["charge"]), outstanding=_r2(outstanding), on_hold=0.0)
    out["state"] = state
    out["closed"] = state in CLOSED
    return out


def decorate(db: Database, r: dict, links: list[dict], on: date | None = None) -> dict:
    r = dict(r)
    r.update(compute(r, links))
    on = on or _today(db)
    r["release_due"] = release_due(db, r)
    r["age_days"] = (on - date.fromisoformat(r["start_date"][:10])).days
    r["overdue_days"] = (on - date.fromisoformat(r["release_due"])).days if not r["closed"] and r.get("remind", 1) else None
    r["label"] = r.get("title") or r.get("merchant_name") or f"Reserve #{r['id']}"
    return r


def _today(db: Database) -> date:
    from .schedules import local_today
    return local_today(db)


def _sync(con, rid: int) -> None:
    """Persist the computed state in reserves.status (used by SQL filters and reminders)."""
    r = con.execute("SELECT * FROM reserves WHERE id=?", (rid,)).fetchone()
    if not r:
        return
    st = compute(dict(r), _links(con, [rid]).get(rid, []))["state"]
    if st != r["status"]:
        con.execute("UPDATE reserves SET status=?, updated_at=? WHERE id=?", (st, now_iso(), rid))


def get(db: Database, rid: int) -> dict:
    r = db.one(SELECT + " WHERE r.id=?", (rid,))
    if not r:
        raise NotFound(rid)
    out = decorate(db, r, _links(db, [rid]).get(rid, []))
    if out["state"] != r["status"]:
        with db.tx() as con:
            con.execute("UPDATE reserves SET status=? WHERE id=?", (out["state"], rid))
    return out


def list_reserves(db: Database, f: dict | None = None) -> dict:
    """Reserves (newest first). f: state = open,partial (default) | settled | released | cancelled | all;
    method, account_id, project_id, review=1 (only the ones from emails that need review)."""
    f = f or {}
    where, params = ["1=1"], []
    for k in ("account_id", "project_id"):
        if f.get(k):
            where.append(f"r.{k}=?"); params.append(int(f[k]))
    if f.get("method") in METHODS:
        where.append("r.method=?"); params.append(f["method"])
    if str(f.get("review") or "") in ("1", "true", "yes"):
        where.append("COALESCE(r.needs_review,0)=1")
    rows = db.all(SELECT + " WHERE " + " AND ".join(where) + " ORDER BY r.start_date DESC, r.id DESC", params)
    lk = _links(db, [r["id"] for r in rows])
    on = _today(db)
    items = [decorate(db, r, lk.get(r["id"], []), on) for r in rows]
    stale = [(x["state"], x["id"]) for x, r in zip(items, rows) if x["state"] != r["status"]]
    if stale:
        with db.tx() as con:
            con.executemany("UPDATE reserves SET status=? WHERE id=?", stale)
    counts = {k: 0 for k in STATES}
    for x in items:
        counts[x["state"]] += 1
    raw = [s.strip() for s in str(f.get("state") or f.get("status") or "open,partial").split(",") if s.strip()]
    if "all" not in raw:
        items = [x for x in items if x["state"] in raw]
    base, rates = db.get_setting("base_currency", "USD"), fx_rates(db)
    holds, deps = defaultdict(float), defaultdict(float)
    for x in items:
        if x["closed"] or x.get("needs_review"):
            continue
        (holds if x["method"] == "hold" else deps)[x["currency"]] += x["outstanding"]
    tot = lambda d: {"by_currency": [{"currency": c, "amount": _r2(v)} for c, v in sorted(d.items()) if abs(v) > EPS],
                     "total_base": _r2(sum(convert(v, c, base, rates) for c, v in d.items())), "base_currency": base}
    return {"items": items, "count": len(items), "state_counts": counts, "on_hold": tot(holds), "deposits": tot(deps),
            "review_count": db.one("SELECT COUNT(*) n FROM reserves WHERE COALESCE(needs_review,0)=1")["n"]}


def review_count(db: Database) -> int:
    return db.one("SELECT COUNT(*) n FROM reserves WHERE COALESCE(needs_review,0)=1")["n"]


def holds_by_account(db: Database) -> dict[int, float]:
    """Open card holds per account, in the account's currency (memo: balances never include them)."""
    rates = fx_rates(db)
    rows = db.all("""SELECT r.*, a.currency account_currency FROM reserves r JOIN accounts a ON a.id=r.account_id
                     WHERE r.method='hold' AND r.status IN ('open','partial') AND COALESCE(r.needs_review,0)=0""")
    lk = _links(db, [r["id"] for r in rows])
    out: dict[int, float] = defaultdict(float)
    for r in rows:
        c = compute(r, lk.get(r["id"], []))
        if not c["closed"] and c["on_hold"] > EPS:
            out[r["account_id"]] += convert(c["on_hold"], r["currency"], r["account_currency"], rates)
    return {k: _r2(v) for k, v in out.items()}


# ------------------------------------------------------------------ create / edit
EDITABLE = ("title", "merchant", "end_date", "start_date", "ref", "note", "category_id", "project_id", "member_id",
            "remind", "amount", "account_id", "currency", "ledger_id")


def _clean(con, body: dict, existing: dict | None = None) -> dict:
    d = {}
    if "title" in body:
        d["title"] = str(body.get("title") or "").strip()[:120]
    if "merchant" in body:
        d["merchant_id"] = get_or_create(con, "merchants", str(body.get("merchant") or "").strip() or None)
    elif "merchant_id" in body:
        d["merchant_id"] = int(body["merchant_id"]) if body.get("merchant_id") else None
    for k in ("start_date", "end_date"):
        if k in body:
            d[k] = _day(body.get(k), k)
    for k in ("category_id", "project_id", "member_id", "ledger_id", "account_id"):
        if k in body:
            d[k] = int(body[k]) if body.get(k) not in (None, "", 0, "0") else None
    for k in ("ref", "note"):
        if k in body:
            d[k] = str(body.get(k) or "").strip()[:500]
    if "remind" in body:
        v = body.get("remind")
        d["remind"] = 1 if (v is True or str(v).lower() in ("1", "true", "yes", "on")) else 0
    if "amount" in body:
        d["amount"] = _num(body.get("amount"), "amount")
    if "currency" in body and body.get("currency"):
        d["currency"] = str(body["currency"]).upper().strip()
    merged = {**(existing or {}), **d}
    if merged.get("end_date") and merged.get("start_date") and merged["end_date"] < merged["start_date"]:
        raise ValidationError("Check-out can't be before check-in")
    if d.get("category_id") and not con.execute("SELECT 1 FROM categories WHERE id=? AND kind='expense'", (d["category_id"],)).fetchone():
        raise ValidationError("Category must be an expense category")
    if d.get("account_id"):
        a = con.execute("SELECT * FROM accounts WHERE id=?", (d["account_id"],)).fetchone()
        if not a:
            raise ValidationError("Account not found")
        if a["system_role"] == "deposits":
            raise ValidationError("Choose the card or account that paid, not the Deposits account")
    return d


def create(db: Database, body: dict) -> dict:
    """{method: hold|deposit, account_id, amount, currency?, merchant?, title?, start_date?, end_date?, category_id?,
    project_id?, member_id?, ref?, note?, remind?, paid_amount? (deposit: what left the paying account, in its
    currency, when it differs from the reserve currency), deposit_tx_id? (link an existing transfer into Deposits)}.
    A deposit also posts the transfer paying account → Deposits 押金."""
    method = str(body.get("method") or "hold").lower()
    if method not in METHODS:
        raise ValidationError("method must be hold or deposit")
    if not body.get("account_id"):
        raise ValidationError("Choose the card or account (account_id)")
    if body.get("amount") in (None, ""):
        raise ValidationError("Enter the amount held or deposited")
    rates = fx_rates(db)
    base = db.get_setting("base_currency", "USD")
    demo = bool(body.get("is_demo"))
    with db.tx() as con:
        rid = _create(con, body, method, rates, base, demo)
    return get(db, rid)


def _create(con, body: dict, method: str, rates, base: str, demo: bool) -> int:
    d = _clean(con, {**body, "start_date": body.get("start_date") or body.get("date") or date.today().isoformat()})
    acct = con.execute("SELECT * FROM accounts WHERE id=?", (d["account_id"],)).fetchone()
    cur = d.get("currency") or acct["currency"]
    if cur not in rates and cur != acct["currency"]:
        raise ValidationError(f"Add an exchange rate for {cur} first (Settings → Currencies)")
    d.update(method=method, currency=cur, needs_review=1 if body.get("needs_review") else 0,
             source=str(body.get("source") or "web")[:20], external_id=body.get("external_id"),
             raw_text=(body.get("raw_text") or None), is_demo=1 if demo else 0, status="open",
             created_at=now_iso(), updated_at=now_iso())
    d.setdefault("ledger_id", int(body.get("ledger_id") or 1))
    d.setdefault("remind", 1)
    if not d.get("title"):
        m = con.execute("SELECT name FROM merchants WHERE id=?", (d.get("merchant_id"),)).fetchone() if d.get("merchant_id") else None
        d["title"] = m[0] if m else ""
    dep = None
    if method == "deposit":
        dep = deposits_account(con, cur, base, demo)
        d["deposit_account_id"] = dep["id"]
    cols = list(d)
    rid = con.execute(f"INSERT INTO reserves({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [d[c] for c in cols]).lastrowid
    if method == "deposit":
        if body.get("deposit_tx_id"):
            _link(con, rid, {"transaction_id": body["deposit_tx_id"], "role": "deposit"}, rates, base)
        else:
            paid = _num(body.get("paid_amount"), "paid_amount")
            same = acct["currency"] == cur
            amt_src = d["amount"] if same else (paid if paid is not None else _r2(convert(d["amount"], cur, acct["currency"], rates)))
            when = d["start_date"] + " " + (str(body.get("time") or "12:00")[:5])
            tid = _insert_tx(con, {"kind": "transfer", "account_id": acct["id"], "to_account_id": dep["id"], "amount": amt_src,
                                   "currency": acct["currency"], "to_amount": None if same else d["amount"], "date": when,
                                   "note": f"Deposit 押金 · {d['title'] or 'Reserve #' + str(rid)}".strip(),
                                   "project_id": d.get("project_id"), "member_id": d.get("member_id"),
                                   "ledger_id": d.get("ledger_id"), "source": "reserve", "is_demo": 1 if demo else 0}, rates)
            con.execute("INSERT INTO reserve_links(reserve_id, transaction_id, role, amount, alloc_amount, created_at) VALUES(?,?,?,?,?,?)",
                        (rid, tid, "deposit", amt_src, d["amount"], now_iso()))
    _sync(con, rid)
    return rid


def update(db: Database, rid: int, body: dict) -> dict:
    r = db.one("SELECT * FROM reserves WHERE id=?", (rid,))
    if not r:
        raise NotFound(rid)
    with db.tx() as con:
        d = _clean(con, {k: v for k, v in body.items() if k in EDITABLE or k == "merchant_id"}, r)
        if r["method"] == "deposit":
            for k in ("amount", "currency", "account_id"):
                if k in d and d[k] != r[k]:
                    raise ValidationError("A deposit's amount, currency and account come from its deposit transfer: edit that transaction instead")
                d.pop(k, None)
        elif "currency" in d and d["currency"] != r["currency"] and con.execute("SELECT 1 FROM reserve_links WHERE reserve_id=?", (rid,)).fetchone():
            raise ValidationError("Unlink the charges before changing the hold's currency")
        if d:
            d["updated_at"] = now_iso()
            con.execute(f"UPDATE reserves SET {','.join(k + '=?' for k in d)} WHERE id=?", list(d.values()) + [rid])
        _sync(con, rid)
    return get(db, rid)


def delete(db: Database, rid: int, unlink: bool = False) -> dict:
    """Delete a reserve. Refused while transactions are linked unless unlink=True (the links go, the transactions
    stay). Its reminder is dismissed."""
    if not db.one("SELECT 1 x FROM reserves WHERE id=?", (rid,)):
        raise NotFound(rid)
    n = db.one("SELECT COUNT(*) n FROM reserve_links WHERE reserve_id=?", (rid,))["n"]
    if n and not unlink:
        raise ValidationError(f"{n} transaction(s) are linked to this reserve: unlink them first (the transactions are kept)")
    with db.tx() as con:
        con.execute("DELETE FROM reserve_links WHERE reserve_id=?", (rid,))
        con.execute("DELETE FROM reserves WHERE id=?", (rid,))
        con.execute("UPDATE reminders SET status='dismissed', done_reason='reserve_deleted', done_at=?, updated_at=? "
                    "WHERE kind='reserve' AND reserve_id=? AND status='open'", (now_iso(), now_iso(), rid))
    return {"ok": True, "unlinked": n}


# ------------------------------------------------------------------ links
def _tolerance(deposit: float) -> float:
    return max(FX_TOL_ABS, abs(deposit) * FX_TOL_PCT)


def _role_for(r, t, dep_id) -> str:
    if r["method"] == "hold":
        return "charge"
    if t["kind"] == "transfer" and t["to_account_id"] == dep_id:
        return "deposit"
    if t["kind"] == "transfer" and t["account_id"] == dep_id:
        return "refund"
    if t["kind"] == "expense" and t["account_id"] == dep_id:
        return "apply"
    if t["kind"] in ("refund", "income"):
        return "refund"
    return "charge"


def _state_sums(con, rid: int, exclude_link: int | None = None) -> dict:
    s = defaultdict(float)
    for l in con.execute("SELECT role, COALESCE(alloc_amount, amount) a FROM reserve_links WHERE reserve_id=? AND id!=?",
                         (rid, exclude_link or 0)):
        s[l["role"]] += l["a"] or 0
    s["outstanding"] = s["deposit"] - s["refund"] - s["apply"] - s["forfeit"] - s["fx"]
    return s


def _link(con, rid: int, body: dict, rates, base: str) -> int:
    r = con.execute("SELECT * FROM reserves WHERE id=?", (rid,)).fetchone()
    if not r:
        raise NotFound(rid)
    try:
        tid = int(body.get("transaction_id") or 0)
    except (TypeError, ValueError):
        raise ValidationError("transaction_id must be an id")
    t = con.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()
    if not t:
        raise ValidationError(f"Transaction {tid} not found")
    if t["status"] != "posted":
        raise ValidationError(f"Transaction {tid} is still in review: approve it first")
    if con.execute("SELECT 1 FROM reserve_links WHERE reserve_id=? AND transaction_id=?", (rid, tid)).fetchone():
        raise ValidationError("That transaction is already linked to this reserve")
    dep_id = r["deposit_account_id"]
    role = str(body.get("role") or _role_for(r, t, dep_id)).lower()
    if role not in ROLES or role == "fx":
        raise ValidationError("role must be deposit, charge, refund, apply or forfeit")
    if r["method"] == "hold" and role != "charge":
        raise ValidationError("A card hold only takes charges (the final bill and extra charges)")
    sums = _state_sums(con, rid)
    t = dict(t)
    if role == "charge":
        if t["kind"] != "expense":
            raise ValidationError("A charge must be an expense")
        if dep_id and t["account_id"] == dep_id:
            role = "apply"
    if role == "deposit":
        if t["kind"] != "transfer" or t["to_account_id"] != dep_id:
            raise ValidationError("A deposit must be a transfer into this reserve's Deposits account")
    elif role == "refund":
        if t["kind"] == "transfer" and t["account_id"] == dep_id:
            pass
        elif t["kind"] in ("refund", "income") and t["account_id"] != dep_id:
            t = _convert_refund(con, r, t, sums, body, rates)  # a merchant refund to the card -> transfer from Deposits
        else:
            raise ValidationError("A refund must be a transfer out of the Deposits account (or a refund/income into the account that gets the money back)")
    elif role in ("apply", "forfeit"):
        if t["kind"] != "expense" or t["account_id"] != dep_id:
            raise ValidationError("An applied or kept deposit must be an expense paid from the Deposits account")
    # amounts: `amount` in the transaction's currency, alloc in the reserve's currency
    tx_amt = t["amount"] or 0
    tx_cur = t["currency"]
    alloc_cur_amt = (t["to_amount"] if t["to_amount"] is not None else tx_amt) if role == "deposit" else tx_amt
    alloc_cur = r["currency"]
    used_elsewhere = con.execute("SELECT COALESCE(SUM(amount),0) FROM reserve_links WHERE transaction_id=?", (tid,)).fetchone()[0]
    amount = _num(body.get("amount"), "amount")
    if amount is None:
        amount = _r2(tx_amt - used_elsewhere)
    if amount <= EPS:
        raise ValidationError("This transaction is already fully linked to other reserves")
    if amount > tx_amt - used_elsewhere + EPS:
        raise ValidationError(f"{amount:.2f} is more than the unlinked part of the transaction ({tx_amt - used_elsewhere:.2f} {tx_cur})")
    alloc = _num(body.get("alloc_amount"), "alloc_amount")
    if alloc is None:
        if role == "deposit":
            alloc = _r2(alloc_cur_amt * amount / tx_amt) if tx_amt else amount
        elif tx_cur == alloc_cur:
            alloc = amount
        else:
            alloc = _r2(convert(amount, tx_cur, alloc_cur, rates))
    cross = tx_cur != alloc_cur or (t["kind"] == "transfer" and t.get("to_amount") is not None)
    if role in ("refund", "apply", "forfeit"):
        left = sums["outstanding"]
        if alloc > left + (_tolerance(sums["deposit"]) if cross and role == "refund" else 0) + EPS:
            raise ValidationError(f"{alloc:.2f} {alloc_cur} is more than the remaining deposit ({left:.2f} {alloc_cur})")
    final = 1 if (body.get("final") in (True, 1, "1", "true", "yes")) else 0
    lid = con.execute("INSERT INTO reserve_links(reserve_id, transaction_id, role, amount, alloc_amount, final, created_at) VALUES(?,?,?,?,?,?,?)",
                      (rid, tid, role, amount, alloc, final, now_iso())).lastrowid
    if r["method"] == "deposit" and role in ("refund", "deposit") and cross:
        _auto_fx(con, r, rates)
    _sync(con, rid)
    return lid


def _convert_refund(con, r, t: dict, sums, body, rates) -> dict:
    """A deposit refund that arrived as a 'refund'/'income' (e.g. from an email) into the card or bank becomes a
    transfer Deposits → that account, so it never counts as income or negative spending."""
    dep = con.execute("SELECT * FROM accounts WHERE id=?", (r["deposit_account_id"],)).fetchone()
    recv_cur = t["currency"]
    alloc = _num(body.get("alloc_amount"), "alloc_amount")
    if alloc is None:
        # converted with the current rates; a small difference to what is left becomes an FX adjustment (_auto_fx)
        alloc = t["amount"] if recv_cur == dep["currency"] else _r2(convert(t["amount"], recv_cur, dep["currency"], rates))
    con.execute("""UPDATE transactions SET kind='transfer', account_id=?, to_account_id=?, amount=?, currency=?, to_amount=?,
        orig_amount=NULL, orig_currency=NULL, category_id=NULL, tax_amount=NULL, tip_amount=NULL, tax_rate=NULL,
        reimbursable=0, note=TRIM(COALESCE(note,'') || ' · Deposit refund 押金退还'), updated_at=? WHERE id=?""",
                (dep["id"], t["account_id"], alloc, dep["currency"], None if recv_cur == dep["currency"] else t["amount"], now_iso(), t["id"]))
    body["alloc_amount"] = alloc
    body["amount"] = alloc
    return dict(con.execute("SELECT * FROM transactions WHERE id=?", (t["id"],)).fetchone())


def _auto_fx(con, r, rates) -> int | None:
    """After a foreign-currency refund: a small residual (≤ 3 % / 1 unit) is posted as an FX adjustment on the
    Deposits account (reason Correction: excluded from income/expense) so it returns to exactly 0."""
    s = _state_sums(con, r["id"])
    res = _r2(s["outstanding"])
    if s["refund"] <= EPS or abs(res) <= EPS or abs(res) > _tolerance(s["deposit"]):
        return None
    last = con.execute("""SELECT MAX(t.date) FROM reserve_links l JOIN transactions t ON t.id=l.transaction_id
                          WHERE l.reserve_id=? AND l.role='refund'""", (r["id"],)).fetchone()[0]  # dated with the refund
    tid = _insert_tx(con, {"kind": "adjustment", "account_id": r["deposit_account_id"], "amount": -res,
                           "date": last or datetime.now().strftime("%Y-%m-%d %H:%M"), "adj_reason": "correction",
                           "note": f"FX difference 汇率差额 · {r['title'] or 'Reserve #' + str(r['id'])}",
                           "source": "reserve", "ledger_id": r["ledger_id"] or 1, "is_demo": r["is_demo"] or 0}, rates)
    con.execute("INSERT INTO reserve_links(reserve_id, transaction_id, role, amount, alloc_amount, created_at) VALUES(?,?,?,?,?,?)",
                (r["id"], tid, "fx", res, res, now_iso()))
    return tid


def link(db: Database, rid: int, body: dict) -> dict:
    """{transaction_id, role?, amount?, alloc_amount?, final?} or {items: [...]}, all or nothing."""
    items = body.get("items") if isinstance(body.get("items"), list) else [body]
    if not items:
        raise ValidationError("Nothing to link")
    rates, base = fx_rates(db), db.get_setting("base_currency", "USD")
    with db.tx() as con:
        for it in items:
            _link(con, rid, dict(it), rates, base)
    return get(db, rid)


def unlink(db: Database, lid: int) -> dict:
    l = db.one("SELECT * FROM reserve_links WHERE id=?", (lid,))
    if not l:
        raise NotFound(lid)
    with db.tx() as con:
        con.execute("DELETE FROM reserve_links WHERE id=?", (lid,))
        _sync(con, l["reserve_id"])
    return get(db, l["reserve_id"])


# ------------------------------------------------------------------ check-out / settle
def settle(db: Database, rid: int, body: dict) -> dict:
    """Check-out in one step (all or nothing; dry_run previews it).
    Hold:    {final: {transaction_id} | {amount, date?, category_id?, account_id?, note?}, charges?: [tid…]} → the final
             bill is linked as a charge and the reserve settles (the rest of the hold is released, nothing recorded);
             or {release: true} → released with no charge.
    Deposit: {refund: {account_id, amount? (deposit returned, reserve currency; default = what is left), received?
             (amount that arrived, account currency), date?}, apply: {amount, category_id?, date?, note?},
             forfeit: {amount, category_id?, date?, note?}, charge: {amount, account_id?, category_id?} (extra bill
             paid by card), charges?: [tid…]}."""
    r0 = get(db, rid)
    dry = bool(body.get("dry_run"))
    rates, base = fx_rates(db), db.get_setting("base_currency", "USD")
    plan: list[dict] = []
    if r0["closed"]:
        raise ValidationError("This reserve is already closed: reopen it first")
    con = db.connect() if dry else None
    try:
        with (_null() if dry else db.tx()) as c:
            con = con or c
            r = con.execute("SELECT * FROM reserves WHERE id=?", (rid,)).fetchone()
            when = (_day(body.get("date")) or date.today().isoformat()) + " " + datetime.now().strftime("%H:%M")
            title = r["title"] or f"Reserve #{rid}"
            common = {"project_id": r["project_id"], "member_id": r["member_id"], "ledger_id": r["ledger_id"] or 1,
                      "merchant_id": r["merchant_id"], "source": "reserve", "is_demo": r["is_demo"] or 0}
            for tid in body.get("charges") or []:
                _link(con, rid, {"transaction_id": tid, "role": "charge"}, rates, base)
                plan.append({"action": "link", "role": "charge", "transaction_id": tid})
            if r["method"] == "hold":
                fin = body.get("final")
                if fin:
                    if fin.get("transaction_id"):
                        _link(con, rid, {"transaction_id": fin["transaction_id"], "role": "charge", "final": True}, rates, base)
                        plan.append({"action": "link", "role": "charge", "transaction_id": fin["transaction_id"], "final": True})
                    else:
                        amt = _num(fin.get("amount"), "final bill amount")
                        if amt is None:
                            raise ValidationError("Enter the final bill amount")
                        aid = int(fin.get("account_id") or r["account_id"])
                        tid = _insert_tx(con, {**common, "kind": "expense", "account_id": aid, "amount": amt,
                                               "currency": fin.get("currency") or r["currency"],
                                               "category_id": fin.get("category_id") or r["category_id"],
                                               "date": (_day(fin.get("date")) or when[:10]) + when[10:],
                                               "note": fin.get("note") or f"Final bill 结账 · {title}"}, rates)
                        _link(con, rid, {"transaction_id": tid, "role": "charge", "final": True}, rates, base)
                        plan.append({"action": "create", "kind": "expense", "role": "charge", "amount": amt, "account_id": aid, "final": True})
                elif body.get("release"):
                    con.execute("UPDATE reserves SET closed_state='released', closed_reason='released', closed_at=? WHERE id=?", (now_iso(), rid))
                    plan.append({"action": "release"})
                elif not body.get("charges"):
                    raise ValidationError("Give the final bill, or release the hold")
            else:
                dep_id = r["deposit_account_id"]
                s = _state_sums(con, rid)
                left = s["outstanding"]
                ap, fo = body.get("apply") or {}, body.get("forfeit") or {}
                ap_amt, fo_amt = _num(ap.get("amount"), "applied amount"), _num(fo.get("amount"), "kept amount")
                rf = body.get("refund") or None
                rf_amt = _num(rf.get("amount"), "refund amount") if rf else None
                rf_to = None
                if rf:
                    rf_to = con.execute("SELECT * FROM accounts WHERE id=?", (int(rf.get("account_id") or r["account_id"]),)).fetchone()
                    if not rf_to:
                        raise ValidationError("Refund account not found")
                    if rf_to["id"] == dep_id:
                        raise ValidationError("Choose the account that got the money back")
                rest = _r2(left - (ap_amt or 0) - (fo_amt or 0))
                if rf and rf_amt is None:
                    recv0 = _num(rf.get("received"), "amount received")
                    if recv0 is not None and rf_to["currency"] != r["currency"]:
                        # only the amount that arrived is known: its value at today's rate; a small difference to
                        # what is left is posted as an FX adjustment (Deposits returns to exactly 0)
                        rf_amt = _r2(convert(recv0, rf_to["currency"], r["currency"], rates))
                    else:
                        rf_amt = rest
                total = (ap_amt or 0) + (fo_amt or 0) + (rf_amt or 0)
                if not (ap_amt or fo_amt or rf_amt or body.get("charge") or body.get("charges")):
                    raise ValidationError("Nothing to settle: enter a refund, an applied or a kept amount")
                cross_rf = bool(rf and rf_to["currency"] != r["currency"])
                if total > left + (_tolerance(s["deposit"]) if cross_rf else 0) + EPS:
                    raise ValidationError(f"Refund + applied + kept ({total:.2f}) is more than the remaining deposit ({left:.2f} {r['currency']})")
                for amt, part, role, default_note in ((ap_amt, ap, "apply", "Deposit applied to the bill 押金抵扣"),
                                                      (fo_amt, fo, "forfeit", "Deposit kept by the merchant 押金被扣")):
                    if amt:
                        tid = _insert_tx(con, {**common, "kind": "expense", "account_id": dep_id, "amount": amt,
                                               "currency": r["currency"], "category_id": part.get("category_id") or r["category_id"],
                                               "date": (_day(part.get("date")) or when[:10]) + when[10:],
                                               "note": (part.get("note") or default_note) + f" · {title}"}, rates)
                        _link(con, rid, {"transaction_id": tid, "role": role}, rates, base)
                        plan.append({"action": "create", "kind": "expense", "role": role, "amount": amt, "account_id": dep_id})
                if rf and rf_amt and rf_amt > EPS:
                    to = rf_to
                    aid = to["id"]
                    recv = _num(rf.get("received"), "amount received")
                    cross = to["currency"] != r["currency"]
                    if cross and recv is None:
                        recv = _r2(convert(rf_amt, r["currency"], to["currency"], rates))
                    tid = _insert_tx(con, {**common, "merchant_id": None, "kind": "transfer", "account_id": dep_id, "to_account_id": aid,
                                           "amount": rf_amt, "currency": r["currency"], "to_amount": recv if cross else None,
                                           "date": (_day(rf.get("date")) or when[:10]) + when[10:],
                                           "note": f"Deposit refund 押金退还 · {title}"}, rates)
                    _link(con, rid, {"transaction_id": tid, "role": "refund"}, rates, base)
                    plan.append({"action": "create", "kind": "transfer", "role": "refund", "amount": rf_amt, "received": recv, "account_id": aid})
                ch = body.get("charge") or {}
                ch_amt = _num(ch.get("amount"), "extra charge")
                if ch_amt:
                    aid = int(ch.get("account_id") or r["account_id"])
                    tid = _insert_tx(con, {**common, "kind": "expense", "account_id": aid, "amount": ch_amt,
                                           "currency": ch.get("currency") or r["currency"], "category_id": ch.get("category_id") or r["category_id"],
                                           "date": when, "note": ch.get("note") or f"Final bill 结账 · {title}"}, rates)
                    _link(con, rid, {"transaction_id": tid, "role": "charge"}, rates, base)
                    plan.append({"action": "create", "kind": "expense", "role": "charge", "amount": ch_amt, "account_id": aid})
            _sync(con, rid)
            after = decorate(db, dict(con.execute(SELECT + " WHERE r.id=?", (rid,)).fetchone()), _links(con, [rid]).get(rid, []))
            if dry:
                con.rollback()
    finally:
        if dry and con is not None:
            con.close()
    if dry:
        after.pop("links", None)
        return {"dry_run": True, "plan": plan, "result": after}
    return {"plan": plan, "reserve": get(db, rid)}


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def close(db: Database, rid: int, how: str = "close") -> dict:
    """close: hold → settled if something was captured, else released; deposit → settled only when nothing is left.
    release: a hold ends with no (more) charge. cancel: booking cancelled (holds with no charge). reopen: undo."""
    r = get(db, rid)
    with db.tx() as con:
        if how == "reopen":
            con.execute("UPDATE reserves SET closed_state=NULL, closed_reason=NULL, closed_at=NULL WHERE id=?", (rid,))
            con.execute("UPDATE reserve_links SET final=0 WHERE reserve_id=?", (rid,))
        else:
            if r["closed"]:
                raise ValidationError("Already closed")
            if r["method"] == "deposit":
                if how in ("release", "cancel"):
                    raise ValidationError("A deposit is closed by refunding, applying or keeping it (Check out)")
                if abs(r["outstanding"]) > EPS:
                    raise ValidationError(f"{r['outstanding']:.2f} {r['currency']} of the deposit is still outstanding: refund, apply or keep it first (Check out)")
                st = "settled"
            elif how == "cancel":
                if r["captured"] > EPS:
                    raise ValidationError("Charges are linked: use Release or Close instead")
                st = "cancelled"
            elif how == "release":
                st = "settled" if r["captured"] > EPS else "released"
            else:
                st = "settled" if r["captured"] > EPS else "released"
            con.execute("UPDATE reserves SET closed_state=?, closed_reason=?, closed_at=? WHERE id=?", (st, how, now_iso(), rid))
        _sync(con, rid)
    return get(db, rid)


# ------------------------------------------------------------------ review queue / email holds
def confirm(db: Database, rid: int) -> dict:
    if not db.one("SELECT 1 x FROM reserves WHERE id=?", (rid,)):
        raise NotFound(rid)
    with db.tx() as con:
        con.execute("UPDATE reserves SET needs_review=0, updated_at=? WHERE id=?", (now_iso(), rid))
    return get(db, rid)


def to_transaction(db: Database, rid: int) -> dict:
    """'It's a real charge': a detected hold becomes an expense in the review queue (same amount, card, merchant,
    date, email id) and the reserve goes away."""
    from . import ledger
    r = get(db, rid)
    if r["method"] != "hold" or r["link_count"]:
        raise ValidationError("Only an unlinked card hold can be turned into a charge")
    rates = fx_rates(db)
    with db.tx() as con:
        tid = _insert_tx(con, {"kind": "expense", "account_id": r["account_id"], "amount": r["amount"], "currency": r["currency"],
                               "merchant_id": r["merchant_id"], "category_id": r["category_id"], "project_id": r["project_id"],
                               "date": r["start_date"] + " 12:00", "note": r["note"] or r["title"] or "", "status": "review",
                               "source": r["source"] or "web", "confidence": 0.5, "raw_text": r.get("raw_text"),
                               "external_id": r.get("external_id"), "ledger_id": r["ledger_id"] or 1, "is_demo": r["is_demo"] or 0}, rates)
        con.execute("DELETE FROM reserves WHERE id=?", (rid,))
        con.execute("UPDATE reminders SET status='dismissed', done_reason='reserve_deleted' WHERE kind='reserve' AND reserve_id=?", (rid,))
    return ledger.get_tx(db, tid)


def from_review(db: Database, tid: int, body: dict) -> dict:
    """Convert a To-review item into a reserve (hold or deposit) with its amount, account, merchant, date and email id;
    the review item is removed. Body overrides any field (method, end_date, title, …)."""
    t = db.one("SELECT t.*, m.name merchant_name FROM transactions t LEFT JOIN merchants m ON m.id=t.merchant_id WHERE t.id=?", (tid,))
    if not t:
        raise NotFound(tid)
    if t["status"] != "review":
        raise ValidationError("Only items in the review queue can be converted (link posted transactions instead)")
    if t["kind"] not in ("expense", "transfer", "refund", "income"):
        raise ValidationError("This kind of item can't become a reserve")
    data = {"method": body.get("method") or "hold", "account_id": t["account_id"],
            "amount": t["orig_amount"] if t["orig_currency"] else t["amount"], "currency": t["orig_currency"] or t["currency"],
            "merchant": t["merchant_name"], "start_date": (t["date"] or "")[:10], "category_id": t["category_id"] if t["kind"] == "expense" else None,
            "project_id": t["project_id"], "member_id": t["member_id"], "note": t["note"], "source": t["source"],
            "external_id": t["external_id"], "raw_text": t["raw_text"], "ledger_id": t["ledger_id"], "is_demo": t["is_demo"]}
    if not t["merchant_name"] and t["note"]:
        data["title"] = str(t["note"]).strip()[:120]
    data.update({k: v for k, v in (body or {}).items() if v not in (None, "")})
    if not data.get("account_id"):
        raise ValidationError("Choose the card or account first (account_id)")
    method = str(data.get("method")).lower()
    if method not in METHODS:
        raise ValidationError("method must be hold or deposit")
    rates, base = fx_rates(db), db.get_setting("base_currency", "USD")
    with db.tx() as con:
        rid = _create(con, data, method, rates, base, bool(data.get("is_demo")))
        con.execute("UPDATE receipts SET transaction_id=NULL WHERE transaction_id=?", (tid,))
        con.execute("DELETE FROM transactions WHERE id=?", (tid,))
    return get(db, rid)


def from_transaction(db: Database, tid: int, body: dict) -> dict:
    """Turn a posted expense (e.g. auto-posted from an email alert) into a reserve, in place.
    hold: the expense goes away (a pre-authorization is not spending), its email id moves to the hold.
    deposit: the expense becomes the transfer card → Deposits 押金 (same id, receipts, email) and is linked as the
    deposit. Body overrides the reserve fields (method, title, end_date, …); amount/currency/account come from the
    transaction for a deposit."""
    t = db.one("SELECT t.*, m.name merchant_name FROM transactions t LEFT JOIN merchants m ON m.id=t.merchant_id WHERE t.id=?", (tid,))
    if not t:
        raise NotFound(tid)
    if t["status"] == "review":
        return from_review(db, tid, body)
    if t["kind"] != "expense":
        raise ValidationError("Only an expense can be turned into a reserve (link other transactions to a reserve instead)")
    if db.one("SELECT 1 x FROM reserve_links WHERE transaction_id=?", (tid,)):
        raise ValidationError("This transaction is already linked to a reserve")
    if db.one("SELECT 1 x FROM reimb_links WHERE expense_id=? OR receipt_id=?", (tid, tid)):
        raise ValidationError("This expense is linked to reimbursements: unlink them first")
    method = str(body.get("method") or "hold").lower()
    if method not in METHODS:
        raise ValidationError("method must be hold or deposit")
    if method == "hold" and db.one("SELECT 1 x FROM reconciled_tx WHERE transaction_id=?", (tid,)):
        raise ValidationError("This expense is in a reconciled statement, so it was really charged: "
                              "use a deposit, or undo the reconciliation first")
    cur, amt = (t["orig_currency"], t["orig_amount"]) if t["orig_currency"] else (t["currency"], t["amount"])
    data = {"account_id": t["account_id"], "amount": amt, "currency": cur, "merchant": t["merchant_name"],
            "start_date": (t["date"] or "")[:10], "category_id": t["category_id"], "project_id": t["project_id"],
            "member_id": t["member_id"], "note": t["note"], "source": t["source"], "ledger_id": t["ledger_id"],
            "is_demo": t["is_demo"]}
    if not t["merchant_name"] and t["note"]:
        data["title"] = str(t["note"]).strip()[:120]
    data.update({k: v for k, v in (body or {}).items() if v not in (None, "")})
    if method == "deposit":  # the money really left: the transaction is the deposit, its numbers win
        data.update(account_id=t["account_id"], amount=amt, currency=cur)
        data.pop("paid_amount", None)
    rates, base = fx_rates(db), db.get_setting("base_currency", "USD")
    demo = bool(data.get("is_demo"))
    with db.tx() as con:
        if method == "hold":
            data.update(external_id=t["external_id"], raw_text=t["raw_text"])
            rid = _create(con, data, "hold", rates, base, demo)
            con.execute("UPDATE receipts SET transaction_id=NULL WHERE transaction_id=?", (tid,))
            con.execute("DELETE FROM transactions WHERE id=?", (tid,))
        else:
            dep = deposits_account(con, cur, base, demo)
            ex = dict(con.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone())
            tx = normalize_tx(con, {"kind": "transfer", "to_account_id": dep["id"], "category_id": None,
                                    "to_amount": t["orig_amount"] if t["orig_currency"] == dep["currency"] else None,
                                    "note": (t["note"] or f"Deposit 押金 · {data.get('title') or t['merchant_name'] or ''}").strip()},
                              rates, existing=ex)
            tx["updated_at"] = now_iso()
            cols = [k for k in TX_FIELDS + ["updated_at"] if k in tx]
            con.execute(f"UPDATE transactions SET {','.join(c + '=?' for c in cols)} WHERE id=?", [tx[k] for k in cols] + [tid])
            rid = _create(con, {**data, "deposit_tx_id": tid}, "deposit", rates, base, demo)
    return get(db, rid)


def create_from_email(db: Database, ex: dict, account_id, date_s: str | None, mid: str, subject: str) -> dict:
    """Email alert recognised as a card hold (rule kind 'hold' or authorization wording): a hold reserve that needs
    review (Confirm / It's a real charge)."""
    data = {"method": "hold", "account_id": account_id, "amount": ex.get("amount"), "currency": ex.get("currency"),
            "merchant": ex.get("merchant"), "start_date": (date_s or date.today().isoformat())[:10], "needs_review": True,
            "source": "email", "external_id": mid, "raw_text": (ex.get("text") or "")[:4000],
            "note": (subject or "").strip()[:200], "ledger_id": int(db.get_setting("default_ledger_id", "1") or 1)}
    acct = db.one("SELECT currency FROM accounts WHERE id=?", (account_id,)) if account_id else None
    if acct and not data["currency"]:
        data["currency"] = acct["currency"]
    rates, base = fx_rates(db), db.get_setting("base_currency", "USD")
    with db.tx() as con:
        rid = _create(con, data, "hold", rates, base, False)
    return get(db, rid)


# ------------------------------------------------------------------ suggestions / badges / transaction hooks
def suggest(db: Database, reserve_id: int | None = None, transaction_id: int | None = None, limit: int = 50) -> dict:
    """From a reserve: posted transactions that could belong to it (same merchant / card / Deposits account, dated
    check-in − 3 days … release date + 30 days), best first, with the role they would get. From a transaction: the
    open reserves it could belong to."""
    from .ledger import TX_SELECT, decorate as tx_decorate
    limit = max(1, min(int(limit or 50), 200))
    if reserve_id:
        r = get(db, int(reserve_id))
        lo = (date.fromisoformat(r["start_date"]) - timedelta(days=3)).isoformat()
        hi = (date.fromisoformat(r["release_due"]) + timedelta(days=30)).isoformat() + " 99"
        linked = {l["transaction_id"] for l in r["links"]}
        dep = r.get("deposit_account_id")
        rows = db.all(TX_SELECT + """ WHERE t.status='posted' AND t.date>=? AND t.date<=? AND t.kind IN ('expense','refund','income','transfer')
                      AND (t.merchant_id=? OR t.account_id=? OR t.account_id=? OR t.to_account_id=?) ORDER BY t.date""",
                      (lo, hi, r["merchant_id"] or -1, r["account_id"] or -1, dep or -1, dep or -1))
        out = []
        for t in rows:
            if t["id"] in linked:
                continue
            if r["method"] == "hold" and t["kind"] != "expense":
                continue
            if t["kind"] == "transfer" and dep not in (t["account_id"], t["to_account_id"]):
                continue
            t = tx_decorate(t)
            sc, why = 0.0, []
            if r["merchant_id"] and t["merchant_id"] == r["merchant_id"]:
                sc += 40; why.append("merchant")
            if t["account_id"] in (r["account_id"], dep) or t["to_account_id"] == dep:
                sc += 20; why.append("account")
            end = r.get("end_date") or r["start_date"]
            if r["start_date"] <= t["date"][:10] <= (date.fromisoformat(end) + timedelta(days=3)).isoformat():
                sc += 25; why.append("date")
            if abs((t["amount"] or 0) - (r["amount"] or 0)) <= max(1, (r["amount"] or 0) * 0.3):
                sc += 10; why.append("amount")
            t.update(score=sc, match=why, role=_role_for(r, t, dep))
            out.append(t)
        out.sort(key=lambda x: (-x["score"], x["date"]))
        return {"reserve": r, "items": out[:limit]}
    if transaction_id:
        t = db.one("SELECT * FROM transactions WHERE id=?", (int(transaction_id),))
        if not t:
            raise NotFound(transaction_id)
        items = [x for x in list_reserves(db, {"state": "open,partial"})["items"] if not x.get("needs_review")]
        linked = {l["reserve_id"] for l in db.all("SELECT reserve_id FROM reserve_links WHERE transaction_id=?", (t["id"],))}
        out = []
        for r in items:
            if r["id"] in linked or (r["method"] == "hold" and t["kind"] != "expense"):
                continue
            sc = (40 if r["merchant_id"] and r["merchant_id"] == t["merchant_id"] else 0) + \
                 (20 if t["account_id"] in (r["account_id"], r.get("deposit_account_id")) else 0) + \
                 (25 if r["start_date"] <= (t["date"] or "")[:10] <= r["release_due"] else 0)
            r.pop("links", None)
            out.append({**r, "score": sc, "role": _role_for(r, t, r.get("deposit_account_id"))})
        out.sort(key=lambda x: -x["score"])
        return {"transaction_id": t["id"], "items": out[:limit]}
    raise ValidationError("Give reserve_id or transaction_id")


def annotate(db: Database, rows: list[dict]) -> list[dict]:
    """Transaction rows get `reserves`: [{id, role, title, state, method}] when they are linked to a reserve."""
    ids = [r["id"] for r in rows if r.get("id")]
    if not ids:
        return rows
    lk = defaultdict(list)
    for l in db.all(f"""SELECT l.transaction_id, l.role, r.id, r.title, r.status, r.method FROM reserve_links l
                        JOIN reserves r ON r.id=l.reserve_id WHERE l.transaction_id IN ({','.join('?' * len(ids))})""", ids):
        lk[l["transaction_id"]].append({"id": l["id"], "role": l["role"], "title": l["title"], "state": l["status"], "method": l["method"]})
    for r in rows:
        if r.get("id") in lk:
            r["reserves"] = lk[r["id"]]
    return rows


def check_tx_update(con, ex: dict, tx: dict) -> None:
    """Called by ledger.update_tx: a transaction linked to a reserve keeps its kind and currency; a whole-amount
    link follows a changed amount."""
    links = con.execute("SELECT * FROM reserve_links WHERE transaction_id=?", (ex["id"],)).fetchall()
    if not links:
        return
    if tx["kind"] != ex["kind"]:
        raise ValidationError("This transaction is linked to a reserve: unlink it before changing its type")
    if tx["currency"] != ex["currency"] or tx.get("account_id") != ex.get("account_id") and any(l["role"] in ("refund", "apply", "forfeit", "fx") for l in links) \
            or (tx.get("to_account_id") != ex.get("to_account_id") and any(l["role"] == "deposit" for l in links)):
        raise ValidationError("This transaction is linked to a reserve: unlink it before moving it to another account or currency")
    if abs((tx["amount"] or 0) - (ex["amount"] or 0)) > EPS and len(links) == 1 and abs(links[0]["amount"] - (ex["amount"] or 0)) <= EPS:
        l = links[0]
        r = con.execute("SELECT currency FROM reserves WHERE id=?", (l["reserve_id"],)).fetchone()
        new_alloc = tx["amount"] if (tx["currency"] == r["currency"] and l["role"] != "deposit") else \
            (tx.get("to_amount") if l["role"] == "deposit" and tx.get("to_amount") is not None else
             _r2((l["alloc_amount"] or l["amount"]) * tx["amount"] / ex["amount"]) if ex["amount"] else tx["amount"])
        con.execute("UPDATE reserve_links SET amount=?, alloc_amount=? WHERE id=?", (tx["amount"], new_alloc, l["id"]))
    for l in links:
        _sync(con, l["reserve_id"])


def export_rows(db: Database) -> list[dict]:
    rows = list_reserves(db, {"state": "all"})["items"]
    out = []
    for r in rows:
        out.append({"id": r["id"], "method": r["method"], "title": r["label"], "merchant": r.get("merchant_name"),
                    "account": r.get("account_name"), "amount": r["amount"], "currency": r["currency"], "check_in": r["start_date"],
                    "check_out": r.get("end_date"), "state": r["state"], "outstanding": r["outstanding"],
                    "links": "; ".join(f"#{l['transaction_id']}:{l['role']}={l['alloc_amount']:.2f}" for l in r["links"])})
    return out
