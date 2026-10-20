"""Reminder Center 提醒中心: credit-card statement/payment reminders and 'remind only' schedules.

Credit cards (accounts.stmt_day = 账单日, due_day = 还款日 or due_after_days = N days after the statement):
* each closed cycle gets one reminder (statement date, due date, statement balance = amount owed at the end of the
  statement date, honouring reconciliation checkpoints); only the latest closed cycle is generated (no backfill)
* auto-completed when transfers into the card after the statement date add up to the statement amount
  (amount_override if the user typed the real 账单金额); partial payments show the remaining amount
Card expiry (accounts.card_expiry): a 'card_expiry' reminder from N days (setting card_expiry_lead_days, default 60)
before the end of the expiry month, listing the schedules charged to the card; see the "card expiry" section below.
Schedules with post_mode='remind' create a reminder per occurrence instead of a transaction; marking it paid can create
the transaction from the schedule's template, and the reminder completes when that linked transaction is posted.
The job is idempotent (unique keys) and only touches accounts/schedules the user configured.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

from . import ledger
from .db import Database, now_iso

log = logging.getLogger(__name__)
VIEWS = ("upcoming", "later", "all_upcoming", "overdue", "snoozed", "done", "open", "all")


class ReminderError(ValueError):
    pass


def _d(s) -> date | None:
    if not s:
        return None
    if isinstance(s, date):
        return s
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def today(db: Database) -> date:
    from .schedules import local_today
    return local_today(db)


def _dim(y: int, m: int, day: int) -> date:
    """Day `day` of month y-m, clamped to the month end (31 -> Feb 28/29)."""
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return date(y, m, min(day, (nxt - timedelta(days=1)).day))


def _shift(y: int, m: int, n: int) -> tuple[int, int]:
    k = y * 12 + (m - 1) + n
    return k // 12, k % 12 + 1


def statement_date(stmt_day: int, y: int, m: int) -> date:
    return _dim(y, m, int(stmt_day))


def last_statement(stmt_day: int, on: date) -> date:
    """Latest statement date <= on."""
    s = statement_date(stmt_day, on.year, on.month)
    if s <= on:
        return s
    y, m = _shift(on.year, on.month, -1)
    return statement_date(stmt_day, y, m)


def prev_statement(stmt_day: int, s: date) -> date:
    y, m = _shift(s.year, s.month, -1)
    return statement_date(stmt_day, y, m)


def next_statement(stmt_day: int, s: date) -> date:
    y, m = _shift(s.year, s.month, 1)
    return statement_date(stmt_day, y, m)


def due_date(acct: dict, s: date) -> date | None:
    """Payment due date for statement date s: N days after (due_after_days) or the next due_day after s."""
    if acct.get("due_after_days"):
        return s + timedelta(days=int(acct["due_after_days"]))
    if acct.get("due_day"):
        d = _dim(s.year, s.month, int(acct["due_day"]))
        if d <= s:
            y, m = _shift(s.year, s.month, 1)
            d = _dim(y, m, int(acct["due_day"]))
        return d
    return None


# ---------------------------------------------------------------- generation
def owed_at(db: Database, aid: int, d: date) -> float:
    """Amount owed on a liability account at the end of day d (reconciliation checkpoints honoured)."""
    return round(-ledger.account_balance_at(db, aid, f"{d.isoformat()} 23:59"), 2) + 0.0


def payments_after(db: Database, aid: int, s: date, until: date | None = None) -> float:
    """Posted money into the card after the statement date: transfers in (+ refunds/income booked on the card)."""
    q = """SELECT COALESCE(SUM(CASE WHEN kind='transfer' THEN COALESCE(to_amount, amount) ELSE amount END), 0) v
           FROM transactions WHERE status='posted' AND date > ? AND date <= ?
           AND ((kind='transfer' AND to_account_id=?) OR (kind IN ('refund') AND account_id=?))"""
    hi = (until.isoformat() if until else "9999-12-31") + " 23:59"
    return round(db.one(q, (f"{s.isoformat()} 23:59", hi, aid, aid))["v"], 2) + 0.0


def card_accounts(db: Database) -> list[dict]:
    return db.all("SELECT * FROM accounts WHERE type='credit_card' AND archived=0 AND stmt_day IS NOT NULL "
                  "AND (due_day IS NOT NULL OR due_after_days IS NOT NULL)")


def generate_cards(db: Database, on: date | None = None) -> list[int]:
    """One reminder for the latest closed cycle of each configured card (idempotent). Returns new reminder ids."""
    on = on or today(db)
    new = []
    for a in card_accounts(db):
        s = last_statement(a["stmt_day"], on)
        due = due_date(a, s)
        owed = owed_at(db, a["id"], s)
        amt = max(owed, 0.0)
        mn = round(amt * float(a["min_pay_pct"]) / 100, 2) if a.get("min_pay_pct") and amt else None
        with db.tx() as con:
            if con.execute("SELECT 1 FROM reminders WHERE kind='card' AND account_id=? AND statement_date=?",
                           (a["id"], s.isoformat())).fetchone():
                continue
            st, why = ("done", "nothing_due") if amt < 0.005 else ("open", None)
            rid = con.execute("""INSERT INTO reminders(kind, account_id, title, cycle_start, statement_date, due_date, amount,
                minimum, currency, status, done_reason, autopay, created_at, updated_at) VALUES('card',?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (a["id"], a["name"], (prev_statement(a["stmt_day"], s) + timedelta(days=1)).isoformat(), s.isoformat(),
                 due.isoformat() if due else s.isoformat(), amt, mn, a["currency"], st, why, a.get("autopay") or 0,
                 now_iso(), now_iso())).lastrowid
        new.append(rid)
    return new


