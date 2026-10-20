"""Scheduled transactions 周期记账: recurrence rules, the idempotent runner and helpers.

Weekdays are 0=Monday … 6=Sunday. Occurrences are calendar dates in the user's time zone (setting `timezone`,
default America/Los_Angeles); the generated transaction is dated `<date> <time_of_day>` in local wall-clock time,
so daylight-saving changes never move or duplicate an occurrence.
Idempotency: every generated occurrence is recorded in schedule_occurrences (PRIMARY KEY schedule_id+date) in the
same database transaction as the new transaction, so re-runs never duplicate — and an occurrence whose transaction
you deleted is not re-created.
"""
from __future__ import annotations

import calendar
import json
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import ledger
from .db import Database, now_iso

log = logging.getLogger("finance.schedules")

FREQS = ("once", "daily", "weekly", "monthly", "quarterly", "yearly")
TEMPLATE_FIELDS = ["kind", "amount", "currency", "account_id", "to_account_id", "to_amount", "category_id", "merchant",
                   "member_id", "project_id", "ledger_id", "tags", "note", "adj_reason", "ticker", "shares", "price", "fee"]
DEFAULT_TZ = "America/Los_Angeles"
WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class ScheduleError(ValueError):
    pass


def _d(s) -> date | None:
    if s in (None, ""):
        return None
    if isinstance(s, date):
        return s
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ScheduleError(f"Invalid date: {s}")


def tz(db: Database) -> ZoneInfo:
    try:
        return ZoneInfo(db.get_setting("timezone") or DEFAULT_TZ)
    except Exception:  # noqa: BLE001
        return ZoneInfo(DEFAULT_TZ)


def local_today(db: Database, now: datetime | None = None) -> date:
    return (now.astimezone(tz(db)) if now else datetime.now(tz(db))).date()


# ---------------------------------------------------------------- recurrence
def _add_months(y: int, m: int, n: int) -> tuple[int, int]:
    k = y * 12 + (m - 1) + n
    return k // 12, k % 12 + 1


def _day_in_month(y: int, m: int, day: int) -> date:
    """day 1..31 clamped to the month's length; -1 (or 0) = last day of the month."""
    last = calendar.monthrange(y, m)[1]
    return date(y, m, last if day is None or day <= 0 or day > last else day)


def _nth_weekday(y: int, m: int, nth: int, wd: int) -> date | None:
    """nth 1..5 (5 = fifth, skipped when the month has none) or -1 = last <wd> of the month."""
    if nth == -1:
        d = date(y, m, calendar.monthrange(y, m)[1])
        return d - timedelta(days=(d.weekday() - wd) % 7)
    d = date(y, m, 1)
    d += timedelta(days=(wd - d.weekday()) % 7) + timedelta(weeks=nth - 1)
    return d if d.month == m else None


