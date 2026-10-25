"""Loan Center 借贷中心: money you lent (借出) or borrowed (借入) — a family loan, a mortgage, a car loan — with its
expected repayment schedule, each repayment split into principal and interest, and reminders.

Model (no new transaction kinds; nothing here changes how a transaction counts in the stats):
* A loan owns ONE account that holds the outstanding principal: borrowed → a liability ('loan' for a bank, 'payable'
  for a person), lent → a 'receivable'. The account balance IS the outstanding amount (edits, adjustments and
  reconciliations included), so the Loan Center never keeps a second copy of it.
* Disbursement: borrowed = transfer loan account → the account that got the money; lent = transfer paying account →
  receivable. Net worth does not change and nothing counts as income or spending.
* A repayment is one EVENT of 1–4 linked transactions in the loan's currency:
    borrowed: principal = transfer pay account → loan account; interest / escrow / fee = expenses from the pay account
    lent:     principal = transfer receivable → receiving account; interest / fee = income into that account
  Only interest, escrow and fees are spending or income; the principal is a transfer.
* An existing loan can be tracked "from now on": the loan uses the account's current balance (no history rewrite).
* Expected schedule: fixed-payment amortization at the period rate (annual rate / periods per year) from the CURRENT
  outstanding, so extra payments shorten the term. freq 'once' = one repayment at the maturity date, 'none' = repay
  any time (interest, if any, accrues daily on the outstanding amount).
* A loan can own a recurring schedule (schedules.loan_id): each occurrence posts the split payment (post) or makes a
  Reminder Center item (remind). Without a schedule, the next unpaid due date (or the maturity date) is a 'loan'
  reminder.

loan_events(id, loan_id, kind disburse|payment|writeoff, date, due_date, account_id, note) group the
loan_links(event_id, transaction_id, role disburse|principal|interest|escrow|fee|writeoff, amount in loan currency).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from .db import ACCOUNT_TYPES, Database, now_iso
from .ledger import EFFECT_SQL, TX_FIELDS, ValidationError, convert, fx_rates, normalize_tx

DIRECTIONS = ("lent", "borrowed")
KINDS = ("person", "bank", "other")
FREQS = ("monthly", "biweekly", "weekly", "quarterly", "yearly", "once", "none")
PERIODS = {"monthly": 12, "biweekly": 26, "weekly": 52, "quarterly": 4, "yearly": 1}
ROLES = ("disburse", "principal", "interest", "escrow", "fee", "writeoff")
ACCT_TYPES = {"borrowed": ("loan", "payable"), "lent": ("receivable",)}
EPS = 0.005
MAX_ROWS = 720


class NotFound(KeyError):
    pass


def _r2(v) -> float:
    return round(v or 0.0, 2) + 0.0


def _num(v, what: str, positive: bool = False, allow_zero: bool = True) -> float | None:
    if v in (None, ""):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be a number")
    if x < 0 or (positive and x <= 0) or (not allow_zero and x == 0):
        raise ValidationError(f"{what} must be greater than zero" if positive else f"{what} can't be negative")
    return x


def _day(v, what: str = "date") -> str | None:
    if v in (None, ""):
        return None
    try:
        return datetime.strptime(str(v).strip().replace("T", " ")[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValidationError(f"{what} must be YYYY-MM-DD")


def _today(db: Database) -> date:
    from .schedules import local_today
    return local_today(db)


def _cat(con, kind: str, *names: str) -> int | None:
    """First existing category of `kind` with one of `names` (sub-categories first)."""
    for name in names:
        r = con.execute("SELECT id FROM categories WHERE kind=? AND lower(name)=? ORDER BY parent_id IS NULL, id LIMIT 1",
                        (kind, name.lower())).fetchone()
        if r:
            return r[0]
    return None


# ------------------------------------------------------------------ balances (inside a db transaction)
def balance(con, aid: int, until: str | None = None) -> float:
    """Signed cash balance of one account like ledger.cash_balances (reconciliation-anchored), using `con`.
    until = 'YYYY-MM-DD HH:MM' inclusive (None = everything posted)."""
    cond, params = "status='posted'", []
    if until:
        cond += " AND date <= ?"; params.append(until)
    cp = con.execute("SELECT as_of, cash_balance FROM reconciliations WHERE account_id=?" + (" AND as_of <= ?" if until else "")
                     + " ORDER BY as_of DESC, id DESC LIMIT 1", [aid] + ([until] if until else [])).fetchone()
    if cp:
        start, cond = cp["cash_balance"], cond + " AND date > ?"
        params.append(cp["as_of"])
    else:
        a = con.execute("SELECT opening_balance FROM accounts WHERE id=?", (aid,)).fetchone()
        start = (a[0] if a else 0) or 0
    a = con.execute(f"SELECT COALESCE(SUM({EFFECT_SQL}),0) FROM transactions WHERE {cond} AND account_id=?", params + [aid]).fetchone()[0]
    b = con.execute(f"SELECT COALESCE(SUM(COALESCE(to_amount, amount)),0) FROM transactions WHERE {cond} AND kind='transfer' "
                    "AND to_account_id=?", params + [aid]).fetchone()[0]
    return _r2(start + (a or 0) + (b or 0))


def outstanding(con, loan, until: str | None = None) -> float:
    """What is still owed (positive): −balance for a borrowed loan, +balance for money lent."""
    b = balance(con, loan["account_id"], until)
    return _r2(-b if loan["direction"] == "borrowed" else b)


# ------------------------------------------------------------------ schedule maths
def period_rate(loan) -> float:
    n = PERIODS.get(loan["freq"])
    return (loan["rate"] or 0) / 100 / n if n else 0.0


def fixed_payment(bal: float, i: float, n: int) -> float:
    """Level payment that repays `bal` in `n` periods at period rate `i`."""
    if n <= 0:
        return _r2(bal)
    if i <= 0:
        return _r2(bal / n)
    return _r2(bal * i / (1 - (1 + i) ** -n))


def _rule(loan, start: str) -> dict:
    f = loan["freq"]
    d = date.fromisoformat(start)
    if f in ("weekly", "biweekly"):
        return {"freq": "weekly", "interval": 2 if f == "biweekly" else 1, "weekdays": str(d.weekday()), "start_date": start}
    return {"freq": f, "interval": 1, "month_mode": "day", "month_day": d.day, "start_date": start}


def due_dates(loan, until: date, after: date | None = None):
    """Expected due dates from first_due up to `until` (periodic loans) or [maturity] for freq 'once'."""
    from .schedules import iter_occurrences
    if loan["freq"] == "once":
        m = loan["maturity_date"] or loan["first_due"]
        if m and date.fromisoformat(m) <= until and (not after or date.fromisoformat(m) >= after):
            yield date.fromisoformat(m)
        return
    if loan["freq"] not in PERIODS or not loan["first_due"]:
        return
    rule = _rule(loan, loan["first_due"])
    if loan["maturity_date"]:
        rule["end_date"] = max(loan["maturity_date"], loan["first_due"])
    for d in iter_occurrences(rule, until):
        if not after or d >= after:
            yield d


def project(loan, bal: float, start_due: date | None, limit: int = MAX_ROWS) -> dict:
    """Fixed-payment schedule from the current outstanding `bal`, the first row due on `start_due`."""
    rows, i = [], period_rate(loan)
    pay, esc = loan["payment"] or 0, loan["escrow"] or 0
    warn = None
    if bal <= EPS or not start_due:
        return {"rows": rows, "payoff_date": None, "future_interest": 0.0, "warning": None}
    if loan["freq"] == "once":
        days = max((start_due - date.fromisoformat(loan["start_date"])).days, 0)
        it = _r2(bal * (loan["rate"] or 0) / 100 * days / 365)
        rows.append({"due_date": start_due.isoformat(), "payment": _r2(bal + it), "principal": _r2(bal), "interest": it,
                     "escrow": 0.0, "balance_after": 0.0})
        return {"rows": rows, "payoff_date": start_due.isoformat(), "future_interest": it, "warning": None}
    if pay <= EPS:
        return {"rows": rows, "payoff_date": None, "future_interest": 0.0, "warning": "Set the regular payment (or the number of payments) to see the schedule"}
    tot_int = 0.0
    for d in due_dates(loan, date(9999, 1, 1), after=start_due):
        it = _r2(bal * i)
        pr = _r2(min(pay - it, bal))
        if pr <= EPS:
            warn = "The payment does not cover the interest: the loan never gets paid off"
            rows.append({"due_date": d.isoformat(), "payment": _r2(pay + esc), "principal": 0.0, "interest": it, "escrow": esc, "balance_after": _r2(bal)})
            break
        last = loan["maturity_date"] and d.isoformat() >= loan["maturity_date"]
        if (last and bal - pr > EPS) or 0 < bal - pr < max(1.0, pay * 0.01):  # balloon at maturity / rounding cents
            pr = _r2(bal)
        bal = _r2(bal - pr)
        tot_int += it
        rows.append({"due_date": d.isoformat(), "payment": _r2(pr + it + esc), "principal": pr, "interest": it, "escrow": esc,
                     "balance_after": bal})
        if bal <= EPS or len(rows) >= limit:
            break
    return {"rows": rows, "payoff_date": rows[-1]["due_date"] if rows and rows[-1]["balance_after"] <= EPS else None,
            "future_interest": _r2(tot_int), "warning": warn}


# ------------------------------------------------------------------ reading
SELECT = """SELECT l.*, a.name account_name, a.type account_type, a.archived account_archived, pa.name pay_account_name,
  pa.currency pay_account_currency, ic.name interest_category_name, ec.name escrow_category_name
  FROM loans l LEFT JOIN accounts a ON a.id=l.account_id LEFT JOIN accounts pa ON pa.id=l.pay_account_id
  LEFT JOIN categories ic ON ic.id=l.interest_category_id LEFT JOIN categories ec ON ec.id=l.escrow_category_id"""

LINK_SELECT = """SELECT k.*, t.kind tx_kind, t.date tx_date, t.amount tx_amount, t.currency tx_currency, t.status tx_status,
  t.note tx_note, t.account_id tx_account_id, a.name tx_account_name, t.to_account_id tx_to_account_id,
  ta.name tx_to_account_name, c.name tx_category_name
  FROM loan_links k JOIN transactions t ON t.id=k.transaction_id LEFT JOIN accounts a ON a.id=t.account_id
  LEFT JOIN accounts ta ON ta.id=t.to_account_id LEFT JOIN categories c ON c.id=t.category_id"""


def _events(con, lid: int) -> list[dict]:
    ev = [dict(r) for r in con.execute("SELECT * FROM loan_events WHERE loan_id=? ORDER BY date, id", (lid,))]
    links = defaultdict(list)
    for k in con.execute(LINK_SELECT + " WHERE k.loan_id=? ORDER BY k.id", (lid,)):
        links[k["event_id"]].append(dict(k))
    out = []
    for e in ev:
        e["links"] = links.get(e["id"], [])
        s = defaultdict(float)
        for k in e["links"]:
            s[k["role"]] += k["amount"] or 0
        e.update({r: _r2(s[r]) for r in ROLES})
        e["total"] = _r2(sum(s[r] for r in ("principal", "interest", "escrow", "fee")) + s["disburse"] + s["writeoff"])
        e["posted"] = all(k["tx_status"] == "posted" for k in e["links"])
        out.append(e)
    return out


def _is_paid(got: float, expected: float, freq: str) -> bool:
    return got > EPS and (got >= expected - EPS or expected <= EPS or freq == "once")


def _paid_dues(events) -> dict[str, float]:
    out = defaultdict(float)
    for e in events:
        if e["kind"] == "payment" and e["due_date"] and e["links"]:
            out[e["due_date"]] += e["total"]
    return out


def compute(con, loan: dict, on: date) -> dict:
    """Outstanding, interest paid/earned, due rows (paid / partial / overdue) and the projection."""
    x = dict(loan)
    ev = _events(con, loan["id"])
    x["events"] = ev
    out = outstanding(con, loan)
    x["outstanding"] = out
    yr = str(on.year)
    x["interest_total"] = _r2(sum(e["interest"] for e in ev))
    x["interest_ytd"] = _r2(sum(e["interest"] for e in ev if (e["date"] or "")[:4] == yr))
    x["principal_repaid"] = _r2(sum(e["principal"] for e in ev))
    x["disbursed"] = _r2(sum(e["disburse"] for e in ev))
    x["payment_count"] = sum(1 for e in ev if e["kind"] == "payment")
    x["last_payment"] = max((e["date"] for e in ev if e["kind"] == "payment"), default=None)
    paid = _paid_dues(ev)
    expected = round((loan["payment"] or 0) + (loan["escrow"] or 0), 2)
    past, overdue, partial = [], [], []
    for d in due_dates(loan, on):
        ds = d.isoformat()
        got = paid.get(ds, 0.0)
        st = "paid" if _is_paid(got, expected, loan["freq"]) else "partial" if got > EPS else ("overdue" if d < on else "due")
        if st in ("overdue", "due") and out <= EPS:
            continue
        past.append({"due_date": ds, "expected": expected, "paid": _r2(got), "status": st})
        if st == "overdue":
            overdue.append(ds)
        elif st == "partial":
            partial.append(ds)
    x["due_rows"] = past[-60:]
    x["overdue"] = overdue
    x["partial"] = partial
    nxt = next((date.fromisoformat(r["due_date"]) for r in past if r["status"] in ("overdue", "due", "partial")), None)
    if not nxt and out > EPS:
        for d in due_dates(loan, date(9999, 1, 1), after=on + timedelta(days=1)):
            if not _is_paid(paid.get(d.isoformat(), 0.0), expected, loan["freq"]):
                nxt = d
                break
    x["next_due"] = nxt.isoformat() if nxt else None
    pj = project(loan, out, nxt) if loan["freq"] != "none" else {"rows": [], "payoff_date": None, "future_interest": 0.0, "warning": None}
    x["projection"] = pj["rows"]
    x["payoff_date"] = pj["payoff_date"]
    x["future_interest"] = pj["future_interest"]
    x["projection_warning"] = pj["warning"]
    x["next_payment"] = pj["rows"][0] if pj["rows"] else None
    if loan["freq"] == "none" and (loan["rate"] or 0) > 0 and out > EPS:
        x["accrued_interest"] = accrued(con, loan, on)
    if loan["status"] == "active" and out <= EPS and (x["payment_count"] or x["disbursed"]):
        x["state"] = "paid_off"
    else:
        x["state"] = loan["status"]
    x["closed"] = x["state"] != "active"
    x["overdue_days"] = (on - date.fromisoformat(overdue[0])).days if overdue and not x["closed"] else None
    if loan["freq"] == "none" and loan["maturity_date"] and not x["closed"] and out > EPS and loan["maturity_date"] < on.isoformat():
        x["overdue_days"] = (on - date.fromisoformat(loan["maturity_date"])).days
    x["label"] = loan["name"] or loan["counterparty"] or f"Loan #{loan['id']}"
    return x


def accrued(con, loan, on: date) -> float:
    """Simple daily interest on the outstanding amount since the last repayment (or the start)."""
    last = con.execute("SELECT MAX(date) FROM loan_events WHERE loan_id=? AND kind='payment'", (loan["id"],)).fetchone()[0]
    since = date.fromisoformat((last or loan["start_date"])[:10])
    return _r2(outstanding(con, loan) * (loan["rate"] or 0) / 100 * max((on - since).days, 0) / 365)


def _sync(con, lid: int, on: date) -> dict:
    """Drop events that lost every transaction; persist paid_off / active."""
    con.execute("DELETE FROM loan_events WHERE loan_id=? AND id NOT IN (SELECT DISTINCT event_id FROM loan_links WHERE loan_id=?)", (lid, lid))
    loan = con.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
    x = compute(con, dict(loan), on)
    if loan["status"] in ("active", "paid_off") and x["state"] != loan["status"]:
        con.execute("UPDATE loans SET status=?, updated_at=? WHERE id=?", (x["state"], now_iso(), lid))
    if x["state"] == "paid_off" and loan["schedule_id"]:
        con.execute("UPDATE schedules SET enabled=0, updated_at=? WHERE id=?", (now_iso(), loan["schedule_id"]))
    return x


def get(db: Database, lid: int, on: date | None = None) -> dict:
    on = on or _today(db)
    with db.tx() as con:
        if not con.execute("SELECT 1 FROM loans WHERE id=?", (lid,)).fetchone():
            raise NotFound(lid)
        _sync(con, lid, on)
        x = compute(con, dict(con.execute(SELECT + " WHERE l.id=?", (lid,)).fetchone()), on)
    if x.get("schedule_id"):
        s = db.one("SELECT id, name, enabled, post_mode, time_of_day FROM schedules WHERE id=?", (x["schedule_id"],))
        x["schedule"] = s
    return x


def list_loans(db: Database, f: dict | None = None) -> dict:
    """Loans + totals. f: state = active (default) | paid_off | closed | all; direction = lent | borrowed."""
    f = f or {}
    on = _today(db)
    base, rates = db.get_setting("base_currency", "USD"), fx_rates(db)
    with db.tx() as con:
        for (lid,) in con.execute("SELECT id FROM loans").fetchall():
            _sync(con, lid, on)
        rows = [compute(con, dict(r), on) for r in con.execute(SELECT + " ORDER BY l.status, l.start_date DESC, l.id DESC")]
    counts = defaultdict(int)
    for x in rows:
        counts[x["state"]] += 1
    tot = {d: defaultdict(float) for d in DIRECTIONS}
    ytd = {d: 0.0 for d in DIRECTIONS}
    for x in rows:
        if not x["closed"]:
            tot[x["direction"]][x["currency"]] += x["outstanding"]
        ytd[x["direction"]] += convert(x["interest_ytd"], x["currency"], base, rates)
    want = [s.strip() for s in str(f.get("state") or "active").split(",") if s.strip()]
    items = [x for x in rows if "all" in want or x["state"] in want]
    if f.get("direction") in DIRECTIONS:
        items = [x for x in items if x["direction"] == f["direction"]]
    for x in items:
        x.pop("events", None)
        x["projection"] = x["projection"][:1]
    pack = lambda d: {"by_currency": [{"currency": c, "amount": _r2(v)} for c, v in sorted(d.items()) if abs(v) > EPS],
                      "total_base": _r2(sum(convert(v, c, base, rates) for c, v in d.items())), "base_currency": base}
    return {"items": items, "count": len(items), "state_counts": dict(counts), "owed_to_me": pack(tot["lent"]),
            "i_owe": pack(tot["borrowed"]), "interest_earned_ytd": _r2(ytd["lent"]), "interest_paid_ytd": _r2(ytd["borrowed"]),
            "overdue_count": sum(1 for x in rows if x.get("overdue_days") and x["overdue_days"] > 0),
            "base_currency": base, "today": on.isoformat()}


# ------------------------------------------------------------------ create / edit
EDITABLE = ("name", "counterparty", "kind", "pay_account_id", "rate", "freq", "first_due", "term", "payment", "escrow",
            "interest_category_id", "escrow_category_id", "maturity_date", "note", "remind", "start_date")


def _clean(con, body: dict, ex: dict | None = None) -> dict:
    d = {}
    for k in ("name", "counterparty", "note"):
        if k in body:
            d[k] = str(body.get(k) or "").strip()[:200]
    if "kind" in body:
        d["kind"] = str(body.get("kind") or "person").lower()
        if d["kind"] not in KINDS:
            raise ValidationError("kind must be person, bank or other")
    if "freq" in body:
        d["freq"] = str(body.get("freq") or "none").lower()
        if d["freq"] not in FREQS:
            raise ValidationError(f"freq must be one of {', '.join(FREQS)}")
    for k in ("start_date", "first_due", "maturity_date"):
        if k in body:
            d[k] = _day(body.get(k), k)
    if "rate" in body:
        r = _num(body.get("rate"), "rate")
        if r is not None and r > 100:
            raise ValidationError("rate is a yearly percentage (e.g. 6.5)")
        d["rate"] = r or 0.0
    for k in ("payment", "escrow"):
        if k in body:
            v = _num(body.get(k), k)
            d[k] = _r2(v) if v else None
    if "term" in body:
        try:
            d["term"] = int(body["term"]) if body.get("term") not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            raise ValidationError("term is the number of payments")
        if d["term"] is not None and not 1 <= d["term"] <= 1200:
            raise ValidationError("term must be between 1 and 1200 payments")
    for k in ("pay_account_id", "interest_category_id", "escrow_category_id"):
        if k in body:
            d[k] = int(body[k]) if body.get(k) not in (None, "", 0, "0") else None
    if "remind" in body:
        v = body.get("remind")
        d["remind"] = 1 if (v is True or str(v).lower() in ("1", "true", "yes", "on")) else 0
    m = {**(ex or {}), **d}
    if m.get("freq") in PERIODS or m.get("freq") == "once":
        if m.get("freq") == "once" and not (m.get("maturity_date") or m.get("first_due")):
            raise ValidationError("A one-time repayment needs its due date (maturity_date)")
        if m.get("freq") in PERIODS and not m.get("first_due"):
            raise ValidationError("Enter the first payment date (first_due)")
    if m.get("first_due") and m.get("start_date") and m["first_due"] < m["start_date"]:
        raise ValidationError("The first payment can't be before the start date")
    if m.get("maturity_date") and m.get("start_date") and m["maturity_date"] < m["start_date"]:
        raise ValidationError("The maturity date can't be before the start date")
    if d.get("pay_account_id"):
        a = con.execute("SELECT * FROM accounts WHERE id=?", (d["pay_account_id"],)).fetchone()
        if not a:
            raise ValidationError("Payment account not found")
        if ex and a["id"] == ex.get("account_id"):
            raise ValidationError("Choose the bank account the payments go through, not the loan account")
    for k, kind in (("interest_category_id", None), ("escrow_category_id", "expense")):
        if d.get(k):
            c = con.execute("SELECT kind FROM categories WHERE id=?", (d[k],)).fetchone()
            want = kind or ("income" if (m.get("direction") or body.get("direction")) == "lent" else "expense")
            if not c or c[0] != want:
                raise ValidationError(f"{k.replace('_id', '').replace('_', ' ').capitalize()} must be an {want} category")
    return d


def _new_account(con, direction: str, kind: str, name: str, currency: str, opening: float, demo: bool, start: str, rates) -> int:
    """The loan's account; what is already owed is its opening balance (an 'Opening balance' adjustment like a new
    account in Accounts: never income or spending)."""
    typ = "receivable" if direction == "lent" else ("loan" if kind == "bank" else "payable")
    grp = con.execute("SELECT id FROM account_groups WHERE name=?", (ACCOUNT_TYPES[typ][1],)).fetchone()
    aid = con.execute("""INSERT INTO accounts(group_id, name, type, currency, opening_balance, note, is_demo, created_at)
        VALUES(?,?,?,?,0,?,?,?)""", (grp[0] if grp else None, name, typ, currency,
                                     "Loan Center 借贷中心: holds what is still owed. Created automatically.", 1 if demo else 0, now_iso())).lastrowid
    if abs(opening) > EPS:
        _insert_tx(con, {"kind": "adjustment", "amount": opening, "account_id": aid, "date": start + " 00:00", "adj_reason": "opening",
                         "note": "Opening balance 期初余额", "source": "opening_balance", "is_demo": 1 if demo else 0}, rates)
    return aid


def create(db: Database, body: dict) -> dict:
    """{direction lent|borrowed, counterparty, name?, kind person|bank|other, principal, currency?, start_date?,
    account_id? (an existing loan / payable / receivable account: tracked from its current balance), pay_account_id
    (where the money came from / went, and where repayments are paid from or arrive), disburse? (post the transfer
    now; default true for a new account), balance? (new account, not disbursed: what is owed today, default principal),
    rate? (% per year), freq?, first_due?, term? (number of payments), payment? (regular payment; computed from term),
    escrow?, interest_category_id?, escrow_category_id?, maturity_date?, note?, remind?}"""
    direction = str(body.get("direction") or "").lower()
    if direction not in DIRECTIONS:
        raise ValidationError("direction must be lent (money you lent) or borrowed (money you owe)")
    if not (body.get("counterparty") or body.get("name")):
        raise ValidationError("Who is the loan with? (counterparty)")
    on = _today(db)
    rates = fx_rates(db)
    demo = bool(body.get("is_demo"))
    with db.tx() as con:
        d = _clean(con, {"kind": "person", "freq": "none", "rate": 0, "start_date": on.isoformat(), **body})
        d.update(direction=direction, status="active", ledger_id=int(body.get("ledger_id") or 1), is_demo=1 if demo else 0,
                 created_at=now_iso(), updated_at=now_iso())
        d.setdefault("remind", 1)
        d["name"] = d.get("name") or d.get("counterparty")
        principal = _num(body.get("principal"), "principal")
        pay = None
        if d.get("pay_account_id"):
            pay = con.execute("SELECT * FROM accounts WHERE id=?", (d["pay_account_id"],)).fetchone()
        if body.get("account_id"):
            acct = con.execute("SELECT * FROM accounts WHERE id=?", (int(body["account_id"]),)).fetchone()
            if not acct:
                raise ValidationError("Account not found")
            if acct["type"] not in ACCT_TYPES[direction] or acct["system_role"]:
                raise ValidationError(("Money you owe uses a Loan or Other payable account" if direction == "borrowed"
                                       else "Money you lent uses a Receivable account") + f" ({acct['name']} is {acct['type']})")
            if con.execute("SELECT 1 FROM loans WHERE account_id=? AND status='active'", (acct["id"],)).fetchone():
                raise ValidationError(f"{acct['name']} already belongs to another active loan")
            disburse = body.get("disburse") in (True, 1, "1", "true", "yes")
            d["account_id"] = acct["id"]
            d["currency"] = acct["currency"]
        else:
            disburse = body.get("disburse") not in (False, 0, "0", "false", "no")
            cur = str(body.get("currency") or (pay["currency"] if pay else db.get_setting("base_currency", "USD"))).upper()
            if not principal and disburse:
                raise ValidationError("Enter the amount lent or borrowed (principal)")
            owed = 0.0 if disburse else (_num(body.get("balance"), "balance") if body.get("balance") not in (None, "") else (principal or 0))
            prefix = "[DEMO] " if demo else ""
            nm = prefix + ("Loan · " if direction == "borrowed" else "Lent · ") + str(d["name"])[:80]
            d["account_id"] = _new_account(con, direction, d["kind"], nm, cur, _r2(-owed if direction == "borrowed" else owed), demo,
                                           d["start_date"], rates)
            d["currency"] = cur
        if disburse:
            if not principal:
                raise ValidationError("Enter the amount lent or borrowed (principal)")
            if not pay:
                raise ValidationError("Choose the account the money " + ("went into" if direction == "borrowed" else "came from") + " (pay_account_id)")
        if pay and pay["id"] == d["account_id"]:
            raise ValidationError("Choose the bank account the payments go through, not the loan account")
        d["principal"] = _r2(principal) if principal else None
        if not d.get("interest_category_id"):
            d["interest_category_id"] = _cat(con, "income", "Interest") if direction == "lent" else _cat(con, "expense", "Interest Paid")
        if d.get("escrow") and not d.get("escrow_category_id"):
            d["escrow_category_id"] = _cat(con, "expense", "Property Tax", "Taxes")
        cols = list(d)
        lid = con.execute(f"INSERT INTO loans({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [d[c] for c in cols]).lastrowid
        loan = dict(con.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone())
        if disburse:
            _disburse(con, loan, principal, d["start_date"] + " " + str(body.get("time") or "12:00")[:5], pay["id"], rates,
                      body.get("note_disburse"))
        _fill_payment(con, loan)
        _sync(con, lid, on)
    return get(db, lid)


def _fill_payment(con, loan: dict) -> None:
    """Regular payment from the number of payments when it was left blank."""
    loan = dict(con.execute("SELECT * FROM loans WHERE id=?", (loan["id"],)).fetchone())
    if loan["payment"] or not loan["term"] or loan["freq"] not in PERIODS:
        return
    bal = outstanding(con, loan) or (loan["principal"] or 0)
    p = fixed_payment(bal, period_rate(loan), loan["term"])
    con.execute("UPDATE loans SET payment=? WHERE id=?", (p, loan["id"]))


def update(db: Database, lid: int, body: dict) -> dict:
    with db.tx() as con:
        ex = con.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not ex:
            raise NotFound(lid)
        ex = dict(ex)
        d = _clean(con, {k: v for k, v in body.items() if k in EDITABLE}, ex)
        if d.get("pay_account_id") == ex["account_id"]:
            raise ValidationError("Choose the bank account the payments go through, not the loan account")
        if "term" in d and "payment" not in d and d["term"] != ex["term"]:
            d["payment"] = None  # recomputed from the new term
        if d:
            d["updated_at"] = now_iso()
            con.execute(f"UPDATE loans SET {','.join(k + '=?' for k in d)} WHERE id=?", list(d.values()) + [lid])
        _fill_payment(con, ex)
        loan = dict(con.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone())
        if loan["schedule_id"]:
            _sync_schedule(con, loan)
        _sync(con, lid, _today(db))
    return get(db, lid)


def delete(db: Database, lid: int, unlink: bool = False) -> dict:
    """Delete the loan record. Refused while transactions are linked unless unlink=True (links go, transactions and
    the account stay). Its schedule is deleted (generated history kept) and its reminder dismissed."""
    loan = db.one("SELECT * FROM loans WHERE id=?", (lid,))
    if not loan:
        raise NotFound(lid)
    n = db.one("SELECT COUNT(*) n FROM loan_links WHERE loan_id=?", (lid,))["n"]
    if n and not unlink:
        raise ValidationError(f"{n} transaction(s) are linked to this loan: unlink them first (the transactions are kept)")
    with db.tx() as con:
        con.execute("DELETE FROM loan_links WHERE loan_id=?", (lid,))
        con.execute("DELETE FROM loan_events WHERE loan_id=?", (lid,))
        if loan["schedule_id"]:
            con.execute("DELETE FROM schedule_occurrences WHERE schedule_id=?", (loan["schedule_id"],))
            con.execute("DELETE FROM schedules WHERE id=?", (loan["schedule_id"],))
        con.execute("DELETE FROM loans WHERE id=?", (lid,))
        con.execute("UPDATE reminders SET status='dismissed', done_reason='loan_deleted', done_at=?, updated_at=? "
                    "WHERE kind='loan' AND loan_id=? AND status='open'", (now_iso(), now_iso(), lid))
    return {"ok": True, "unlinked": n}


# ------------------------------------------------------------------ posting
def _insert_tx(con, data: dict, rates) -> int:
    tx = normalize_tx(con, data, rates)
    tx["updated_at"] = tx["created_at"] = now_iso()
    cols = [k for k in TX_FIELDS + ["updated_at", "created_at", "schedule_id"] if k in tx]
    return con.execute(f"INSERT INTO transactions({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                       [tx[k] for k in cols]).lastrowid


def _event(con, loan, kind: str, when: str, due: str | None, aid: int | None, note: str | None) -> int:
    return con.execute("INSERT INTO loan_events(loan_id, kind, date, due_date, account_id, note, created_at) VALUES(?,?,?,?,?,?,?)",
                       (loan["id"], kind, when, due, aid, note, now_iso())).lastrowid


def _link_row(con, loan, eid: int, tid: int, role: str, amount: float) -> None:
    con.execute("INSERT INTO loan_links(loan_id, event_id, transaction_id, role, amount, created_at) VALUES(?,?,?,?,?,?)",
                (loan["id"], eid, tid, role, _r2(amount), now_iso()))


def _common(loan) -> dict:
    return {"ledger_id": loan["ledger_id"] or 1, "source": "loan", "is_demo": loan["is_demo"] or 0}


def _disburse(con, loan, amount: float, when: str, aid: int, rates, note: str | None = None) -> int:
    pay = con.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    cross = pay["currency"] != loan["currency"]
    title = loan["name"] or loan["counterparty"] or ""
    if loan["direction"] == "borrowed":  # loan account → bank
        data = {"kind": "transfer", "account_id": loan["account_id"], "to_account_id": aid, "amount": amount,
                "currency": loan["currency"], "note": note or f"Loan received 借入 · {title}"}
    else:  # bank → receivable
        data = {"kind": "transfer", "account_id": aid, "to_account_id": loan["account_id"], "amount": amount,
                "currency": loan["currency"], "to_amount": amount if cross else None, "note": note or f"Money lent 借出 · {title}"}
    eid = _event(con, loan, "disburse", when, None, aid, note)
    tid = _insert_tx(con, {**_common(loan), **data, "date": when}, rates)
    _link_row(con, loan, eid, tid, "disburse", amount)
    return eid


def _parts(loan, principal, interest, escrow, fee) -> list[tuple[str, float]]:
    return [(r, v) for r, v in (("principal", principal), ("interest", interest), ("escrow", escrow), ("fee", fee)) if v and v > EPS]


def _part_data(con, loan, role: str, amount: float, aid: int, body: dict) -> dict:
    """One transaction of a repayment, in the loan's currency (normalize_tx converts for an account in another one)."""
    title = loan["name"] or loan["counterparty"] or ""
    acct = con.execute("SELECT currency FROM accounts WHERE id=?", (aid,)).fetchone()
    cross = acct and acct["currency"] != loan["currency"]
    lent = loan["direction"] == "lent"
    zh = {"principal": "本金", "interest": "利息", "escrow": "代缴税险", "fee": "费用"}[role]
    note = f"{'Repayment' if lent else 'Loan payment'} {zh} · {role} · {title}"
    if body.get("note"):
        note = f"{body['note']} · {note}"
    if role == "principal":
        if lent:
            return {"kind": "transfer", "account_id": loan["account_id"], "to_account_id": aid, "amount": amount,
                    "currency": loan["currency"], "note": note}
        return {"kind": "transfer", "account_id": aid, "to_account_id": loan["account_id"], "amount": amount, "currency": loan["currency"],
                "to_amount": amount if cross else None, "note": note}
    cat = (body.get(f"{role}_category_id") or (loan["escrow_category_id"] if role == "escrow" else loan["interest_category_id"]))
    if role == "fee" and not body.get("fee_category_id"):
        cat = loan["interest_category_id"] if lent else (_cat(con, "expense", "Bank Fees") or loan["interest_category_id"])
    return {"kind": "income" if lent else "expense", "account_id": aid, "amount": amount, "currency": loan["currency"],
            "category_id": cat, "note": note}