def create_for_schedule(con, s: dict, tpl: dict, d: date) -> int:
    """Called by schedules.run for post_mode='remind' (inside its transaction)."""
    acct = con.execute("SELECT currency FROM accounts WHERE id=?", (tpl.get("account_id"),)).fetchone()
    return con.execute("""INSERT INTO reminders(kind, schedule_id, occurrence_date, account_id, to_account_id, title, due_date,
        amount, currency, status, created_at, updated_at) VALUES('schedule',?,?,?,?,?,?,?,?, 'open', ?, ?)""",
        (s["id"], d.isoformat(), tpl.get("account_id"), tpl.get("to_account_id"), s["name"], d.isoformat(),
         tpl.get("amount"), tpl.get("currency") or (acct[0] if acct else None), now_iso(), now_iso())).lastrowid


def refresh(db: Database, on: date | None = None) -> int:
    """Auto-complete open reminders: cards paid in full; schedule reminders whose linked transaction is posted."""
    n = 0
    for r in db.all("SELECT * FROM reminders WHERE status='open'"):
        why = None
        if r["kind"] == "card":
            amt = r["amount_override"] if r["amount_override"] is not None else r["amount"]
            paid = payments_after(db, r["account_id"], _d(r["statement_date"]))
            if amt is not None and paid + 0.005 >= amt:
                why = "auto_paid"
        elif r["transaction_id"]:
            t = db.one("SELECT status FROM transactions WHERE id=?", (r["transaction_id"],))
            if t and t["status"] == "posted":
                why = "tx_posted"
        if why:
            with db.tx() as con:
                con.execute("UPDATE reminders SET status='done', done_reason=?, done_at=?, updated_at=? WHERE id=? AND status='open'",
                            (why, now_iso(), now_iso(), r["id"]))
            n += 1
    return n


def run(db: Database, on: date | None = None, notify: bool = True) -> dict:
    on = on or today(db)
    from .loans import sync_reminders as sync_loans
    sync_loans(db, on)
    new = generate_cards(db, on) + generate_expiry(db, on) + generate_reserves(db, on)
    done = refresh(db, on) + refresh_expiry(db, on) + refresh_reserves(db, on)
    sent = notify_due(db, on) if notify else 0
    return {"as_of": on.isoformat(), "created": len(new), "auto_completed": done, "notified": sent}


# ---------------------------------------------------------------- reading
def _decorate(db: Database, r: dict, on: date) -> dict:
    r = dict(r)
    amt = r["amount_override"] if r["amount_override"] is not None else r["amount"]
    r["statement_amount"] = amt
    if r["kind"] == "card" and r["statement_date"]:
        paid = payments_after(db, r["account_id"], _d(r["statement_date"]))
        r["paid"] = paid
        r["remaining"] = round(max((amt or 0) - paid, 0), 2) + 0.0
    else:
        r["paid"], r["remaining"] = None, amt
    due = _d(r["due_date"])
    r["days_left"] = (due - on).days if due else None
    snoozed = r["snooze_until"] and _d(r["snooze_until"]) > on
    r["state"] = ("done" if r["status"] in ("done", "dismissed") else "snoozed" if snoozed
                  else "overdue" if due and due < on else "upcoming")
    r["lead_days"] = lead_days(db, r)
    if r["state"] == "upcoming" and r["days_left"] is not None and r["days_left"] > r["lead_days"]:
        r["state"] = "later"  # not shown before its lead window (only in the 'all upcoming' filter)
    if r["account_id"]:
        a = db.one("SELECT name, last4 FROM accounts WHERE id=?", (r["account_id"],)) or {}
        r["account_name"], r["account_last4"] = a.get("name"), a.get("last4")
    if r["kind"] == "card_expiry":
        r.update(expiry_info(db, r, on))
        r["paid"] = r["remaining"] = r["statement_amount"] = None
        r["expired"] = bool(due and due < on)
    if r["kind"] == "reserve":
        r.update(reserve_info(db, r))
    if r["kind"] == "loan":
        from .loans import reminder_info
        r.update(reminder_info(db, r))
    elif r["kind"] == "schedule" and r.get("schedule_id"):
        s = db.one("SELECT loan_id FROM schedules WHERE id=?", (r["schedule_id"],))
        r["loan_id"] = (s or {}).get("loan_id")
    return r


