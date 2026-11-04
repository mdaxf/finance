"""Low balance 余额预警: a bank account's balance over the next N days, and an alert when it may drop below a threshold.

forecast() starts from the account's posted balance at the end of today and adds, day by day up to today + N days:
* posted   transactions already on the account dated after today
* pending  transactions not posted yet (status 'review'); one dated today or earlier counts today
* schedule every occurrence of an enabled schedule that is not generated yet (as month_remaining: skip_before and
           schedule_occurrences honoured; a 'remind only' date still counts while its reminder is open): expenses /
           transfers out subtract, income / refunds / transfers in add. A variable amount without a suggested amount is
           listed but adds 0 (unknown_count). Loan payments are transfer schedules, so the whole payment counts.
* bill     open bills paid from the account (bills.account_id; no account: the setting low_balance_bill_account_id),
           due in the window (overdue: today). A bill with no amount is listed, adds 0. A bill matching a schedule on the
           account (amount within the bill match tolerance, dated within 3 days) is left out: the schedule is the same payment.
* card     credit cards whose statements are paid from the account (accounts.pay_from_account_id): what is left of each
           open card statement reminder (due in the window, overdue: today), the latest closed statement when no reminder
           exists for it yet, and the next statement when its due date is in the window (estimated: the card's balance owed
           today minus the open statements). Left out when a schedule already pays the card from this account.
Amounts in another currency are converted to the account's currency with the stored rates.

Alert (reminders kind 'low_balance', one row per account): accounts with low_bal_threshold set (blank = off) and
low_bal_days (blank = setting low_balance_days, default 14). The reminder is open while the lowest end-of-day balance in
the window is below the threshold: due_date = the first day below it, amount = the lowest balance. It completes by
itself when the forecast is back above ('lb_clear'), is dismissed when the alert is turned off ('lb_off'), and comes back
after either of those when the forecast drops below again. Marked done by hand while still low: it stays done until the
forecast recovers (then a later drop alerts again).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from . import ledger
from .db import Database, now_iso

TYPES = ("checking", "savings", "cash", "ewallet")
DAYS_DEFAULT = 14
THRESHOLD_DEFAULT = 500.0
MAX_DAYS = 120
MATCH_DAYS = 3
EPS = 0.005
_SIGN = {"expense": -1, "income": 1, "refund": 1, "transfer": -1, "adjustment": 1, "buy": -1, "sell": 1, "dividend": 1,
         "invest_fee": -1, "reimbursement": 1}


def _r2(v) -> float:
    return round(float(v or 0), 2) + 0.0


def _d(s) -> date | None:
    if not s:
        return None
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def today(db: Database) -> date:
    from .schedules import local_today
    return local_today(db)


def default_days(db: Database) -> int:
    try:
        v = int(db.get_setting("low_balance_days") or DAYS_DEFAULT)
    except ValueError:
        return DAYS_DEFAULT
    return min(max(v, 1), MAX_DAYS)


def days_for(db: Database, acct: dict) -> int:
    return min(max(int(acct["low_bal_days"]), 1), MAX_DAYS) if acct.get("low_bal_days") else default_days(db)


def bill_account(db: Database) -> int | None:
    v = db.get_setting("low_balance_bill_account_id") or ""
    return int(v) if str(v).isdigit() else None


def _tx_effect(t: dict, aid: int) -> float:
    if t["kind"] == "transfer" and t["to_account_id"] == aid and t["account_id"] != aid:
        return float(t["to_amount"] if t["to_amount"] is not None else t["amount"])
    return _SIGN.get(t["kind"], 0) * float(t["amount"] or 0)


def _tx_title(db: Database, t: dict) -> str:
    if t.get("merchant_id"):
        m = db.one("SELECT name FROM merchants WHERE id=?", (t["merchant_id"],))
        if m:
            return m["name"]
    if t["kind"] == "transfer":
        other = t["to_account_id"] if t["account_id"] == t["_aid"] else t["account_id"]
        a = db.one("SELECT name FROM accounts WHERE id=?", (other,)) or {}
        return ("→ " if t["account_id"] == t["_aid"] else "← ") + (a.get("name") or "transfer")
    return (t.get("note") or t["kind"]).strip()[:60]


def _schedule_events(db: Database, acct: dict, on: date, until: date, rates) -> tuple[list[dict], set[int]]:
    """Occurrences of enabled schedules touching the account; also the card ids a schedule pays from it."""
    from .schedules import _d as sd, iter_occurrences
    aid, cur = acct["id"], acct["currency"]
    out, pays_cards = [], set()
    for s in db.all("SELECT * FROM schedules WHERE enabled=1 ORDER BY id"):
        try:
            tpl = json.loads(s["template"] or "{}")
        except ValueError:
            continue
        kind = tpl.get("kind") or "expense"
        src = int(tpl["account_id"]) if str(tpl.get("account_id") or "").isdigit() else None
        dst = int(tpl["to_account_id"]) if str(tpl.get("to_account_id") or "").isdigit() else None
        if src == aid and kind == "transfer" and dst:
            pays_cards.add(dst)
        if src != aid and not (kind == "transfer" and dst == aid):
            continue
        incoming = kind == "transfer" and dst == aid and src != aid
        sb = sd(s["skip_before"])
        done = {r["occurrence_date"] for r in db.all("SELECT occurrence_date FROM schedule_occurrences WHERE schedule_id=?", (s["id"],))}
        open_rem = set()
        if s["post_mode"] == "remind":  # a remind-only date with its reminder still open: not paid yet
            open_rem = {r["occurrence_date"] for r in db.all(
                "SELECT occurrence_date FROM reminders WHERE kind='schedule' AND schedule_id=? AND status='open'", (s["id"],))}
        tcur = tpl.get("currency") or cur
        for d in iter_occurrences(s, until):
            ds = d.isoformat()
            if d < on or (sb and d < sb) or (ds in done and ds not in open_rem):
                continue
            amt = tpl.get("to_amount") if incoming and tpl.get("to_amount") not in (None, "") else tpl.get("amount")
            known = amt not in (None, "")
            v = 0.0
            if known:
                v = float(amt)
                if not (incoming and tpl.get("to_amount") not in (None, "")):
                    v = ledger.convert(v, tcur, cur, rates)
                v = v if incoming else _SIGN.get(kind, 0) * v
            out.append({"date": ds, "source": "schedule", "title": s["name"], "amount": _r2(v) if known else None,
                        "schedule_id": s["id"], "loan_id": s.get("loan_id"), "remind": s["post_mode"] == "remind"})
    return out, pays_cards


def _bill_events(db: Database, acct: dict, on: date, until: date, rates, sched: list[dict]) -> list[dict]:
    from . import bills
    aid, cur = acct["id"], acct["currency"]
    default = bill_account(db)
    tol = bills.match_tolerance(db)
    out = []
    for b in db.all("SELECT * FROM bills WHERE status='open' ORDER BY id"):
        if (b["account_id"] or default) != aid:
            continue
        due = _d(b["due_date"] or b["issued_date"])
        if not due or due > until:
            continue
        when = max(due, on)
        v = None
        if b["amount"] is not None:
            v = -_r2(ledger.convert(float(b["amount"]), b["currency"] or cur, cur, rates))
            same = [e for e in sched if e["amount"] is not None and e["amount"] < 0
                    and abs(abs(e["amount"]) - abs(v)) <= bills.tolerance_for(abs(v), tol)
                    and abs((_d(e["date"]) - due).days) <= MATCH_DAYS]
            if same:
                continue
        out.append({"date": when.isoformat(), "source": "bill", "title": b["payee"], "amount": v, "bill_id": b["id"],
                    "overdue": due < on, "due_date": due.isoformat()})
    return out


def _card_events(db: Database, acct: dict, on: date, until: date, rates, pays_cards: set[int]) -> list[dict]:
    from . import reminders as rm
    aid, cur = acct["id"], acct["currency"]
    out = []
    for c in db.all("SELECT * FROM accounts WHERE type='credit_card' AND archived=0 AND pay_from_account_id=?", (aid,)):
        if c["id"] in pays_cards:
            continue  # a schedule pays this card from the account: already counted
        conv = (lambda v: ledger.convert(v, c["currency"], cur, rates))
        open_total = 0.0
        stmts = set()
        for r in db.all("SELECT * FROM reminders WHERE kind='card' AND account_id=? AND status='open' ORDER BY due_date", (c["id"],)):
            stmts.add(r["statement_date"])
            amt = r["amount_override"] if r["amount_override"] is not None else r["amount"]
            left = max((amt or 0) - rm.payments_after(db, c["id"], _d(r["statement_date"])), 0)
            open_total += left
            due = _d(r["due_date"])
            if left > EPS and due and due <= until:
                out.append({"date": max(due, on).isoformat(), "source": "card", "title": c["name"], "amount": -_r2(conv(left)),
                            "card_account_id": c["id"], "reminder_id": r["id"], "statement_date": r["statement_date"],
                            "due_date": due.isoformat(), "overdue": due < on})
        if not c["stmt_day"] or not (c["due_day"] or c["due_after_days"]):
            continue
        s1 = rm.last_statement(c["stmt_day"], on)
        due1 = rm.due_date(c, s1)
        if s1.isoformat() not in stmts and due1 and on <= due1 and not db.one(
                "SELECT 1 x FROM reminders WHERE kind='card' AND account_id=? AND statement_date=?", (c["id"], s1.isoformat())):
            left = max(rm.owed_at(db, c["id"], s1) - rm.payments_after(db, c["id"], s1), 0)
            open_total += left
            if left > EPS and due1 <= until:
                out.append({"date": due1.isoformat(), "source": "card", "title": c["name"], "amount": -_r2(conv(left)),
                            "card_account_id": c["id"], "statement_date": s1.isoformat(), "due_date": due1.isoformat()})
        s2 = rm.next_statement(c["stmt_day"], s1)
        due2 = rm.due_date(c, s2)
        if s2 <= until and due2 and due2 <= until:
            est = max(rm.owed_at(db, c["id"], on) - open_total, 0)
            if est > EPS:
                out.append({"date": due2.isoformat(), "source": "card", "title": c["name"], "amount": -_r2(conv(est)),
                            "card_account_id": c["id"], "statement_date": s2.isoformat(), "due_date": due2.isoformat(),
                            "estimated": True})
    return out


def forecast(db: Database, aid: int, days: int | None = None, on: date | None = None, threshold: float | None = None) -> dict:
    acct = db.one("SELECT * FROM accounts WHERE id=?", (aid,))
    if not acct:
        raise KeyError(aid)
    on = on or today(db)
    days = min(max(int(days), 1), MAX_DAYS) if days else days_for(db, acct)
    until = on + timedelta(days=days)
    thr = threshold if threshold is not None else acct.get("low_bal_threshold")
    rates = ledger.fx_rates(db)
    start = ledger.cash_balance(db, aid, until=f"{on.isoformat()} 23:59")
    events: list[dict] = []
    hi = f"{until.isoformat()} 23:59"
    for t in db.all("""SELECT * FROM transactions WHERE (account_id=? OR to_account_id=?) AND date <= ?
                       AND (status!='posted' OR date > ?) ORDER BY date, id""", (aid, aid, hi, f"{on.isoformat()} 23:59")):
        t = dict(t, _aid=aid)
        if t["kind"] != "transfer" and t["account_id"] != aid:
            continue
        posted = t["status"] == "posted"
        events.append({"date": max(_d(t["date"]), on).isoformat(), "source": "posted" if posted else "pending",
                       "title": _tx_title(db, t), "amount": _r2(_tx_effect(t, aid)), "transaction_id": t["id"]})
    sched, pays_cards = _schedule_events(db, acct, on, until, rates)
    events += sched
    events += _bill_events(db, acct, on, until, rates, sched)
    events += _card_events(db, acct, on, until, rates, pays_cards)
    order = {"pending": 0, "posted": 1, "schedule": 2, "bill": 3, "card": 4}
    events.sort(key=lambda e: (e["date"], (e["amount"] or 0) < 0, order[e["source"]]))
    bal, daily, i = start, [], 0
    for n in range(days + 1):
        ds = (on + timedelta(days=n)).isoformat()
        while i < len(events) and events[i]["date"] == ds:
            bal = _r2(bal + (events[i]["amount"] or 0))
            events[i]["balance"] = bal
            i += 1
        daily.append({"date": ds, "balance": bal})
    low = min(daily, key=lambda x: (x["balance"], x["date"]))
    below = next((x for x in daily if x["balance"] < thr - EPS), None) if thr is not None else None
    return {"account_id": aid, "account_name": acct["name"], "currency": acct["currency"], "as_of": on.isoformat(),
            "days": days, "until": until.isoformat(), "threshold": thr, "alert_on": acct.get("low_bal_threshold") is not None,
            "start_balance": _r2(start), "end_balance": daily[-1]["balance"], "lowest": low, "first_below": below,
            "below": below is not None, "unknown_count": sum(1 for e in events if e["amount"] is None),
            "events": events, "daily": daily}


# ---------------------------------------------------------------- the 'low_balance' reminder
def alert_accounts(db: Database) -> list[dict]:
    q = ",".join("?" * len(TYPES))
    return db.all(f"SELECT * FROM accounts WHERE archived=0 AND low_bal_threshold IS NOT NULL AND type IN ({q})", TYPES)


def outlook(db: Database, on: date | None = None) -> list[dict]:
    """Home: the forecast of every account with a low balance alert (daily balances, no event list), lowest first."""
    on = on or today(db)
    out = []
    for a in alert_accounts(db):
        f = forecast(db, a["id"], on=on)
        out.append({k: f[k] for k in ("account_id", "account_name", "currency", "as_of", "days", "until", "threshold",
                                      "start_balance", "end_balance", "lowest", "first_below", "below", "unknown_count", "daily")})
    out.sort(key=lambda x: (not x["below"], x["lowest"]["balance"] - (x["threshold"] or 0), x["account_name"]))
    return out


def sync_reminders(db: Database, on: date | None = None) -> None:
    on = on or today(db)
    live = set()
    for a in alert_accounts(db):
        live.add(a["id"])
        f = forecast(db, a["id"], on=on)
        r = db.one("SELECT * FROM reminders WHERE kind='low_balance' AND account_id=?", (a["id"],))
        with db.tx() as con:
            if f["below"]:
                due, amt = f["first_below"]["date"], f["lowest"]["balance"]
                if not r:
                    con.execute("""INSERT INTO reminders(kind, account_id, title, due_date, amount, minimum, currency, status,
                        created_at, updated_at) VALUES('low_balance',?,?,?,?,?,?,'open',?,?)""",
                                (a["id"], a["name"], due, amt, a["low_bal_threshold"], a["currency"], now_iso(), now_iso()))
                elif r["status"] == "open":
                    if (r["due_date"], r["amount"], r["minimum"], r["title"], r["currency"]) != (due, amt, a["low_bal_threshold"], a["name"], a["currency"]):
                        con.execute("UPDATE reminders SET due_date=?, amount=?, minimum=?, title=?, currency=?, updated_at=? WHERE id=?",
                                    (due, amt, a["low_bal_threshold"], a["name"], a["currency"], now_iso(), r["id"]))
                elif r["done_reason"] in ("lb_clear", "lb_off"):  # a new drop after it had recovered: alert again
                    con.execute("""UPDATE reminders SET status='open', done_reason=NULL, done_at=NULL, notified_at=NULL, snooze_until=NULL,
                        due_date=?, amount=?, minimum=?, title=?, currency=?, updated_at=? WHERE id=?""",
                                (due, amt, a["low_bal_threshold"], a["name"], a["currency"], now_iso(), r["id"]))
            elif r and (r["status"] == "open" or r["done_reason"] not in ("lb_clear", "lb_off")):
                con.execute("UPDATE reminders SET status='done', done_reason='lb_clear', done_at=CASE WHEN status='open' "
                            "THEN ? ELSE done_at END, amount=?, updated_at=? WHERE id=?",
                            (now_iso(), f["lowest"]["balance"], now_iso(), r["id"]))
    with db.tx() as con:
        for r in con.execute("SELECT id, account_id FROM reminders WHERE kind='low_balance' AND status='open'").fetchall():
            if r["account_id"] not in live:
                con.execute("UPDATE reminders SET status='dismissed', done_reason='lb_off', done_at=?, updated_at=? WHERE id=?",
                            (now_iso(), now_iso(), r["id"]))


def reminder_info(db: Database, r: dict) -> dict:
    return {"threshold": r.get("minimum"), "lowest": r.get("amount"), "remaining": None, "statement_amount": r.get("amount"),
            "paid": None, "minimum": None}


def reminder_text(r: dict, zh: bool, tw: bool = False) -> str:
    """One line for a low balance reminder (Telegram / email / iPhone notify); tw = Traditional Chinese (emails)."""
    from .ingest import fmt_money
    low, thr, d = fmt_money(r.get("lowest"), r["currency"]), fmt_money(r.get("threshold"), r["currency"]), _d(r["due_date"])
    when = f"{d.month}/{d.day}" if d else ""
    if zh:
        s = f"{r.get('title') or ''} · 余额可能降至 {low}（{when} 起低于 {thr}）"
        return s.replace("余额可能降至", "餘額可能降至").replace("起低于", "起低於") if tw else s
    return f"{r.get('title') or ''} · may drop to {low} (below {thr} from {when})"