def normalize_rule(r: dict) -> dict:
    """Validate and fill a recurrence rule (dict with the schedule columns)."""
    freq = (r.get("freq") or "monthly").lower()
    if freq not in FREQS:
        raise ScheduleError(f"freq must be one of {FREQS}")
    start = _d(r.get("start_date"))
    if not start:
        raise ScheduleError("start_date is required")
    end = _d(r.get("end_date"))
    if end and end < start:
        raise ScheduleError("end_date is before start_date")
    try:
        interval = max(1, int(r.get("interval") or 1))
        max_count = int(r["max_count"]) if r.get("max_count") not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        raise ScheduleError("interval / max_count must be whole numbers")
    if max_count is not None and max_count < 1:
        raise ScheduleError("max_count must be at least 1")
    wds = r.get("weekdays")
    if isinstance(wds, str):
        wds = [int(x) for x in wds.split(",") if x.strip() != ""]
    wds = sorted({int(x) for x in (wds or [])})
    if any(w < 0 or w > 6 for w in wds):
        raise ScheduleError("weekdays are 0 (Mon) … 6 (Sun)")
    if freq == "weekly" and not wds:
        wds = [start.weekday()]
    mode = (r.get("month_mode") or "day").lower()
    if mode not in ("day", "nth"):
        raise ScheduleError("month_mode must be 'day' or 'nth'")
    month_day = r.get("month_day")
    month_day = int(month_day) if month_day not in (None, "") else start.day
    if month_day < -1 or month_day > 31 or month_day == 0:
        raise ScheduleError("month_day must be 1–31 or -1 (last day)")
    nth = int(r["nth"]) if r.get("nth") not in (None, "") else (-1 if start.day > 28 else (start.day - 1) // 7 + 1)
    if nth not in (1, 2, 3, 4, 5, -1):
        raise ScheduleError("nth must be 1–5 or -1 (last)")
    nwd = int(r["nth_weekday"]) if r.get("nth_weekday") not in (None, "") else start.weekday()
    if nwd < 0 or nwd > 6:
        raise ScheduleError("nth_weekday is 0 (Mon) … 6 (Sun)")
    tod = str(r.get("time_of_day") or "09:00")[:5]
    try:
        datetime.strptime(tod, "%H:%M")
    except ValueError:
        raise ScheduleError("time_of_day must be HH:MM")
    return {"freq": freq, "interval": interval, "weekdays": ",".join(map(str, wds)), "month_mode": mode,
            "month_day": month_day, "nth": nth, "nth_weekday": nwd, "start_date": start.isoformat(),
            "end_date": end.isoformat() if end else None, "max_count": max_count, "time_of_day": tod}


def iter_occurrences(rule: dict, until: date, limit: int = 100000):
    """All occurrence dates from start_date up to `until` (inclusive), honouring end_date and max_count."""
    r = normalize_rule(rule)
    start, end = _d(r["start_date"]), _d(r["end_date"])
    stop = min(until, end) if end else until
    n = 0

    def ok(d):
        return d is not None and start <= d <= stop

    freq, iv = r["freq"], r["interval"]
    if freq == "once":
        if start <= stop and (r["max_count"] or 1) >= 1:
            yield start
        return
    if freq == "daily":
        d = start
        while d <= stop and n < limit:
            yield d
            n += 1
            if r["max_count"] and n >= r["max_count"]:
                return
            d += timedelta(days=iv)
        return
    if freq == "weekly":
        wds = [int(x) for x in r["weekdays"].split(",") if x != ""]
        week0 = start - timedelta(days=start.weekday())
        w = 0
        while week0 + timedelta(weeks=w) <= stop and n < limit:
            for wd in wds:
                d = week0 + timedelta(weeks=w, days=wd)
                if ok(d):
                    yield d
                    n += 1
                    if r["max_count"] and n >= r["max_count"]:
                        return
            w += iv
        return
    step = {"monthly": iv, "quarterly": 3 * iv, "yearly": 12 * iv}[freq]
    k = 0
    while n < limit:
        y, m = _add_months(start.year, start.month, k)
        if date(y, m, 1) > stop:
            return
        d = _day_in_month(y, m, r["month_day"]) if r["month_mode"] == "day" else _nth_weekday(y, m, r["nth"], r["nth_weekday"])
        if ok(d):
            yield d
            n += 1
            if r["max_count"] and n >= r["max_count"]:
                return
        k += step


def next_dates(rule: dict, after: date, count: int = 5, horizon_years: int = 30) -> list[date]:
    """The next `count` occurrence dates on or after `after`."""
    out = []
    for d in iter_occurrences(rule, after + timedelta(days=366 * horizon_years)):
        if d >= after:
            out.append(d)
            if len(out) >= count:
                break
    return out


def describe(rule: dict) -> str:
    r = normalize_rule(rule)
    iv, f = r["interval"], r["freq"]
    every = lambda unit: f"Every {iv} {unit}s" if iv > 1 else f"Every {unit}"
    if f == "once":
        s = f"Once on {r['start_date']}"
    elif f == "daily":
        s = every("day")
    elif f == "weekly":
        s = every("week") + " on " + ", ".join(WD[int(x)] for x in r["weekdays"].split(","))
    else:
        unit = {"monthly": "month", "quarterly": "quarter", "yearly": "year"}[f]
        if r["month_mode"] == "day":
            day = "the last day" if r["month_day"] == -1 else f"day {r['month_day']}"
        else:
            day = ("the last " if r["nth"] == -1 else f"the {['', '1st', '2nd', '3rd', '4th', '5th'][r['nth']]} ") + WD[r["nth_weekday"]]
        if f == "yearly":
            day += " of " + calendar.month_abbr[_d(r["start_date"]).month]
        s = every(unit) + " on " + day
    s += f" at {r['time_of_day']}"
    if r["end_date"]:
        s += f", until {r['end_date']}"
    if r["max_count"]:
        s += f", {r['max_count']} times"
    return s


# ---------------------------------------------------------------- storage
def _template(data: dict) -> dict:
    t = {k: data.get(k) for k in TEMPLATE_FIELDS if data.get(k) not in (None, "")}
    t["kind"] = t.get("kind") or "expense"
    if t["kind"] not in ledger.KINDS:
        raise ScheduleError(f"kind must be one of {ledger.KINDS}")
    if t["kind"] != "transfer":
        t.pop("to_account_id", None); t.pop("to_amount", None)
    if t["kind"] != "adjustment":
        t.pop("adj_reason", None)
    if isinstance(t.get("tags"), list):
        t["tags"] = ",".join(t["tags"])
    return t


def _validate_template(db: Database, t: dict, variable: bool):
    """Dry-validate the template by normalizing it like a real transaction (nothing is written)."""
    rates = ledger.fx_rates(db)
    probe = dict(t, date=date.today().isoformat(), status="review" if variable and not t.get("amount") else "posted")
    with db.tx() as con:
        ledger.normalize_tx(con, probe, rates)
        con.rollback()


def get(db: Database, sid: int, today: date | None = None) -> dict | None:
    s = db.one("SELECT * FROM schedules WHERE id=?", (sid,))
    return decorate(db, s, today) if s else None


def decorate(db: Database, s: dict, today: date | None = None) -> dict:
    today = today or local_today(db)
    s["template"] = json.loads(s["template"] or "{}") if isinstance(s["template"], str) else s["template"]
    s["weekdays_list"] = [int(x) for x in (s.get("weekdays") or "").split(",") if x != ""]
    s["description"] = describe(s)
    nxt = next_dates(s, max(today, _d(s["skip_before"]) or today), 5) if s["enabled"] else []
    done = {r["occurrence_date"] for r in db.all("SELECT occurrence_date FROM schedule_occurrences WHERE schedule_id=?", (s["id"],))}
    s["next_dates"] = [f"{d.isoformat()} {s['time_of_day']}" for d in nxt if d.isoformat() not in done][:5]
    st = db.one("SELECT COUNT(*) n, MAX(occurrence_date) last FROM schedule_occurrences WHERE schedule_id=?", (s["id"],))
    s["generated_count"], s["last_generated"] = st["n"], st["last"]
    t = s["template"]
    acc = {a["id"]: a for a in db.all("SELECT id, name, currency FROM accounts")}
    s["account_name"] = (acc.get(int(t["account_id"])) or {}).get("name") if t.get("account_id") else None
    s["to_account_name"] = (acc.get(int(t["to_account_id"])) or {}).get("name") if t.get("to_account_id") else None
    s["currency"] = t.get("currency") or (acc.get(int(t["account_id"])) or {}).get("currency") if t.get("account_id") else t.get("currency")
    s["finished"] = bool(s["enabled"]) and not nxt
    return s


def save(db: Database, data: dict, sid: int | None = None) -> dict:
    ex = db.one("SELECT * FROM schedules WHERE id=?", (sid,)) if sid else None
    if sid and not ex:
        raise KeyError(sid)
    merged = dict(ex or {})
    if ex:
        merged["template"] = json.loads(ex["template"])
    merged.update({k: v for k, v in data.items() if k != "template"})
    tpl = _template(data["template"]) if "template" in data else merged.get("template")
    if not tpl:
        raise ScheduleError("template (the transaction to create) is required")
    rule = normalize_rule(merged)
    post_mode = (merged.get("post_mode") or "post").lower()
    if post_mode not in ("post", "review", "remind"):
        raise ScheduleError("post_mode must be 'post', 'review' or 'remind' (reminder only, no transaction)")
    variable = 1 if merged.get("variable_amount") in (1, True, "1", "true", "on") else 0
    try:
        _validate_template(db, tpl, bool(variable))
    except ledger.ValidationError as e:
        raise ScheduleError(str(e))
    name = (merged.get("name") or "").strip() or _auto_name(db, tpl)
    ld = merged.get("lead_days")
    try:
        ld = None if ld in (None, "") else int(ld)
    except (TypeError, ValueError):
        raise ScheduleError("lead_days must be a whole number")
    if ld is not None and not 0 <= ld <= 365:
        raise ScheduleError("lead_days must be between 0 and 365")
    row = {"lead_days": ld, **rule, "name": name, "template": json.dumps(tpl, ensure_ascii=False), "post_mode": post_mode,
           "variable_amount": variable, "enabled": 0 if merged.get("enabled") in (0, False, "0") else 1,
           "updated_at": now_iso()}
    with db.tx() as con:
        if ex:
            con.execute(f"UPDATE schedules SET {','.join(k + '=?' for k in row)} WHERE id=?", list(row.values()) + [sid])
        else:
            row["created_at"] = now_iso()
            sid = con.execute(f"INSERT INTO schedules({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values())).lastrowid
    return get(db, sid)


def _auto_name(db: Database, t: dict) -> str:
    bits = [t.get("merchant") or t.get("note") or ""]
    if not bits[0] and t.get("category_id"):
        c = db.one("SELECT name FROM categories WHERE id=?", (t["category_id"],))
        bits = [c["name"] if c else ""]
    if not bits[0] and t.get("ticker"):
        bits = [f"{t['kind']} {t['ticker']}"]
    return (bits[0] or t["kind"].replace("_", " ").title())[:60]


def set_enabled(db: Database, sid: int, enabled: bool, today: date | None = None) -> dict:
    """Pause / resume. Resuming skips the dates missed while paused (no back-fill)."""
    today = today or local_today(db)
    with db.tx() as con:
        con.execute("UPDATE schedules SET enabled=?, skip_before=COALESCE(?, skip_before), updated_at=? WHERE id=?",
                    (1 if enabled else 0, today.isoformat() if enabled else None, now_iso(), sid))
    return get(db, sid)


def delete(db: Database, sid: int, keep_history: bool = True) -> dict:
    with db.tx() as con:
        n = 0
        if not keep_history:
            n = con.execute("DELETE FROM transactions WHERE schedule_id=?", (sid,)).rowcount
        con.execute("DELETE FROM schedule_occurrences WHERE schedule_id=?", (sid,))
        con.execute("DELETE FROM schedules WHERE id=?", (sid,))
    return {"ok": True, "deleted_transactions": n, "kept_history": keep_history}


def from_transaction(db: Database, tid: int) -> dict:
    """A schedule template pre-filled from an existing transaction ('Make recurring')."""
    t = ledger.get_tx(db, tid)
    if not t:
        raise KeyError(tid)
    tpl = {k: t.get(k) for k in TEMPLATE_FIELDS if t.get(k) not in (None, "")}
    if t.get("orig_currency"):
        tpl["amount"], tpl["currency"] = t["orig_amount"], t["orig_currency"]
    tpl["merchant"] = t.get("merchant_name") or None
    d = _d(t["date"])
    y, m = _add_months(d.year, d.month, 1)
    # first occurrence one month after the original, so the existing transaction is not duplicated
    return {"template": {k: v for k, v in tpl.items() if v not in (None, "")}, "freq": "monthly", "interval": 1,
            "month_mode": "day", "month_day": d.day, "start_date": _day_in_month(y, m, d.day).isoformat(),
            "time_of_day": t["date"][11:16] or "09:00", "name": _auto_name(db, {**tpl, "kind": t["kind"]}),
            "source_transaction_id": tid}


# ---------------------------------------------------------------- runner
def upcoming(db: Database, days: int = 30, today: date | None = None, limit: int = 200) -> list[dict]:
    today = today or local_today(db)
    until = today + timedelta(days=days)
    out = []
    for s in db.all("SELECT * FROM schedules WHERE enabled=1"):
        s = decorate(db, s, today)
        done = {r["occurrence_date"] for r in db.all("SELECT occurrence_date FROM schedule_occurrences WHERE schedule_id=?", (s["id"],))}
        sb = _d(s["skip_before"]) or today
        for d in iter_occurrences(s, until):
            if d >= max(today, sb) and d.isoformat() not in done:
                t = s["template"]
                out.append({"date": f"{d.isoformat()} {s['time_of_day']}", "schedule_id": s["id"], "name": s["name"],
                            "kind": t.get("kind"), "amount": t.get("amount"), "currency": s["currency"],
                            "account_name": s["account_name"], "to_account_name": s["to_account_name"],
                            "post_mode": "review" if s["variable_amount"] else s["post_mode"],
                            "variable_amount": s["variable_amount"]})
    out.sort(key=lambda x: (x["date"], x["schedule_id"]))
    return out[:limit]


def run(db: Database, as_of: date | str | None = None, dry_run: bool = False, trigger: str = "manual",
        catchup_days: int | None = None) -> dict:
    """Generate every occurrence due on or before `as_of` (default: today in the user's time zone).
    Occurrences older than the catch-up window (setting schedule_catchup_days, default 90) are skipped and counted."""
    as_of = _d(as_of) or local_today(db)
    cap = int(catchup_days if catchup_days is not None else (db.get_setting("schedule_catchup_days") or 90))
    oldest = as_of - timedelta(days=cap)
    generated, errors, items = 0, [], []
    to_review = skipped_old = 0
    for s in db.all("SELECT * FROM schedules WHERE enabled=1 ORDER BY id"):
        try:
            tpl = json.loads(s["template"])
            done = {r["occurrence_date"] for r in db.all("SELECT occurrence_date FROM schedule_occurrences WHERE schedule_id=?", (s["id"],))}
            sb = _d(s["skip_before"])
            # remind-only schedules surface ahead of time (reminder lead days) so the reminder is useful
            horizon = as_of + timedelta(days=s["lead_days"] if s["lead_days"] is not None else int(db.get_setting("reminder_lead_days") or 10)) if s["post_mode"] == "remind" else as_of
            for d in iter_occurrences(s, horizon):
                ds = d.isoformat()
                if ds in done or (sb and d < sb):
                    continue
                if d < oldest:
                    skipped_old += 1
                    continue
                review = bool(s["variable_amount"]) or s["post_mode"] == "review"
                remind = s["post_mode"] == "remind"
                item = {"schedule_id": s["id"], "name": s["name"], "date": f"{ds} {s['time_of_day']}",
                        "status": "reminder" if remind else "review" if review else "posted"}
                if not dry_run:
                    item.update(_remind(db, s, tpl, d) if remind else _generate(db, s, tpl, d, review))
                    if item.get("warning"):
                        errors.append({"schedule_id": s["id"], "date": ds, "error": item["warning"]})
                items.append(item)
                generated += 1
                to_review += item["status"] == "review"
        except Exception as e:  # noqa: BLE001 - one broken schedule must not stop the others
            log.exception("schedule %s failed", s["id"])
            errors.append({"schedule_id": s["id"], "error": str(e)})
    summary = (f"{'Would generate' if dry_run else 'Generated'} {generated} transaction(s) up to {as_of}"
               + (f" ({to_review} to review)" if to_review else "")
               + (f"; skipped {skipped_old} older than {cap} days" if skipped_old else "")
               + (f"; {len(errors)} error(s)" if errors else ""))
    if not dry_run:
        with db.tx() as con:
            con.execute("INSERT INTO schedule_runs(time,as_of,trigger,dry_run,generated,to_review,skipped_old,errors,summary) "
                        "VALUES(?,?,?,?,?,?,?,?,?)", (now_iso(), as_of.isoformat(), trigger, 0, generated, to_review,
                                                      skipped_old, json.dumps(errors, ensure_ascii=False), summary))
            if trigger in ("daily", "startup"):
                con.execute("INSERT OR REPLACE INTO settings VALUES('schedule_last_auto_run',?)", (as_of.isoformat(),))
    return {"as_of": as_of.isoformat(), "dry_run": dry_run, "generated": generated, "to_review": to_review,
            "skipped_old": skipped_old, "catchup_days": cap, "errors": errors, "items": items, "summary": summary}


def _remind(db: Database, s: dict, tpl: dict, d: date) -> dict:
    """'Remind only' schedules: a Reminder Center item instead of a transaction (idempotent like _generate)."""
    from .reminders import create_for_schedule
    with db.tx() as con:
        if con.execute("SELECT 1 FROM schedule_occurrences WHERE schedule_id=? AND occurrence_date=?", (s["id"], d.isoformat())).fetchone():
            return {"status": "exists"}
        rid = create_for_schedule(con, s, tpl, d)
        con.execute("INSERT INTO schedule_occurrences(schedule_id, occurrence_date, transaction_id) VALUES(?,?,NULL)",
                    (s["id"], d.isoformat()))
    return {"reminder_id": rid, "status": "reminder"}


def _generate(db: Database, s: dict, tpl: dict, d: date, review: bool) -> dict:
    """Create one occurrence + its idempotency row atomically."""
    if s.get("loan_id"):  # Loan Center: the occurrence posts the split repayment (principal transfer + interest …)
        from .loans import post_scheduled
        return post_scheduled(db, s, d, review)
    rates = ledger.fx_rates(db)
    data = dict(tpl, date=f"{d.isoformat()} {s['time_of_day']}", source="scheduled",
                status="review" if review else "posted", raw_text=f"Scheduled: {s['name']}")
    if s["variable_amount"]:
        data["note"] = ((tpl.get("note") or "") + " · confirm the amount 请确认金额").strip(" ·")
    warning = None
    with db.tx() as con:
        if con.execute("SELECT 1 FROM schedule_occurrences WHERE schedule_id=? AND occurrence_date=?", (s["id"], d.isoformat())).fetchone():
            return {"status": "exists"}
        try:
            tx = ledger.normalize_tx(con, data, rates)
        except ledger.ValidationError as e:  # e.g. account archived/deleted: put it in To review instead of failing
            warning = str(e)
            tx = ledger.normalize_tx(con, dict(data, status="review"), rates)
        tx["schedule_id"] = s["id"]
        tx["updated_at"] = tx["created_at"] = now_iso()
        cols = [k for k in ledger.TX_FIELDS + ["updated_at", "created_at", "schedule_id"] if k in tx]
        tid = con.execute(f"INSERT INTO transactions({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                          [tx[k] for k in cols]).lastrowid
        con.execute("INSERT INTO schedule_occurrences(schedule_id, occurrence_date, transaction_id) VALUES(?,?,?)",
                    (s["id"], d.isoformat(), tid))
    return {"transaction_id": tid, "status": tx["status"], "warning": warning}


def runs(db: Database, limit: int = 30) -> list[dict]:
    out = db.all("SELECT * FROM schedule_runs ORDER BY id DESC LIMIT ?", (limit,))
    for r in out:
        r["errors"] = json.loads(r["errors"] or "[]")
    return out


def auto_run_due(db: Database, now: datetime | None = None) -> bool:
    """Daily trigger for the background loop: true once per local day after the configured time (default 00:15)."""
    zone = tz(db)
    now = (now.astimezone(zone) if now else datetime.now(zone))
    hh, mm = (db.get_setting("schedule_run_time") or "00:15").split(":")[:2]
    if (now.hour, now.minute) < (int(hh), int(mm)):
        return False
    return db.get_setting("schedule_last_auto_run") != now.date().isoformat()


# ---------------------------------------------------------------- rest of the month (home KPI cards)
def month_remaining(db: Database, month: str | None = None, ledger_id: int | None = None,
                    today: date | None = None) -> dict:
    """Scheduled income / expense still to come in `month` (YYYY-MM), in the base currency.

    Counts every occurrence of an ENABLED schedule whose transaction kind is 'expense' or 'income' (transfers and every
    other kind are excluded) dated from max(today, 1st of the month) through the month's last day, that is not
    generated yet (no schedule_occurrences row; that also skips the dates before a resumed schedule's skip_before) and
    is within the schedule's start/end date and max count. 'Remind only' schedules count too (bills you will pay), unless
    that date's reminder was already marked paid / done / dismissed. Variable-amount schedules count their suggested amount;
    without one they are listed but add 0. Same stats rules as the KPIs: a reimbursable expense counts 0 and a
    reimbursement (its own kind) is never income. Past months have nothing remaining."""
    today = today or local_today(db)
    month = month or today.strftime("%Y-%m")
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = _day_in_month(y, m, 31)
    lo = max(today, first)
    base = db.get_setting("base_currency", "USD")
    out = {"month": f"{y}-{m:02d}", "from": lo.isoformat(), "to": last.isoformat(), "currency": base,
           "income": 0.0, "expense": 0.0, "net": 0.0, "income_count": 0, "expense_count": 0, "count": 0,
           "unknown_amount_count": 0, "items": []}
    if lo > last:
        return out
    rates = ledger.fx_rates(db)
    accts = {a["id"]: a for a in db.all("SELECT id, name, currency FROM accounts")}
    default_ledger = int(db.get_setting("default_ledger_id", "1") or 1)
    for s in db.all("SELECT * FROM schedules WHERE enabled=1 ORDER BY id"):
        try:
            tpl = json.loads(s["template"] or "{}")
        except ValueError:
            continue
        kind = tpl.get("kind") or "expense"
        if s.get("loan_id"):
            loan_kind = "income" if (db.one("SELECT direction FROM loans WHERE id=?", (s["loan_id"],)) or {}).get("direction") == "lent" else "expense"
            if ledger_id and int(tpl.get("ledger_id") or default_ledger) != int(ledger_id):
                continue
            _loan_remaining(db, s, tpl, loan_kind, lo, last, base, rates, out)
            continue
        if kind not in ("expense", "income"):
            continue
        if ledger_id and int(tpl.get("ledger_id") or default_ledger) != int(ledger_id):
            continue
        sb = _d(s["skip_before"])
        done = {r["occurrence_date"] for r in db.all("SELECT occurrence_date FROM schedule_occurrences WHERE schedule_id=?", (s["id"],))}
        remind = s["post_mode"] == "remind"
        open_rem = set()
        if remind:  # reminder created ahead of time but not paid yet: the bill is still to come
            open_rem = {r["occurrence_date"] for r in db.all(
                "SELECT occurrence_date FROM reminders WHERE kind='schedule' AND schedule_id=? AND status='open'", (s["id"],))}
        acct = accts.get(int(tpl["account_id"])) if str(tpl.get("account_id") or "").isdigit() else None
        cur = tpl.get("currency") or (acct or {}).get("currency") or base
        for d in iter_occurrences(s, last):
            ds = d.isoformat()
            if d < lo or (sb and d < sb):
                continue
            if ds in done and not (remind and ds in open_rem):
                continue
            amt = tpl.get("amount")
            known = amt not in (None, "")
            v = float(amt) if known else 0.0
            if kind == "expense" and tpl.get("reimbursable"):
                v = 0.0  # stats rule: reimbursable expenses count 0 while open
            vb = round(ledger.convert(v, cur, base, rates), 2)
            out[kind] += vb
            out[f"{kind}_count"] += 1
            out["unknown_amount_count"] += 0 if known else 1
            out["items"].append({"date": f"{ds} {s['time_of_day']}", "schedule_id": s["id"], "name": s["name"], "kind": kind,
                                 "amount": v if known else None, "currency": cur, "amount_base": vb if known else None,
                                 "post_mode": s["post_mode"], "variable_amount": bool(s["variable_amount"]),
                                 "account_name": (acct or {}).get("name")})
    out["items"].sort(key=lambda x: (x["date"], x["schedule_id"]))
    out["income"], out["expense"] = round(out["income"], 2), round(out["expense"], 2)
    out["net"] = round(out["income"] - out["expense"], 2)
    out["count"] = out["income_count"] + out["expense_count"]
    return out


def _loan_remaining(db: Database, s: dict, tpl: dict, kind: str, lo: date, last: date, base: str, rates, out: dict) -> None:
    """A loan schedule's occurrence: only its interest (+ escrow) is spending / income; the principal is a transfer."""
    from .loans import expected_flow
    sb = _d(s["skip_before"])
    done = {r["occurrence_date"] for r in db.all("SELECT occurrence_date FROM schedule_occurrences WHERE schedule_id=?", (s["id"],))}
    for d in iter_occurrences(s, last):
        if d < lo or (sb and d < sb) or d.isoformat() in done:
            continue
        f = expected_flow(db, s["loan_id"], d)
        if not f or f[1] <= 0:
            continue
        v, cur = f[1], f[2]
        vb = round(ledger.convert(v, cur, base, rates), 2)
        out[kind] += vb
        out[f"{kind}_count"] += 1
        out["items"].append({"date": f"{d.isoformat()} {s['time_of_day']}", "schedule_id": s["id"], "name": s["name"], "kind": kind,
                             "amount": v, "currency": cur, "amount_base": vb, "post_mode": s["post_mode"], "variable_amount": False,
                             "account_name": None, "loan_id": s["loan_id"]})