def lead_days(db: Database, r: dict) -> int:
    """Days before the due date a reminder shows up: schedule.lead_days, card remind_days, else the global setting
    reminder_lead_days (default 10)."""
    if r.get("kind") == "card_expiry":
        return expiry_lead(db)
    if r.get("kind") == "reserve":
        return 0  # an unreleased hold / unrefunded deposit is chased from its due date (check-out + N days) on
    if r.get("kind") == "schedule" and r.get("schedule_id"):
        s = db.one("SELECT lead_days FROM schedules WHERE id=?", (r["schedule_id"],)) or {}
        if s.get("lead_days") is not None:
            return int(s["lead_days"])
    elif r.get("account_id"):
        a = db.one("SELECT remind_days FROM accounts WHERE id=?", (r["account_id"],)) or {}
        if a.get("remind_days") is not None:
            return int(a["remind_days"])
    return int(db.get_setting("reminder_lead_days") or 10)


def listing(db: Database, view: str = "open", on: date | None = None) -> dict:
    on = on or today(db)
    if view not in VIEWS:
        raise ReminderError(f"view must be one of {VIEWS}")
    sync_expiry(db, on)
    sync_reserves(db, on)
    from .loans import sync_reminders as sync_loans
    sync_loans(db, on)
    rows = [_decorate(db, r, on) for r in db.all("SELECT * FROM reminders ORDER BY due_date, id")]
    counts = {k: sum(1 for r in rows if r["state"] == k) for k in ("upcoming", "later", "overdue", "snoozed", "done")}
    if view == "open":
        items = [r for r in rows if r["state"] in ("upcoming", "overdue")]
    elif view == "all_upcoming":
        items = [r for r in rows if r["state"] in ("upcoming", "later")]
    elif view == "all":
        items = rows
    else:
        items = [r for r in rows if r["state"] == view]
    if view == "done":
        items.sort(key=lambda r: (r["due_date"] or ""), reverse=True)
    return {"view": view, "today": on.isoformat(), "items": items, "counts": counts, "badge": badge(db, on, rows)}


def badge(db: Database, on: date | None = None, rows: list | None = None) -> int:
    """Nav badge: overdue + upcoming within each reminder's lead days."""
    on = on or today(db)
    if rows is None:
        sync_expiry(db, on)
        sync_reserves(db, on)
        from .loans import sync_reminders as sync_loans
        sync_loans(db, on)
    rows = rows if rows is not None else [_decorate(db, r, on) for r in db.all("SELECT * FROM reminders WHERE status='open'")]
    return sum(1 for r in rows if r["state"] in ("overdue", "upcoming"))


def get(db: Database, rid: int) -> dict:
    r = db.one("SELECT * FROM reminders WHERE id=?", (rid,))
    if not r:
        raise KeyError(rid)
    return _decorate(db, r, today(db))


# ---------------------------------------------------------------- actions
def _set(db: Database, rid: int, **kw) -> dict:
    if not db.one("SELECT 1 x FROM reminders WHERE id=?", (rid,)):
        raise KeyError(rid)
    kw["updated_at"] = now_iso()
    with db.tx() as con:
        con.execute(f"UPDATE reminders SET {','.join(k + '=?' for k in kw)} WHERE id=?", list(kw.values()) + [rid])
    return get(db, rid)


EDITABLE = ("amount_override", "minimum", "reference", "note", "due_date", "title")


def edit(db: Database, rid: int, body: dict) -> dict:
    kw = {k: body[k] for k in EDITABLE if k in body}
    for k in ("amount_override", "minimum"):
        if k in kw:
            kw[k] = None if kw[k] in ("", None) else round(float(kw[k]), 2)
    if "due_date" in kw:
        kw["due_date"] = _d(kw["due_date"]).isoformat()
    r = _set(db, rid, **kw)
    refresh(db)  # a lower typed statement amount may already be covered
    return get(db, rid)


def mark_done(db: Database, rid: int, reason: str = "manual") -> dict:
    out = _set(db, rid, status="done", done_reason=reason, done_at=now_iso())
    if out["kind"] == "card_expiry" and out.get("pending_count"):
        out["warning"] = (f"{out['pending_count']} scheduled payment(s) still use this card and are not confirmed: "
                          + ", ".join(out["pending_names"]))
    return out


def dismiss(db: Database, rid: int) -> dict:
    return _set(db, rid, status="dismissed", done_reason="dismissed", done_at=now_iso())


def reopen(db: Database, rid: int) -> dict:
    return _set(db, rid, status="open", done_reason=None, done_at=None, snooze_until=None)


def snooze(db: Database, rid: int, days: int | None = None, until=None) -> dict:
    d = _d(until) if until else today(db) + timedelta(days=int(days or 1))
    return _set(db, rid, snooze_until=d.isoformat())