def split(con, loan, total: float | None, when: date, due: str | None, extra: bool = False) -> dict:
    """Default split of a repayment: escrow, then interest (period interest on what is owed, or daily accrual for
    'repay any time' loans; 0 for an extra payment), then principal (capped at what is owed)."""
    out = outstanding(con, loan, when.isoformat() + " 23:59")
    esc = 0.0 if extra or loan["direction"] == "lent" else (loan["escrow"] or 0)
    if extra:
        it = 0.0
    elif loan["freq"] in PERIODS:
        it = _r2(out * period_rate(loan))
    elif (loan["rate"] or 0) > 0:
        last = con.execute("SELECT MAX(date) FROM loan_events WHERE loan_id=? AND kind='payment'", (loan["id"],)).fetchone()[0]
        since = date.fromisoformat((last or loan["start_date"])[:10])
        it = _r2(out * loan["rate"] / 100 * max((when - since).days, 0) / 365)
    else:
        it = 0.0
    if total is None:
        pay = loan["payment"] if loan["freq"] in PERIODS and not extra else None
        pr = _r2(min((pay - it) if pay else out, out))
        total = _r2(max(pr, 0) + it + esc)
    rest = total
    esc = _r2(min(esc, rest)); rest -= esc
    it = _r2(min(it, rest)); rest -= it
    pr = _r2(rest)
    return {"total": _r2(total), "principal": pr, "interest": it, "escrow": esc, "fee": 0.0, "outstanding_before": out,
            "over": _r2(max(pr - out, 0))}