def mark_paid(db: Database, rid: int, body: dict) -> dict:
    """Mark paid; optionally create the payment: card -> transfer from `from_account_id` to the card (default amount =
    remaining); schedule reminder -> the schedule's transaction (amount editable)."""
    r = get(db, rid)
    if r["kind"] == "card_expiry":
        raise ReminderError("A card expiry reminder has no payment: confirm the card and its scheduled payments, or press Done")
    if r["kind"] == "reserve":
        raise ReminderError("Check out the reserve instead (Reserves → Check out): record the final bill, the refund or the part kept")
    tx = None
    loan_id = r.get("loan_id") if r["kind"] in ("loan", "schedule") else None
    if loan_id and body.get("create_payment"):
        # Loan Center: the repayment is split into principal (transfer) + interest (expense / income)
        from . import loans
        pay = {"amount": body.get("amount"), "date": body.get("date"), "account_id": body.get("from_account_id")}
        if r["kind"] == "schedule":
            pay["due_date"] = r["occurrence_date"]
        res = loans.record_payment(db, loan_id, pay)
        first = res["plan"][0]["transaction_id"] if res["plan"] else None
        kw = {"status": "done", "done_reason": "paid", "done_at": now_iso()}
        if first:
            kw["transaction_id"] = first
        out = _set(db, rid, **kw)
        out["transaction"] = ledger.get_tx(db, first) if first else None
        out["loan_payment"] = res["plan"]
        return out
    if body.get("create_payment"):
        amount = body.get("amount")
        amount = float(amount) if amount not in (None, "") else (r["remaining"] or r["statement_amount"])
        when = str(body.get("date") or datetime.now().strftime("%Y-%m-%d %H:%M")).replace("T", " ")
        if r["kind"] == "card":
            if not body.get("from_account_id"):
                raise ReminderError("Choose the account the payment comes from")
            tx = ledger.create_tx(db, {"kind": "transfer", "account_id": int(body["from_account_id"]), "to_account_id": r["account_id"],
                                       "amount": amount, "date": when, "note": f"Card payment 还款 · {r['title']}"
                                       + (f" · {r['reference']}" if r.get("reference") else ""), "source": "reminder"})
        else:
            s = db.one("SELECT template, name FROM schedules WHERE id=?", (r["schedule_id"],))
            tpl = json.loads(s["template"]) if s else {}
            data = {**tpl, "amount": amount, "date": when, "source": "reminder"}
            if body.get("from_account_id"):
                data["account_id"] = int(body["from_account_id"])
            tx = ledger.create_tx(db, data)
    kw = {"status": "done", "done_reason": "paid", "done_at": now_iso()}
    if tx:
        kw["transaction_id"] = tx["id"]
    out = _set(db, rid, **kw)
    out["transaction"] = tx
    return out


# ---------------------------------------------------------------- notifications
def _zh(lang: str, db: Database) -> bool:
    if lang in ("zh", "cn"):
        return True
    if lang and lang not in ("auto",):
        from .ingest import resolve_lang
        return resolve_lang(lang) == "zh"
    from .ingest import msg_zh  # auto: the request's Accept-Language (set by main.py), default English
    return msg_zh()


def digest(db: Database, days: int = 7, include_overdue: bool = True, lang: str = "auto", on: date | None = None) -> dict:
    """Short text for phone notifications (iPhone Shortcuts / Telegram)."""
    on = on or today(db)
    zh = _zh(lang, db)
    rows = listing(db, "open", on)["items"]  # 'open' already excludes items before their lead window
    # card expiry reminders are listed during their whole lead window (default 60 days), not only the next `days`
    items = [r for r in rows if (r["state"] == "overdue" and include_overdue) or
             (r["state"] == "upcoming" and r["days_left"] is not None and (r["days_left"] <= days or r["kind"] == "card_expiry"))]
    from .ingest import fmt_money
    lines = []
    for r in items:
        if r["kind"] == "card_expiry":
            r["detail"] = expiry_text(r, zh)
            lines.append(f"{'⚠️ ' if r['state'] == 'overdue' else ''}💳 {r['detail']}")
            continue
        if r["kind"] == "reserve":
            r["detail"] = reserve_text(r, zh)
            lines.append(f"{'⚠️ ' if r['state'] == 'overdue' else ''}🔒 {r['detail']}")
            continue
        amt = r["remaining"] if r["remaining"] is not None else r["statement_amount"]
        due = _d(r["due_date"])
        when = (f"逾期 {-r['days_left']} 天" if zh else f"OVERDUE {-r['days_left']}d") if r["state"] == "overdue" else \
               (("今天" if zh else "today") if r["days_left"] == 0 else ("明天" if zh else "tomorrow") if r["days_left"] == 1
                else f"{due.month}/{due.day}")
        lines.append(f"{'⚠️ ' if r['state'] == 'overdue' else ''}{when} · {r['title']}" + (f" · {fmt_money(amt, r['currency'])}" if amt else ""))
    n = len(items)
    if zh:
        title = f"{n} 条提醒" if n else "没有待办提醒"
        msg = "\n".join(lines) if n else f"未来 {days} 天没有到期的账单 ✓"
    else:
        title = f"{n} reminder{'s' if n != 1 else ''} due" if n else "No reminders due"
        msg = "\n".join(lines) if n else f"Nothing due in the next {days} days ✓"
    return {"count": n, "title": title, "message": msg, "items": [
        {k: r.get(k) for k in ("id", "kind", "title", "due_date", "days_left", "state", "statement_amount", "remaining",
                               "currency", "account_name", "account_last4", "expiry", "expiry_label", "pending_names",
                               "card_confirmed", "new_expiry_label", "detail", "reserve_id", "reserve_method", "reserve_end", "reserve_start")} for r in items], "today": on.isoformat()}