def _pick_due(con, loan, when: date, events=None) -> str | None:
    """The earliest unpaid due date on or before the payment date + half a period (a payment a few days early or late
    still pays that installment)."""
    if loan["freq"] not in PERIODS and loan["freq"] != "once":
        return None
    events = events if events is not None else _events(con, loan["id"])
    paid = _paid_dues(events)
    expected = (loan["payment"] or 0) + (loan["escrow"] or 0)
    slack = 365 // PERIODS.get(loan["freq"], 2) // 2 if loan["freq"] in PERIODS else 30
    for d in due_dates(loan, when + timedelta(days=slack)):
        if not _is_paid(paid.get(d.isoformat(), 0.0), expected, loan["freq"]):
            return d.isoformat()
    return None


def _validate_parts(loan, parts, out: float, tol: float = 0.0):
    pr = sum(v for r, v in parts if r == "principal")
    if pr > out + EPS + tol:
        raise ValidationError(f"Principal {pr:.2f} is more than what is still owed ({out:.2f} {loan['currency']})")
    if not parts:
        raise ValidationError("Nothing to record: enter the amount")


def _resolve(con, loan, body: dict, when: date) -> tuple[list, str | None, bool]:
    extra = body.get("extra") in (True, 1, "1", "true", "yes")
    due = None if extra else (_day(body.get("due_date"), "due_date") if body.get("due_date") else _pick_due(con, loan, when))
    total = _num(body.get("amount"), "amount")
    explicit = any(body.get(k) not in (None, "") for k in ("principal", "interest", "escrow", "fee"))
    if explicit:
        vals = {k: _r2(_num(body.get(k), k) or 0) for k in ("principal", "interest", "escrow", "fee")}
        if total is not None and abs(sum(vals.values()) - total) > EPS:
            raise ValidationError(f"Principal + interest + escrow + fee ({sum(vals.values()):.2f}) must add up to the amount ({total:.2f})")
    else:
        s = split(con, loan, total, when, due, extra)
        vals = {k: s[k] for k in ("principal", "interest", "escrow", "fee")}
    return _parts(loan, **vals), due, extra


def record_payment(db: Database, lid: int, body: dict) -> dict:
    """A repayment, split into linked transactions (all or nothing; dry_run previews it).
    {date?, amount? (total in the loan currency; default = the next expected payment), principal?/interest?/escrow?/
    fee? (explicit split, overrides the default), account_id? (paid from / received into; default the loan's payment
    account), due_date? (installment it pays; default the earliest unpaid one), extra? (extra principal payment, no
    installment), transaction_id? (an existing transaction that IS this payment: it becomes the first part, in place),
    note?, dry_run?}"""
    on = _today(db)
    dry = bool(body.get("dry_run"))
    rates = fx_rates(db)
    con = db.connect()
    try:
        with db.lock:
            con.execute("BEGIN")
            loan = con.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
            if not loan:
                raise NotFound(lid)
            loan = dict(loan)
            if loan["status"] == "closed":
                raise ValidationError("This loan is closed: reopen it first")
            when_d = date.fromisoformat(_day(body.get("date")) or on.isoformat())
            src = None
            if body.get("transaction_id"):
                src = _convertible(con, loan, int(body["transaction_id"]))
                when_d = date.fromisoformat(src["date"][:10])
                body = {**body, "amount": body.get("amount") if body.get("amount") not in (None, "") else src["_loan_amount"],
                        "account_id": src["_pay_account"]}
            aid = int(body.get("account_id") or loan["pay_account_id"] or 0)
            if not aid:
                raise ValidationError("Choose the account the payment is " + ("received into" if loan["direction"] == "lent" else "paid from"))
            if aid == loan["account_id"]:
                raise ValidationError("Choose the bank account, not the loan account")
            if not con.execute("SELECT 1 FROM accounts WHERE id=?", (aid,)).fetchone():
                raise ValidationError("Account not found")
            parts, due, extra = _resolve(con, loan, body, when_d)
            out = outstanding(con, loan)
            if src is not None and src["_principal_in_loan"]:
                out += src["_loan_amount"]  # the transfer being split already reduced what is owed
            _validate_parts(loan, parts, out)
            when = when_d.isoformat() + " " + ((src["date"][11:16] or "12:00") if src else (str(body.get("time") or datetime.now().strftime("%H:%M"))[:5]))
            eid = _event(con, loan, "payment", when, due, aid, body.get("note") or None)
            created = []
            for i, (role, amt) in enumerate(parts):
                data = {**_common(loan), **_part_data(con, loan, role, amt, aid, body), "date": when,
                        "status": src["status"] if src else "posted"}
                if src and i == 0:
                    tid = src["id"]
                    ex = dict(con.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone())
                    keep = {k: ex[k] for k in ("merchant_id", "member_id", "project_id", "tags", "external_id", "raw_text", "source")}
                    ex.update(orig_amount=None, orig_currency=None, tax_amount=None, tip_amount=None, tax_rate=None,
                              to_amount=None, category_id=None, currency=None, reimbursable=0)
                    if src["note"]:
                        data["note"] = src["note"] + " · " + data["note"]
                    tx = normalize_tx(con, {**data, **{k: v for k, v in keep.items() if v is not None}}, rates, existing=ex)
                    tx["updated_at"] = now_iso()
                    cols = [k for k in TX_FIELDS + ["updated_at"] if k in tx]
                    con.execute(f"UPDATE transactions SET {','.join(c + '=?' for c in cols)} WHERE id=?", [tx[k] for k in cols] + [tid])
                else:
                    if src:
                        data.update(merchant_id=src["merchant_id"], member_id=src["member_id"], project_id=src["project_id"])
                    tid = _insert_tx(con, data, rates)
                _link_row(con, loan, eid, tid, role, amt)
                created.append({"role": role, "amount": amt, "transaction_id": tid, "kind": data["kind"], "account_id": data["account_id"],
                                "to_account_id": data.get("to_account_id"), "in_place": bool(src and i == 0)})
            x = _sync(con, lid, on)
            result = {"plan": created, "due_date": due, "extra": extra, "outstanding_after": x["outstanding"] if not dry else _r2(out - sum(v for r, v in parts if r == "principal")),
                      "event_id": eid}
            if dry:
                con.execute("ROLLBACK")
                return {"dry_run": True, **result}
            con.execute("COMMIT")
    except Exception:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return {**result, "loan": get(db, lid)}