_TW = (("已于", "已於"), ("将于", "將於"), ("请更新以下定期付款的卡", "請更新以下定期付款的卡"),
       ("请录入新卡有效期或确认", "請輸入新卡有效期或確認"))


def expiry_text(r: dict, zh: bool, tw: bool = False) -> str:
    """One line for a card_expiry reminder (notify / Telegram / email); tw = Traditional Chinese (emails)."""
    if tw:
        out = expiry_text(r, True)
        for a, b in _TW:
            out = out.replace(a, b)
        return out
    card = (r.get("title") or r.get("account_name") or "") + (f" ••{r['account_last4']}" if r.get("account_last4") else "")
    exp, past, names = r.get("expiry_label"), r.get("state") == "overdue", r.get("pending_names") or []
    if zh:
        s = f"{card} {'已于' if past else '将于'} {exp} 到期"
        if names:
            s += " · 请更新以下定期付款的卡：" + "、".join(names)
        if not r.get("card_confirmed"):
            s += " · 请录入新卡有效期或确认"
        elif r.get("new_expiry_label"):
            s += f" · 新卡有效期 {r['new_expiry_label']}"
        return s
    s = f"{card} {'expired' if past else 'expires'} {exp}"
    if names:
        s += " · Update the payment card for: " + ", ".join(names)
    if not r.get("card_confirmed"):
        s += " · Enter the new expiry or confirm the card"
    elif r.get("new_expiry_label"):
        s += f" · new card expires {r['new_expiry_label']}"
    return s


def notify_due(db: Database, on: date | None = None) -> int:
    """Optional push (setting reminder_notify = 'telegram'; default off): once per reminder when it enters its lead
    window or becomes overdue."""
    if (db.get_setting("reminder_notify") or "off") != "telegram":
        return 0
    from . import telegram_bot
    sender = telegram_bot.notifier()
    if not sender:
        return 0
    on = on or today(db)
    rows = [r for r in listing(db, "open", on)["items"] if not r.get("notified_at")]
    if not rows:
        return 0
    d = digest(db, 3650, True, "auto", on)
    ids = {r["id"] for r in rows}
    text = d["title"] + "\n" + "\n".join(l for l, it in zip(d["message"].split("\n"), d["items"]) if it["id"] in ids)
    if sender(text):
        with db.tx() as con:
            con.executemany("UPDATE reminders SET notified_at=? WHERE id=?", [(now_iso(), i) for i in ids])
        return len(ids)
    return 0


# ---------------------------------------------------------------- card expiry 信用卡到期
# accounts.card_expiry = 'YYYY-MM' (entered as MM/YY; the card works until the END of that month).
# From `card_expiry_lead_days` (setting, default 60) before that day a 'card_expiry' reminder exists for that expiry
# (reminders.expiry; one per card + expiry, so a new expiry starts a fresh cycle with fresh confirmations). It lists the
# active schedules (incl. remind-only) whose template charges the card (template.account_id) so their autopay can be
# moved to the new card. It completes by itself ('resolved') when BOTH:
#   * the card is confirmed: a later expiry was entered (renewed), the user acknowledged it (closed / not renewing), or the
#     account was archived; and
#   * every linked schedule is confirmed ('card updated') or reassigned to another account (moved off the card).
# 'Done' (POST /api/reminders/{id}/done) still closes it at once and returns a warning while schedules are unconfirmed.
EXPIRY_LEAD_DEFAULT = 60
CARD_STATES = ("renewed", "acknowledged")


def parse_expiry(v) -> str | None:
    """'MM/YY', 'MM/YYYY', 'MM-YY', 'YYYY-MM' -> 'YYYY-MM'; blank -> None. ValueError otherwise."""
    s = str(v if v is not None else "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2})\s*[/\-.]\s*(\d{2}|\d{4})", s)
    if m:
        mo, y = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(r"(\d{4})\s*[/\-.]\s*(\d{1,2})", s)
        if not m:
            raise ValueError("Card expiry must be MM/YY (e.g. 08/29)")
        y, mo = int(m.group(1)), int(m.group(2))
    if y < 100:
        y += 2000
    if not 1 <= mo <= 12 or not 2000 <= y <= 2099:
        raise ValueError("Card expiry must be MM/YY with a month 01–12 (e.g. 08/29)")
    return f"{y:04d}-{mo:02d}"


def expiry_end(exp: str) -> date:
    """Last day of the expiry month (the card works through that day)."""
    y, m = int(exp[:4]), int(exp[5:7])
    return _dim(y, m, 31)


def expiry_label(exp: str | None) -> str | None:
    return f"{exp[5:7]}/{exp[:4]}" if exp else None


def expiry_lead(db: Database) -> int:
    try:
        return max(0, min(365, int(db.get_setting("card_expiry_lead_days") or EXPIRY_LEAD_DEFAULT)))
    except (TypeError, ValueError):
        return EXPIRY_LEAD_DEFAULT