def preview_split(db: Database, lid: int, amount=None, when=None, extra=False) -> dict:
    on = _today(db)
    with db.tx() as con:
        loan = con.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not loan:
            raise NotFound(lid)
        loan = dict(loan)
        w = date.fromisoformat(_day(when) or on.isoformat())
        ex = extra in (True, 1, "1", "true", "yes")
        due = None if ex else _pick_due(con, loan, w)
        s = split(con, loan, _num(amount, "amount"), w, due, ex)
        con.rollback()
    return {**s, "due_date": due, "extra": ex, "currency": loan["currency"]}


def _convertible(con, loan, tid: int) -> dict:
    """A transaction that can become (the first part of) a repayment of `loan`: borrowed → an expense from a bank
    account, or a transfer into the loan account; lent → income into an account, or a transfer out of the receivable."""
    t = con.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()
    if not t:
        raise ValidationError(f"Transaction {tid} not found")
    t = dict(t)
    if con.execute("SELECT 1 FROM loan_links WHERE transaction_id=?", (tid,)).fetchone():
        raise ValidationError("This transaction is already linked to a loan")
    if con.execute("SELECT 1 FROM reserve_links WHERE transaction_id=?", (tid,)).fetchone():
        raise ValidationError("This transaction is linked to a reserve: unlink it first")
    if con.execute("SELECT 1 FROM reimb_links WHERE expense_id=? OR receipt_id=?", (tid, tid)).fetchone():
        raise ValidationError("This transaction is linked to reimbursements: unlink them first")
    lent, acct = loan["direction"] == "lent", loan["account_id"]
    t["_principal_in_loan"] = False
    if t["kind"] == "transfer" and ((not lent and t["to_account_id"] == acct) or (lent and t["account_id"] == acct)):
        t["_pay_account"] = t["account_id"] if not lent else t["to_account_id"]
        amt = (t["to_amount"] if t["to_amount"] is not None else t["amount"]) if not lent else t["amount"]
        t["_principal_in_loan"] = t["status"] == "posted"
    elif t["kind"] == ("income" if lent else "expense") and t["account_id"] != acct:
        t["_pay_account"] = t["account_id"]
        if t["orig_currency"] == loan["currency"]:
            amt = t["orig_amount"]
        elif t["currency"] == loan["currency"]:
            amt = t["amount"]
        else:
            raise ValidationError(f"The transaction is in {t['currency']} and the loan in {loan['currency']}: record the payment from the Loan Center instead")
    else:
        raise ValidationError("This transaction can't be a " + ("repayment received (use an income into your account, or a transfer out of the loan account)" if lent
                              else "loan payment (use an expense from your bank account, or a transfer into the loan account)"))
    t["_loan_amount"] = _r2(amt)
    return t