def card_schedules(db: Database, aid: int, on: date | None = None) -> list[dict]:
    """Active (enabled, not ended) schedules whose transaction is charged to / paid from account `aid`."""
    from . import schedules
    on = on or today(db)
    out = []
    for s in db.all("SELECT * FROM schedules WHERE enabled=1 ORDER BY name, id"):
        try:
            tpl = json.loads(s["template"] or "{}")
        except ValueError:
            continue
        try:
            if int(tpl.get("account_id") or 0) != int(aid):
                continue
        except (TypeError, ValueError):
            continue
        d = schedules.decorate(db, dict(s), on)
        if d["finished"]:
            continue
        out.append({"schedule_id": d["id"], "name": d["name"], "post_mode": d["post_mode"], "kind": tpl.get("kind"),
                    "amount": tpl.get("amount"), "currency": d.get("currency"), "description": d.get("description"),
                    "next_date": d["next_dates"][0] if d["next_dates"] else None})
    return out


def generate_expiry(db: Database, on: date | None = None) -> list[int]:
    """Create the card_expiry reminder once a card enters its lead window (idempotent per card + expiry)."""
    on = on or today(db)
    lead = expiry_lead(db)
    new = []
    for a in db.all("SELECT * FROM accounts WHERE type='credit_card' AND archived=0 AND card_expiry IS NOT NULL AND card_expiry<>''"):
        try:
            end = expiry_end(a["card_expiry"])
        except (TypeError, ValueError, IndexError):
            continue
        if on < end - timedelta(days=lead):
            continue
        with db.tx() as con:
            if con.execute("SELECT 1 FROM reminders WHERE kind='card_expiry' AND account_id=? AND expiry=?",
                           (a["id"], a["card_expiry"])).fetchone():
                continue
            new.append(con.execute("""INSERT INTO reminders(kind, account_id, title, due_date, expiry, currency, status,
                autopay, created_at, updated_at) VALUES('card_expiry',?,?,?,?,?,'open',?,?,?)""",
                (a["id"], a["name"], end.isoformat(), a["card_expiry"], a["currency"], a.get("autopay") or 0,
                 now_iso(), now_iso())).lastrowid)
    return new


def expiry_info(db: Database, r: dict, on: date | None = None) -> dict:
    """Linked schedules with their confirmation, the card confirmation and whether the reminder is resolved."""
    on = on or today(db)
    acct = db.one("SELECT id, name, last4, card_expiry, archived FROM accounts WHERE id=?", (r["account_id"],)) or {}
    checks = {c["schedule_id"]: c for c in db.all("SELECT * FROM card_expiry_checks WHERE reminder_id=?", (r["id"],))}
    names = {a["id"]: a["name"] for a in db.all("SELECT id, name FROM accounts")}
    linked = card_schedules(db, r["account_id"], on) if acct else []
    items, on_card = [], set()
    for s in linked:
        on_card.add(s["schedule_id"])
        c = checks.get(s["schedule_id"])
        ok = bool(c and c["action"] == "confirmed")
        items.append({**s, "status": "confirmed" if ok else "pending", "checked_at": c["created_at"] if ok else None})
    for sid, c in checks.items():
        if c["action"] == "reassigned" and sid not in on_card:
            s = db.one("SELECT id, name, template FROM schedules WHERE id=?", (sid,))
            if not s:
                continue
            try:
                to = int(json.loads(s["template"] or "{}").get("account_id") or 0) or c["to_account_id"]
            except (TypeError, ValueError):
                to = c["to_account_id"]
            items.append({"schedule_id": sid, "name": s["name"], "status": "reassigned", "to_account_id": to,
                          "to_account_name": names.get(to), "checked_at": c["created_at"]})
    new_exp = acct.get("card_expiry")
    renewed = bool(new_exp and r.get("expiry") and new_exp > r["expiry"])
    state = r.get("card_state") or ("renewed" if renewed else "closed" if acct.get("archived") else None)
    pending = [i for i in items if i["status"] == "pending"]
    return {"expiry_label": expiry_label(r.get("expiry")), "card_schedules": items, "pending_count": len(pending),
            "pending_names": [i["name"] for i in pending], "card_confirmed": state is not None, "card_state_eff": state,
            "new_expiry": new_exp if renewed else None, "new_expiry_label": expiry_label(new_exp) if renewed else None,
            "resolved": state is not None and not pending, "account_exists": bool(acct)}


def refresh_expiry(db: Database, on: date | None = None) -> int:
    """Expiry reminders: a later expiry on the account confirms the card ('renewed'); an earlier/removed expiry retires the
    reminder; resolved ones complete. Idempotent; called by run() and before listing."""
    on = on or today(db)
    n = 0
    for r in db.all("SELECT * FROM reminders WHERE kind='card_expiry' AND status='open'"):
        acct = db.one("SELECT card_expiry FROM accounts WHERE id=?", (r["account_id"],))
        kw = None
        if not acct:
            kw = {"status": "dismissed", "done_reason": "account_deleted"}
        elif (acct["card_expiry"] or "") != (r["expiry"] or ""):
            if acct["card_expiry"] and acct["card_expiry"] > (r["expiry"] or ""):
                if not r["card_state"]:
                    _set(db, r["id"], card_state="renewed", card_state_at=now_iso())
                    r = dict(r, card_state="renewed")
            else:  # corrected to an earlier month or removed: this cycle no longer applies
                kw = {"status": "dismissed", "done_reason": "expiry_changed"}
        if kw is None and expiry_info(db, r, on)["resolved"]:
            kw = {"status": "done", "done_reason": "resolved"}
        if kw:
            with db.tx() as con:
                con.execute("UPDATE reminders SET status=?, done_reason=?, done_at=?, updated_at=? WHERE id=? AND status='open'",
                            (kw["status"], kw["done_reason"], now_iso(), now_iso(), r["id"]))
            n += 1
    return n