def link(db: Database, lid: int, body: dict) -> dict:
    """Link existing transactions as ONE repayment (or disbursement), without changing them.
    {items: [{transaction_id, role?}], due_date?, extra?}. role defaults: a transfer into/out of the loan account =
    principal (or disburse when it goes the other way); an expense (borrowed) / income (lent) = interest."""
    items = body.get("items") if isinstance(body.get("items"), list) else [body]
    on = _today(db)
    with db.tx() as con:
        loan = con.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not loan:
            raise NotFound(lid)
        loan = dict(loan)
        rows = []
        for it in items:
            tid = int(it.get("transaction_id") or 0)
            t = con.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()
            if not t:
                raise ValidationError(f"Transaction {tid} not found")
            if con.execute("SELECT 1 FROM loan_links WHERE transaction_id=?", (tid,)).fetchone():
                raise ValidationError(f"Transaction {tid} is already linked to a loan")
            role = str(it.get("role") or _role_for(loan, t)).lower()
            if role not in ROLES or role == "writeoff":
                raise ValidationError("role must be principal, interest, escrow, fee or disburse")
            _check_role(loan, t, role)
            amt = (t["to_amount"] if t["kind"] == "transfer" and t["to_account_id"] == loan["account_id"] and t["to_amount"] is not None
                   else t["orig_amount"] if t["orig_currency"] == loan["currency"] else t["amount"])
            if t["kind"] != "transfer" and t["currency"] != loan["currency"] and t["orig_currency"] != loan["currency"]:
                amt = _r2(convert(t["amount"], t["currency"], loan["currency"], fx_rates(db)))
            rows.append((t, role, _r2(amt)))
        if not rows:
            raise ValidationError("Nothing to link")
        kind = "disburse" if all(r == "disburse" for _, r, _ in rows) else "payment"
        if kind == "payment" and any(r == "disburse" for _, r, _ in rows):
            raise ValidationError("Link a disbursement on its own")
        first = min(rows, key=lambda r: r[0]["date"])[0]
        when = first["date"]
        extra = body.get("extra") in (True, 1, "1", "true", "yes")
        due = None if kind == "disburse" or extra else (_day(body.get("due_date")) or _pick_due(con, loan, date.fromisoformat(when[:10])))
        bank = next((t["account_id"] if t["account_id"] != loan["account_id"] else t["to_account_id"] for t, _, _ in rows), None)
        eid = _event(con, loan, kind, when, due, bank, None)
        for t, role, amt in rows:
            _link_row(con, loan, eid, t["id"], role, amt)
        _sync(con, lid, on)
    return get(db, lid)


def _role_for(loan, t) -> str:
    acct, lent = loan["account_id"], loan["direction"] == "lent"
    if t["kind"] == "transfer" and acct in (t["account_id"], t["to_account_id"]):
        into = t["to_account_id"] == acct
        return "disburse" if into == lent else "principal"
    return "interest"


def _check_role(loan, t, role: str) -> None:
    acct, lent = loan["account_id"], loan["direction"] == "lent"
    if role in ("principal", "disburse"):
        into = t["kind"] == "transfer" and t["to_account_id"] == acct
        out_of = t["kind"] == "transfer" and t["account_id"] == acct
        ok = (out_of if lent else into) if role == "principal" else (into if lent else out_of)
        if not ok:
            raise ValidationError(f"Transaction {t['id']}: a {role} must be a transfer " + ("out of" if (role == "principal") == lent else "into")
                                  + " the loan account")
    else:
        want = "income" if lent else "expense"
        if t["kind"] != want or t["account_id"] == acct:
            raise ValidationError(f"Transaction {t['id']}: {role} must be an {want} " + ("into" if lent else "from") + " your bank account")


def unlink(db: Database, link_id: int) -> dict:
    k = db.one("SELECT * FROM loan_links WHERE id=?", (link_id,))
    if not k:
        raise NotFound(link_id)
    with db.tx() as con:
        con.execute("DELETE FROM loan_links WHERE id=?", (link_id,))
        _sync(con, k["loan_id"], _today(db))
    return get(db, k["loan_id"])


def delete_event(db: Database, eid: int, delete_transactions: bool = False) -> dict:
    """Undo a repayment / disbursement: unlink its transactions, or delete them too (delete_transactions)."""
    e = db.one("SELECT * FROM loan_events WHERE id=?", (eid,))
    if not e:
        raise NotFound(eid)
    with db.tx() as con:
        tids = [r[0] for r in con.execute("SELECT transaction_id FROM loan_links WHERE event_id=?", (eid,))]
        con.execute("DELETE FROM loan_links WHERE event_id=?", (eid,))
        con.execute("DELETE FROM loan_events WHERE id=?", (eid,))
        if delete_transactions and tids:
            q = ",".join("?" * len(tids))
            con.execute(f"UPDATE receipts SET transaction_id=NULL WHERE transaction_id IN ({q})", tids)
            con.execute(f"UPDATE schedule_occurrences SET transaction_id=NULL WHERE transaction_id IN ({q})", tids)
            con.execute(f"DELETE FROM transactions WHERE id IN ({q})", tids)
        _sync(con, e["loan_id"], _today(db))
    return get(db, e["loan_id"])


def disburse(db: Database, lid: int, body: dict) -> dict:
    """More money lent / borrowed on the same loan (a top-up or a credit-line draw): {amount, account_id?, date?, note?}."""
    amt = _num(body.get("amount"), "amount", positive=True)
    if not amt:
        raise ValidationError("Enter the amount")
    rates = fx_rates(db)
    with db.tx() as con:
        loan = con.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not loan:
            raise NotFound(lid)
        loan = dict(loan)
        aid = int(body.get("account_id") or loan["pay_account_id"] or 0)
        if not aid or aid == loan["account_id"]:
            raise ValidationError("Choose the bank account")
        when = (_day(body.get("date")) or _today(db).isoformat()) + " " + datetime.now().strftime("%H:%M")
        _disburse(con, loan, amt, when, aid, rates, body.get("note") or None)
        if loan["status"] == "paid_off":
            con.execute("UPDATE loans SET status='active' WHERE id=?", (lid,))
        _sync(con, lid, _today(db))
    return get(db, lid)


def close(db: Database, lid: int, body: dict) -> dict:
    """{action: close|reopen, write_off?: true, category_id?, date?, note?}. Closing with a balance needs write_off:
    lent → the rest becomes an expense from the receivable (a loss, e.g. category Loss); borrowed → income into the
    loan account (debt forgiven). Without a balance it just closes."""
    on = _today(db)
    action = str(body.get("action") or "close").lower()
    rates = fx_rates(db)
    with db.tx() as con:
        loan = con.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not loan:
            raise NotFound(lid)
        loan = dict(loan)
        if action == "reopen":
            con.execute("UPDATE loans SET status='active', closed_at=NULL, closed_reason=NULL, updated_at=? WHERE id=?", (now_iso(), lid))
            _sync(con, lid, on)
        elif action == "close":
            out = outstanding(con, loan)
            if abs(out) > EPS:
                if not body.get("write_off"):
                    raise ValidationError(f"{out:.2f} {loan['currency']} is still owed: record the repayment, or close with a write-off")
                lent = loan["direction"] == "lent"
                cat = int(body["category_id"]) if body.get("category_id") else (_cat(con, "expense", "Loss") if lent else _cat(con, "income", "Other Income"))
                when = (_day(body.get("date")) or on.isoformat()) + " " + datetime.now().strftime("%H:%M")
                eid = _event(con, loan, "writeoff", when, None, None, body.get("note") or None)
                title = loan["name"] or loan["counterparty"] or ""
                tid = _insert_tx(con, {**_common(loan), "kind": "expense" if lent else "income", "account_id": loan["account_id"],
                                       "amount": out, "currency": loan["currency"], "category_id": cat, "date": when,
                                       "note": body.get("note") or (f"Not repaid 坏账 · {title}" if lent else f"Debt forgiven 债务免除 · {title}")}, rates)
                _link_row(con, loan, eid, tid, "writeoff", out)
            con.execute("UPDATE loans SET status='closed', closed_at=?, closed_reason=?, updated_at=? WHERE id=?",
                        (now_iso(), "write_off" if body.get("write_off") else "closed", now_iso(), lid))
            if loan["schedule_id"]:
                con.execute("UPDATE schedules SET enabled=0, updated_at=? WHERE id=?", (now_iso(), loan["schedule_id"]))
        else:
            raise ValidationError("action must be close or reopen")
    return get(db, lid)


# ------------------------------------------------------------------ schedule link
def _template(loan) -> dict:
    title = loan["name"] or loan["counterparty"] or ""
    amt = _r2((loan["payment"] or 0) + (loan["escrow"] or 0))
    if loan["direction"] == "lent":
        return {"kind": "transfer", "account_id": loan["account_id"], "to_account_id": loan["pay_account_id"], "amount": amt,
                "currency": loan["currency"], "note": f"Repayment · {title}", "ledger_id": loan["ledger_id"] or 1}
    return {"kind": "transfer", "account_id": loan["pay_account_id"], "to_account_id": loan["account_id"], "amount": amt,
            "currency": loan["currency"], "note": f"Loan payment · {title}", "ledger_id": loan["ledger_id"] or 1}