def sync_expiry(db: Database, on: date | None = None) -> None:
    on = on or today(db)
    generate_expiry(db, on)
    refresh_expiry(db, on)


def _expiry_reminder(db: Database, rid: int) -> dict:
    r = db.one("SELECT * FROM reminders WHERE id=?", (rid,))
    if not r:
        raise KeyError(rid)
    if r["kind"] != "card_expiry":
        raise ReminderError("Not a card expiry reminder")
    return r


def _linked_ids(db: Database, r: dict) -> set[int]:
    return {s["schedule_id"] for s in card_schedules(db, r["account_id"])}


def confirm_schedule(db: Database, rid: int, sid: int, undo: bool = False) -> dict:
    """'Card updated / confirmed' for one linked schedule (e.g. the merchant autopay now uses the new card)."""
    r = _expiry_reminder(db, rid)
    with db.tx() as con:
        if undo:
            con.execute("DELETE FROM card_expiry_checks WHERE reminder_id=? AND schedule_id=?", (rid, sid))
        else:
            if sid not in _linked_ids(db, r):
                raise ReminderError("That schedule doesn't use this card")
            con.execute("""INSERT INTO card_expiry_checks(reminder_id, schedule_id, action, from_account_id, created_at)
                VALUES(?,?,'confirmed',?,?) ON CONFLICT(reminder_id, schedule_id) DO UPDATE SET action='confirmed',
                to_account_id=NULL, created_at=excluded.created_at""", (rid, sid, r["account_id"], now_iso()))
    refresh_expiry(db)
    return get(db, rid)


def reassign_schedule(db: Database, rid: int, sid: int, account_id) -> dict:
    """Move a linked schedule to another account (its template's account), recorded on this reminder."""
    from . import schedules
    r = _expiry_reminder(db, rid)
    if sid not in _linked_ids(db, r):
        raise ReminderError("That schedule doesn't use this card")
    try:
        to = int(account_id)
    except (TypeError, ValueError):
        raise ReminderError("Choose the account the schedule should use")
    a = db.one("SELECT id, archived FROM accounts WHERE id=?", (to,))
    if not a or a["archived"]:
        raise ReminderError("Choose an active account")
    if to == r["account_id"]:
        raise ReminderError("Choose a different account (or use 'Card updated' if the same card account stays)")
    s = db.one("SELECT template FROM schedules WHERE id=?", (sid,))
    tpl = json.loads(s["template"])
    tpl["account_id"] = to
    try:
        schedules.save(db, {"template": tpl}, sid)
    except schedules.ScheduleError as e:
        raise ReminderError(str(e))
    with db.tx() as con:
        con.execute("""INSERT INTO card_expiry_checks(reminder_id, schedule_id, action, from_account_id, to_account_id, created_at)
            VALUES(?,?,'reassigned',?,?,?) ON CONFLICT(reminder_id, schedule_id) DO UPDATE SET action='reassigned',
            to_account_id=excluded.to_account_id, created_at=excluded.created_at""", (rid, sid, r["account_id"], to, now_iso()))
        # open 'remind only' reminders of that schedule follow it to the new account
        con.execute("UPDATE reminders SET account_id=?, updated_at=? WHERE kind='schedule' AND schedule_id=? AND status='open'",
                    (to, now_iso(), sid))
    refresh_expiry(db)
    return get(db, rid)


def confirm_card(db: Database, rid: int, body: dict) -> dict:
    """The card itself: action 'renew' (+ expiry MM/YY, later than this reminder's) updates the account's expiry;
    'acknowledge' (card closed / not renewed / nothing to change); 'undo' clears a manual confirmation."""
    r = _expiry_reminder(db, rid)
    action = (body or {}).get("action") or "acknowledge"
    if action == "renew":
        exp = parse_expiry((body or {}).get("expiry"))
        if not exp or exp <= (r["expiry"] or ""):
            raise ReminderError(f"Enter the new card's expiry, later than {expiry_label(r['expiry'])}")
        with db.tx() as con:
            con.execute("UPDATE accounts SET card_expiry=? WHERE id=?", (exp, r["account_id"]))
        _set(db, rid, card_state="renewed", card_state_at=now_iso())
    elif action == "acknowledge":
        _set(db, rid, card_state="acknowledged", card_state_at=now_iso())
    elif action == "undo":
        _set(db, rid, card_state=None, card_state_at=None)
    else:
        raise ReminderError("action must be renew, acknowledge or undo")
    refresh_expiry(db)
    generate_expiry(db)
    return get(db, rid)


# ---------------------------------------------------------------- reserves 押金/预授权
# One 'reserve' reminder per open reserve (reminders.reserve_id, unique), due check-out + reserve_release_days (default
# 7) or check-in + reserve_hold_days (default 30) without a check-out date; lead 0 = it shows (and is pushed by
# email / Telegram) from that day. It completes by itself when the reserve closes and comes back if it is reopened.
_TW_RES = (("预授权", "預授權"), ("押金", "押金"), ("未释放", "未釋放"), ("未退还", "未退還"), ("退房", "退房"), ("入住", "入住"))


def reserve_info(db: Database, r: dict) -> dict:
    from . import reserves
    try:
        x = reserves.get(db, r["reserve_id"])
    except KeyError:
        return {"reserve_method": None, "reserve_state": "deleted"}
    return {"reserve_method": x["method"], "reserve_state": x["state"], "remaining": x["outstanding"],
            "statement_amount": x["amount"], "paid": None, "reserve_end": x.get("end_date"), "reserve_start": x["start_date"],
            "title": x["label"], "currency": x["currency"]}


def reserve_text(r: dict, zh: bool, tw: bool = False) -> str:
    """One line for a reserve reminder (Telegram / email / iPhone notify); tw = Traditional Chinese (emails)."""
    from .ingest import fmt_money
    amt = fmt_money(r["remaining"], r["currency"]) if r.get("remaining") is not None else ""
    end, start = r.get("reserve_end"), r.get("reserve_start")
    hold = r.get("reserve_method") == "hold"
    if zh:
        s = f"{r.get('title') or ''} · {'预授权' if hold else '押金'} {amt} {'未释放' if hold else '未退还'}"
        s += f"（退房 {end}）" if end else (f"（入住 {start}）" if start else "")
        if tw:
            for a, b in _TW_RES:
                s = s.replace(a, b)
        return s
    s = f"{r.get('title') or ''} · {'hold' if hold else 'deposit'} {amt} {'not released' if hold else 'not refunded'}"
    return s + (f" (check-out {end})" if end else (f" (check-in {start})" if start else ""))


def generate_reserves(db: Database, on: date | None = None) -> list[int]:
    """Create the reminder of every open reserve (idempotent per reserve); keep due date / amount in step."""
    from . import reserves
    on = on or today(db)
    new = []
    for x in reserves.list_reserves(db, {"state": "open,partial"})["items"]:
        if not x.get("remind") or x.get("needs_review"):
            continue
        due = x["release_due"]
        with db.tx() as con:
            ex = con.execute("SELECT id, status, due_date, amount FROM reminders WHERE kind='reserve' AND reserve_id=?", (x["id"],)).fetchone()
            if ex:
                if ex["status"] == "open" and (ex["due_date"] != due or ex["amount"] != x["outstanding"]):
                    con.execute("UPDATE reminders SET due_date=?, amount=?, title=?, updated_at=? WHERE id=?",
                                (due, x["outstanding"], x["label"], now_iso(), ex["id"]))
                continue
            new.append(con.execute("""INSERT INTO reminders(kind, reserve_id, account_id, title, due_date, amount, currency, status,
                created_at, updated_at) VALUES('reserve',?,?,?,?,?,?,'open',?,?)""",
                (x["id"], x["account_id"], x["label"], due, x["outstanding"], x["currency"], now_iso(), now_iso())).lastrowid)
    return new


def refresh_reserves(db: Database, on: date | None = None) -> int:
    """Open reserve reminders complete when the reserve closes (or reminders are turned off for it), are dismissed
    when it is deleted; ones completed that way reopen with the reserve."""
    n = 0
    for r in db.all("SELECT * FROM reminders WHERE kind='reserve' AND (status='open' OR done_reason LIKE 'reserve_%')"):
        x = db.one("SELECT status, remind, needs_review FROM reserves WHERE id=?", (r["reserve_id"],))
        kw = None
        if r["status"] == "open":
            if not x:
                kw = ("dismissed", "reserve_deleted")
            elif x["status"] in ("settled", "released", "cancelled"):
                kw = ("done", "reserve_" + x["status"])
            elif not x["remind"]:
                kw = ("dismissed", "reserve_no_reminder")
        elif x and x["status"] in ("open", "partial") and x["remind"] and r["done_reason"] != "reserve_deleted":
            with db.tx() as con:
                con.execute("UPDATE reminders SET status='open', done_reason=NULL, done_at=NULL, notified_at=NULL, updated_at=? WHERE id=?",
                            (now_iso(), r["id"]))
            n += 1
        if kw:
            with db.tx() as con:
                con.execute("UPDATE reminders SET status=?, done_reason=?, done_at=?, updated_at=? WHERE id=? AND status='open'",
                            (kw[0], kw[1], now_iso(), now_iso(), r["id"]))
            n += 1
    return n


def sync_reserves(db: Database, on: date | None = None) -> None:
    on = on or today(db)
    refresh_reserves(db, on)
    generate_reserves(db, on)