def _sync_schedule(con, loan) -> None:
    """Keep the loan's schedule template (shown in Scheduled / upcoming) in line with the loan."""
    con.execute("UPDATE schedules SET template=?, updated_at=? WHERE id=?",
                (json.dumps(_template(loan), ensure_ascii=False), now_iso(), loan["schedule_id"]))


def setup_schedule(db: Database, lid: int, body: dict) -> dict:
    """Create (or replace the settings of) the loan's recurring schedule. {post_mode: post|remind, start_date? (first
    occurrence; default the next unpaid due date), time_of_day?, lead_days?, adopt_schedule_id? (an existing schedule
    for this payment: it is paused and its next date / time are used; its history is kept)}."""
    from . import schedules
    x = get(db, lid)
    if x["closed"]:
        raise ValidationError("This loan is closed")
    if x["freq"] not in PERIODS:
        raise ValidationError("Only a loan with regular payments (monthly, weekly, …) can have a schedule")
    if not x["payment"]:
        raise ValidationError("Set the regular payment first")
    if not x["pay_account_id"]:
        raise ValidationError("Set the account the payments go through first")
    mode = str(body.get("post_mode") or "post").lower()
    if mode not in ("post", "remind"):
        raise ValidationError("post_mode must be post (record each payment automatically) or remind (reminder only)")
    on = _today(db)
    start = _day(body.get("start_date"), "start_date")
    tod = str(body.get("time_of_day") or "09:00")[:5]
    lead = body.get("lead_days")
    adopt = None
    if body.get("adopt_schedule_id"):
        adopt = schedules.get(db, int(body["adopt_schedule_id"]))
        if not adopt:
            raise ValidationError("Schedule to replace not found")
        if adopt.get("loan_id"):
            raise ValidationError("That schedule already belongs to a loan")
        nd = schedules.next_dates(adopt, max(on, schedules._d(adopt["skip_before"]) or on), 1)
        start = start or (nd[0].isoformat() if nd else None)
        tod = body.get("time_of_day") or adopt["time_of_day"]
        lead = lead if lead not in (None, "") else adopt.get("lead_days")
    start = start or max(x["next_due"] or on.isoformat(), on.isoformat())
    loan = dict(x)
    try:
        rule = schedules.normalize_rule({**_rule(loan, start), "time_of_day": tod,
                                         "end_date": x["maturity_date"] if x["maturity_date"] and x["maturity_date"] >= start else None})
    except schedules.ScheduleError as e:
        raise ValidationError(str(e))
    if x["freq"] in ("weekly", "biweekly"):
        rule["weekdays"] = str(date.fromisoformat(start).weekday())
    row = {**rule, "name": f"Loan · {x['label']}"[:60], "template": json.dumps(_template(loan), ensure_ascii=False), "post_mode": mode,
           "variable_amount": 0, "enabled": 1, "lead_days": int(lead) if lead not in (None, "") else None, "updated_at": now_iso(),
           "loan_id": lid}
    with db.tx() as con:
        if x["schedule_id"] and con.execute("SELECT 1 FROM schedules WHERE id=?", (x["schedule_id"],)).fetchone():
            con.execute(f"UPDATE schedules SET {','.join(k + '=?' for k in row)}, skip_before=? WHERE id=?",
                        list(row.values()) + [start, x["schedule_id"]])
            sid = x["schedule_id"]
        else:
            row["created_at"] = now_iso()
            row["skip_before"] = start
            sid = con.execute(f"INSERT INTO schedules({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values())).lastrowid
            con.execute("UPDATE loans SET schedule_id=?, updated_at=? WHERE id=?", (sid, now_iso(), lid))
        if adopt:
            con.execute("UPDATE schedules SET enabled=0, updated_at=? WHERE id=?", (now_iso(), adopt["id"]))
            con.execute("UPDATE loans SET adopted_schedule_id=? WHERE id=?", (adopt["id"], lid))
        if not x["first_due"] or x["first_due"] > start:
            con.execute("UPDATE loans SET first_due=? WHERE id=?", (start, lid))
        con.execute("UPDATE reminders SET status='dismissed', done_reason='loan_scheduled', done_at=?, updated_at=? "
                    "WHERE kind='loan' AND loan_id=? AND status='open'", (now_iso(), now_iso(), lid))
    return get(db, lid)


def remove_schedule(db: Database, lid: int) -> dict:
    """Stop the loan's schedule (generated payments are kept)."""
    x = get(db, lid)
    with db.tx() as con:
        if x["schedule_id"]:
            con.execute("DELETE FROM schedule_occurrences WHERE schedule_id=?", (x["schedule_id"],))
            con.execute("DELETE FROM schedules WHERE id=?", (x["schedule_id"],))
            con.execute("UPDATE loans SET schedule_id=NULL, updated_at=? WHERE id=?", (now_iso(), lid))
    return get(db, lid)


def post_scheduled(db: Database, s: dict, d: date, review: bool) -> dict:
    """schedules._generate for a loan schedule: the occurrence posts the split repayment (idempotent)."""
    lid = s["loan_id"]
    rates = fx_rates(db)
    with db.tx() as con:
        if con.execute("SELECT 1 FROM schedule_occurrences WHERE schedule_id=? AND occurrence_date=?", (s["id"], d.isoformat())).fetchone():
            return {"status": "exists"}
        loan = con.execute("SELECT * FROM loans WHERE id=?", (lid,)).fetchone()
        if not loan or loan["status"] != "active" or not loan["pay_account_id"]:
            con.execute("INSERT INTO schedule_occurrences(schedule_id, occurrence_date, transaction_id) VALUES(?,?,NULL)", (s["id"], d.isoformat()))
            return {"status": "skipped", "warning": "Loan closed or without a payment account: nothing posted"}
        loan = dict(loan)
        when = f"{d.isoformat()} {s['time_of_day']}"
        out = outstanding(con, loan, when)
        if out <= EPS:
            con.execute("INSERT INTO schedule_occurrences(schedule_id, occurrence_date, transaction_id) VALUES(?,?,NULL)", (s["id"], d.isoformat()))
            con.execute("UPDATE schedules SET enabled=0 WHERE id=?", (s["id"],))
            return {"status": "skipped", "warning": "Loan paid off: schedule stopped"}
        due = d.isoformat()
        if _paid_dues(_events(con, lid)).get(due, 0) > EPS:  # already paid by hand
            con.execute("INSERT INTO schedule_occurrences(schedule_id, occurrence_date, transaction_id) VALUES(?,?,NULL)", (s["id"], due))
            return {"status": "exists"}
        sp = split(con, loan, None, d, due)
        parts = _parts(loan, sp["principal"], sp["interest"], sp["escrow"], 0)
        eid = _event(con, loan, "payment", when, due, loan["pay_account_id"], None)
        first, warning = None, None
        for role, amt in parts:
            data = {**_common(loan), **_part_data(con, loan, role, amt, loan["pay_account_id"], {}), "date": when,
                    "status": "review" if review else "posted", "source": "scheduled", "raw_text": f"Scheduled: {s['name']}"}
            try:
                tx = normalize_tx(con, data, rates)
            except ValidationError as e:
                warning = str(e)
                tx = normalize_tx(con, dict(data, status="review"), rates)
            tx["schedule_id"] = s["id"]
            tx["updated_at"] = tx["created_at"] = now_iso()
            cols = [k for k in TX_FIELDS + ["updated_at", "created_at", "schedule_id"] if k in tx]
            tid = con.execute(f"INSERT INTO transactions({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [tx[k] for k in cols]).lastrowid
            _link_row(con, loan, eid, tid, role, amt)
            first = first or tid
        con.execute("INSERT INTO schedule_occurrences(schedule_id, occurrence_date, transaction_id) VALUES(?,?,?)", (s["id"], due, first))
        _sync(con, lid, _today(db))
    return {"transaction_id": first, "status": "review" if review else "posted", "warning": warning}


def expected_flow(db: Database, loan_id: int, d: date) -> tuple[str, float, str] | None:
    """For 'rest of the month': (kind, interest + escrow expected on due date d, currency) — the part of a scheduled
    repayment that is spending (borrowed) or income (lent)."""
    x = get(db, loan_id)
    if x["closed"]:
        return None
    for r in x["projection"]:
        if r["due_date"] == d.isoformat():
            return ("income" if x["direction"] == "lent" else "expense"), _r2(r["interest"] + r["escrow"]), x["currency"]
    return None


# ------------------------------------------------------------------ transactions: badges, suggestions, edits
def annotate(db: Database, rows: list[dict]) -> list[dict]:
    """Transaction rows get `loans`: [{id, role, title, direction}] when they belong to a loan."""
    ids = [r["id"] for r in rows if r.get("id")]
    if not ids:
        return rows
    lk = defaultdict(list)
    for k in db.all(f"""SELECT k.transaction_id, k.role, k.amount, l.id, l.name, l.counterparty, l.direction, l.currency FROM loan_links k
                        JOIN loans l ON l.id=k.loan_id WHERE k.transaction_id IN ({','.join('?' * len(ids))})""", ids):
        lk[k["transaction_id"]].append({"id": k["id"], "role": k["role"], "title": k["name"] or k["counterparty"], "direction": k["direction"],
                                        "amount": k["amount"], "currency": k["currency"]})
    for r in rows:
        if r.get("id") in lk:
            r["loans"] = lk[r["id"]]
    return rows


def suggest(db: Database, transaction_id: int | None = None, loan_id: int | None = None, limit: int = 30) -> dict:
    """From a transaction: the active loans it could be a repayment of (best first, `convert` = can be split in
    place). From a loan: recent unlinked transactions that look like its repayments (same account and about the
    expected amount, or the counterparty's name in the note / merchant)."""
    on = _today(db)
    if transaction_id:
        t = db.one("SELECT t.*, m.name merchant_name FROM transactions t LEFT JOIN merchants m ON m.id=t.merchant_id WHERE t.id=?", (transaction_id,))
        if not t:
            raise NotFound(transaction_id)
        if db.one("SELECT 1 x FROM loan_links WHERE transaction_id=?", (transaction_id,)):
            return {"transaction_id": transaction_id, "items": []}
        out = []
        for x in list_loans(db, {"state": "active"})["items"]:
            sc, why = _score(x, t)
            with db.tx() as con:
                try:
                    _convertible(con, x, t["id"])
                    ok = True
                except ValidationError:
                    ok = False
                linkable = True
                try:
                    _check_role(x, t, _role_for(x, t))
                except ValidationError:
                    linkable = False
            if ok or linkable:
                out.append({"id": x["id"], "label": x["label"], "direction": x["direction"], "currency": x["currency"],
                            "outstanding": x["outstanding"], "next_payment": x["next_payment"], "score": sc, "match": why,
                            "convert": ok, "role": _role_for(x, t)})
        out.sort(key=lambda r: -r["score"])
        return {"transaction_id": transaction_id, "items": [r for r in out if r["score"] > 0 or r["role"] == "principal"][:limit]}
    if loan_id:
        x = get(db, loan_id)
        lo = (on - timedelta(days=75)).isoformat()
        aid, acct = x["pay_account_id"] or -1, x["account_id"]
        kind = "income" if x["direction"] == "lent" else "expense"
        rows = db.all("""SELECT t.*, m.name merchant_name, a.name account_name FROM transactions t LEFT JOIN merchants m ON m.id=t.merchant_id
            LEFT JOIN accounts a ON a.id=t.account_id
            WHERE t.date>=? AND t.id NOT IN (SELECT transaction_id FROM loan_links) AND t.status IN ('posted','review')
              AND ((t.kind=? AND t.account_id!=?) OR (t.kind='transfer' AND (t.account_id=? OR t.to_account_id=?)))
            ORDER BY t.date DESC LIMIT 400""", (lo, kind, acct, acct, acct))
        out = []
        for t in rows:
            sc, why = _score(x, t)
            if sc >= 30:
                out.append({**t, "score": sc, "match": why, "role": _role_for(x, t)})
        out.sort(key=lambda r: (-r["score"], r["date"]))
        return {"loan_id": loan_id, "items": out[:limit]}
    raise ValidationError("Give transaction_id or loan_id")


def _score(x, t) -> tuple[int, list]:
    sc, why = 0, []
    acct = x["account_id"]
    if t["kind"] == "transfer" and acct in (t["account_id"], t["to_account_id"]):
        sc += 60; why.append("loan account")
    if x.get("pay_account_id") and t["account_id"] == x["pay_account_id"]:
        sc += 15; why.append("account")
    exp = ((x.get("next_payment") or {}).get("payment") or (x["payment"] or 0) + (x["escrow"] or 0))
    amt = t["orig_amount"] if t.get("orig_currency") == x["currency"] else t["amount"]
    if exp and amt and abs(amt - exp) <= max(1.0, exp * 0.02):
        sc += 40; why.append("amount")
    hay = " ".join(str(t.get(k) or "") for k in ("note", "merchant_name", "raw_text")).lower()
    for w in {str(x.get("counterparty") or "").lower(), str(x.get("name") or "").lower()}:
        if len(w) >= 3 and w in hay:
            sc += 30; why.append("name")
            break
    return sc, why


def check_tx_update(con, ex: dict, tx: dict) -> None:
    """Called by ledger.update_tx: a linked transaction keeps its kind; a changed amount moves its link."""
    links = con.execute("SELECT * FROM loan_links WHERE transaction_id=?", (ex["id"],)).fetchall()
    if not links:
        return
    if tx["kind"] != ex["kind"]:
        raise ValidationError("This transaction is part of a loan payment: unlink it in the Loan Center before changing its type")
    for k in links:
        loan = con.execute("SELECT * FROM loans WHERE id=?", (k["loan_id"],)).fetchone()
        if k["role"] in ("principal", "disburse") and loan and loan["account_id"] not in (tx.get("account_id"), tx.get("to_account_id")):
            raise ValidationError("This transfer is part of a loan payment: it must stay on the loan account (unlink it first)")
        if abs((tx["amount"] or 0) - (ex["amount"] or 0)) > EPS and loan:
            new = tx["to_amount"] if (tx["kind"] == "transfer" and tx.get("to_account_id") == loan["account_id"] and tx.get("to_amount") is not None) \
                else tx["orig_amount"] if tx.get("orig_currency") == loan["currency"] else tx["amount"]
            con.execute("UPDATE loan_links SET amount=? WHERE id=?", (_r2(new), k["id"]))


# ------------------------------------------------------------------ reminders ('loan' kind)
def reminder_due(x: dict) -> str | None:
    """Loans without a schedule: the next unpaid due date, or the maturity date of a 'repay any time' loan."""
    if x["closed"] or x.get("schedule_id") or not x.get("remind", 1) or x["outstanding"] <= EPS:
        return None
    if x["freq"] in PERIODS or x["freq"] == "once":
        return x["next_due"]
    return x.get("maturity_date")


def sync_reminders(db: Database, on: date | None = None) -> int:
    """One open 'loan' reminder per loan, moved to the next due date as payments come in; done when nothing is due."""
    on = on or _today(db)
    if not db.one("SELECT 1 x FROM loans LIMIT 1"):
        return 0
    n = 0
    items = list_loans(db, {"state": "all"})["items"]
    with db.tx() as con:
        for x in items:
            due = reminder_due(x)
            r = con.execute("SELECT * FROM reminders WHERE kind='loan' AND loan_id=?", (x["id"],)).fetchone()
            amt = (x["next_payment"] or {}).get("payment") if x["freq"] in PERIODS or x["freq"] == "once" else x["outstanding"]
            title = ("↩ " if x["direction"] == "lent" else "") + x["label"]
            if due:
                if not r:
                    con.execute("""INSERT INTO reminders(kind, loan_id, account_id, title, due_date, amount, currency, status, created_at, updated_at)
                        VALUES('loan',?,?,?,?,?,?, 'open', ?, ?)""", (x["id"], x["account_id"], title, due, amt, x["currency"], now_iso(), now_iso()))
                    n += 1
                else:
                    # a new installment (due date moved) or one auto-completed earlier reopens; otherwise keep it in step
                    reopen = r["due_date"] != due or (r["status"] != "open" and r["done_reason"] in ("loan_paid", "loan_scheduled"))
                    if reopen or abs((r["amount"] or 0) - (amt or 0)) > EPS or r["title"] != title:
                        con.execute("UPDATE reminders SET due_date=?, amount=?, title=?, currency=?, updated_at=?"
                                    + (", status='open', done_reason=NULL, done_at=NULL, snooze_until=NULL, notified_at=NULL, transaction_id=NULL" if reopen else "")
                                    + " WHERE id=?", (due, amt, title, x["currency"], now_iso(), r["id"]))
            elif r and r["status"] == "open":
                con.execute("UPDATE reminders SET status='done', done_reason='loan_paid', done_at=?, updated_at=? WHERE id=?",
                            (now_iso(), now_iso(), r["id"]))
                n += 1
    return n


def reminder_info(db: Database, r: dict) -> dict:
    x = db.one("SELECT direction, name, counterparty, freq FROM loans WHERE id=?", (r.get("loan_id"),)) or {}
    return {"loan_direction": x.get("direction"), "loan_freq": x.get("freq")}


# ------------------------------------------------------------------ export
def export_rows(db: Database) -> list[dict]:
    out = []
    for x in list_loans(db, {"state": "all"})["items"]:
        out.append({"id": x["id"], "direction": x["direction"], "name": x["label"], "counterparty": x.get("counterparty"),
                    "account": x.get("account_name"), "currency": x["currency"], "principal": x.get("principal"),
                    "outstanding": x["outstanding"], "rate_pct": x["rate"], "freq": x["freq"], "payment": x.get("payment"),
                    "next_due": x.get("next_due"), "payoff_date": x.get("payoff_date"), "interest_ytd": x["interest_ytd"],
                    "interest_total": x["interest_total"], "state": x["state"]})
    return out
