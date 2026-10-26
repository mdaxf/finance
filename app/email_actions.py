"""Email parsing-rule actions 邮件规则动作: a rule of one of these kinds makes something other than a new transaction.

Called by ingest.ingest_email (after the bill step) and by the rule tester (dry_run=True: what would happen, nothing
written). The rule supplies: amount regex, date regex (date_text), payee (fixed name), account_id and target_id.

* schedule_post   target = a schedule. The email is that schedule happening (a paycheck alert for the Salary
                  schedule): the occurrence it belongs to (the date from the date regex, else the latest one on or
                  before the email date + 3 days, within 45 days) is posted like "✓ Post" with the email's amount
                  (else the schedule's) and marked done, so the daily run never posts it again. Already posted -> the
                  email only confirms it (a transaction still in review gets the email's amount and is posted).
                  No occurrence near the email date -> the normal flow (a transaction to review).
* schedule_new    a future charge ("autopay of $150 scheduled for Nov 5", "renews on …"): a one-time schedule on that
                  date in reminder mode (nothing posted; ✓ Post / Paid when it happens). Same name + date = existing.
* loan_payment    target = a loan. Records the payment (principal / interest / escrow split) dated the payment date.
                  A loan with its own schedule goes through schedule_post on it (no double count with the daily run).
                  A payment already recorded within 5 days -> nothing new.
* credit          a travel / store credit: a Credit (issuer = payee, amount, expiry from the date regex or "expires …").
* reward_balance  target = a miles & points program: its balance (and expiry, when found) is updated.

Each returns None to let the normal flow go on, else {result, action, stop, detail, ref_kind, ref_id, transaction_id?}.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import ledger
from .db import Database

KINDS = ("schedule_post", "schedule_new", "loan_payment", "credit", "reward_balance")
POST_WINDOW_DAYS, POST_AHEAD_DAYS = 45, 3
LOAN_DUP_DAYS = 5
EXPIRY_LABELS = r"(?:expires?(?: on)?|expiration(?: date)?|expiry(?: date)?|valid (?:until|through|thru)|use (?:it )?by|good through|有效期至|到期日|有效期)"


class ActionSkip(Exception):
    """The rule matched but the email can't be used for its action; the reason is logged, the normal flow goes on."""


def _ref_date(email_date: str | None, db: Database) -> date:
    from .schedules import local_today
    try:
        return date.fromisoformat(str(email_date or "")[:10])
    except ValueError:
        return local_today(db)


def _date(ex: dict, ref: date, labels: str | None = None) -> date | None:
    from .bills import _labelled_date, parse_date_text
    if ex.get("date_text"):
        d = parse_date_text(ex["date_text"], ref)
        if d:
            return d
    return _labelled_date(ex.get("text") or "", labels, ref) if labels else None


def _account(db: Database, ex: dict, mailbox: dict | None) -> int | None:
    aid = ex.get("rule_account_id") or (mailbox or {}).get("default_account_id") or db.get_setting("default_account_id")
    return int(aid) if aid not in (None, "", 0, "0") else None


def handle(db: Database, ex: dict, sender: str, subject: str, email_date: str | None, mid: str,
           mailbox: dict | None = None, dry_run: bool = False) -> dict | None:
    kind = ex.get("kind")
    if kind not in KINDS:
        return None
    fn = {"schedule_post": _schedule_post, "schedule_new": _schedule_new, "loan_payment": _loan_payment,
          "credit": _credit, "reward_balance": _reward}[kind]
    try:
        out = fn(db, ex, sender, subject, _ref_date(email_date, db), mid, mailbox, dry_run)
    except ActionSkip as e:
        return {"result": kind, "action": "skipped", "stop": False, "detail": str(e), "dry_run": dry_run}
    except (ledger.ValidationError, KeyError, ValueError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args and isinstance(e.args[0], str) else str(e)
        return {"result": kind, "action": "skipped", "stop": False, "detail": f"{kind}: {msg}", "dry_run": dry_run}
    if out is not None:
        out.setdefault("result", kind)
        out["dry_run"] = dry_run
    return out


# ------------------------------------------------------------------ schedules
def pick_occurrence(db: Database, s: dict, ref: date, want: date | None = None) -> date | None:
    """The occurrence an email dated `ref` is about: `want` (the date in the email) when it is one of the schedule's
    dates, else the closest one to it; without a date, the latest one on or before ref + 3 days (within 45 days)."""
    from .schedules import iter_occurrences
    if want:
        lo, hi = want - timedelta(days=POST_WINDOW_DAYS), want + timedelta(days=POST_WINDOW_DAYS)
        ds = [d for d in iter_occurrences(s, hi) if d >= lo]
        return min(ds, key=lambda d: (abs((d - want).days), d)) if ds else None
    hi = ref + timedelta(days=POST_AHEAD_DAYS)
    ds = [d for d in iter_occurrences(s, hi) if d >= ref - timedelta(days=POST_WINDOW_DAYS)]
    return max(ds) if ds else None


def _post_schedule(db: Database, sid: int, ex: dict, ref: date, account_id: int | None, dry_run: bool,
                   label: str = "schedule") -> dict:
    from . import schedules
    s = schedules.get(db, int(sid))
    if not s:
        raise ActionSkip(f"{label}: the rule's schedule #{sid} no longer exists")
    occ = pick_occurrence(db, s, ref, _date(ex, ref))
    if not occ:
        raise ActionSkip(f"{label}: no date of “{s['name']}” near {ref}")
    base = {"ref_kind": "schedule", "ref_id": s["id"], "occurrence_date": occ.isoformat(), "schedule_name": s["name"]}
    row = db.one("SELECT transaction_id FROM schedule_occurrences WHERE schedule_id=? AND occurrence_date=?",
                 (s["id"], occ.isoformat()))
    tx = ledger.get_tx(db, row["transaction_id"]) if row and row["transaction_id"] else None
    amt = ex.get("amount")
    if tx:
        if tx["status"] == "review":
            what = f"{s['name']} {occ}: confirms #{tx['id']} (in review) → posted" + (f" with {amt}" if amt is not None else "")
            if not dry_run:
                upd = {"status": "posted"}
                if amt is not None and not s.get("loan_id"):
                    upd["amount"] = amt
                ledger.update_tx(db, tx["id"], upd)
            return {**base, "action": "confirmed", "stop": True, "detail": what, "transaction_id": tx["id"]}
        return {**base, "action": "existing", "stop": True, "transaction_id": tx["id"],
                "detail": f"{s['name']} {occ}: already posted as #{tx['id']}, nothing new"}
    amount = amt if amt is not None else s["template"].get("amount")
    if amount in (None, ""):
        raise ActionSkip(f"{label}: no amount in the email and “{s['name']}” has none")
    what = f"{s['name']} {occ}: posted {amount}"
    if dry_run:
        return {**base, "action": "post", "stop": True, "detail": what, "amount": amount}
    res = schedules.post_occurrence(db, s["id"], {"occurrence_date": occ.isoformat(), "amount": amount,
                                                  "date": ref.isoformat(), "account_id": account_id}, check=False)
    tid = (res.get("transaction") or {}).get("id")
    if tid:
        with db.tx() as con:
            con.execute("UPDATE transactions SET source='email' WHERE id=? AND source='scheduled'", (tid,))
    return {**base, "action": "posted", "stop": True, "detail": what + (f" (#{tid})" if tid else ""), "transaction_id": tid}


def _schedule_post(db, ex, sender, subject, ref, mid, mailbox, dry_run):
    if not ex.get("target_id"):
        raise ActionSkip("schedule: choose the schedule on the rule")
    return _post_schedule(db, ex["target_id"], ex, ref, ex.get("rule_account_id"), dry_run)


def _schedule_new(db, ex, sender, subject, ref, mid, mailbox, dry_run):
    from . import schedules
    from .bills import _sender_name
    from .email_rules import detect_kind
    d = _date(ex, ref, r"(?:scheduled for|scheduled on|will be (?:charged|debited|processed|paid|deposited) on|renews? on|"
                       r"renewal date|on or after|payment date|processing date|扣款日|扣款时间)")
    if not d:
        raise ActionSkip("new schedule: no date found (set a date regex on the rule)")
    if ex.get("amount") is None:
        raise ActionSkip("new schedule: no amount found")
    aid = _account(db, ex, mailbox)
    if not aid:
        raise ActionSkip("new schedule: choose the account on the rule")
    name = (ex.get("payee") or ex.get("merchant") or _sender_name(sender) or subject or "Email")[:80]
    kind = "income" if detect_kind(ex.get("text") or "") == "income" else "expense"
    base = {"ref_kind": "schedule", "occurrence_date": d.isoformat()}
    same = db.one("SELECT id FROM schedules WHERE name=? AND start_date=? AND freq='once'", (name, d.isoformat()))
    if same:
        return {**base, "ref_id": same["id"], "action": "existing", "stop": True,
                "detail": f"{name} on {d}: already scheduled (#{same['id']})"}
    what = f"{name} on {d}: {kind} {ex['amount']} scheduled (reminder)"
    if dry_run:
        return {**base, "action": "create", "stop": True, "detail": what}
    tpl = {"kind": kind, "amount": ex["amount"], "account_id": aid, "merchant": ex.get("payee") or ex.get("merchant"),
           "note": (subject or "")[:200]}
    if ex.get("currency"):
        tpl["currency"] = ex["currency"]
    s = schedules.save(db, {"name": name, "freq": "once", "start_date": d.isoformat(), "post_mode": "remind",
                            "template": tpl})
    return {**base, "ref_id": s["id"], "action": "created", "stop": True, "detail": what + f" (#{s['id']})"}


# ------------------------------------------------------------------ loans
def _loan_payment(db, ex, sender, subject, ref, mid, mailbox, dry_run):
    from . import loans
    lid = ex.get("target_id")
    if not lid:
        raise ActionSkip("loan: choose the loan on the rule")
    loan = db.one("SELECT * FROM loans WHERE id=?", (int(lid),))
    if not loan:
        raise ActionSkip(f"loan: the rule's loan #{lid} no longer exists")
    name = loan["name"] or loan["counterparty"] or f"Loan #{loan['id']}"
    sid = loan.get("schedule_id")
    if sid and db.one("SELECT 1 x FROM schedules WHERE id=?", (sid,)):
        out = _post_schedule(db, sid, ex, ref, ex.get("rule_account_id"), dry_run, label="loan")
        return {**out, "detail": f"{name}: " + out["detail"], "loan_id": loan["id"]}
    when = _date(ex, ref, r"(?:payment date|paid on|date paid|processed on|posted on|effective date)") or ref
    lo, hi = (when - timedelta(days=LOAN_DUP_DAYS)).isoformat(), (when + timedelta(days=LOAN_DUP_DAYS)).isoformat() + " 99"
    dup = db.one("SELECT id, date FROM loan_events WHERE loan_id=? AND kind='payment' AND date BETWEEN ? AND ?",
                 (loan["id"], lo, hi))
    base = {"ref_kind": "loan", "ref_id": loan["id"]}
    if dup:
        return {**base, "action": "existing", "stop": True, "detail": f"{name}: a payment on {dup['date'][:10]} is already recorded"}
    body = {"date": when.isoformat(), "amount": ex.get("amount"), "account_id": ex.get("rule_account_id"),
            "note": (subject or "")[:200], "dry_run": dry_run}
    res = loans.record_payment(db, loan["id"], body)
    total = round(sum(p["amount"] for p in res["plan"]), 2)
    parts = ", ".join(f"{p['role']} {p['amount']}" for p in res["plan"])
    what = f"{name}: payment {total} on {when} ({parts})"
    tid = next((p.get("transaction_id") for p in res["plan"] if p.get("transaction_id")), None)
    return {**base, "action": "record" if dry_run else "recorded", "stop": True, "detail": what,
            "transaction_id": None if dry_run else tid, "plan": res["plan"]}


# ------------------------------------------------------------------ credits / miles & points
def _credit(db, ex, sender, subject, ref, mid, mailbox, dry_run):
    from . import credits
    from .bills import _sender_name
    if ex.get("amount") is None:
        raise ActionSkip("credit: no amount found")
    exp = _date(ex, ref, EXPIRY_LABELS)
    if not exp:
        raise ActionSkip("credit: no expiry date found (set a date regex on the rule)")
    low = (ex.get("text") or "").lower()
    source = "refund" if "refund" in low or "退款" in low else "bonus" if re.search(r"bonus|promo|reward|courtesy|奖励|赠送", low) else "cancel"
    title = (ex.get("payee") or _sender_name(sender) or ex.get("merchant") or "")[:120]
    aid = ex.get("rule_account_id")
    same = db.one("SELECT id FROM credits WHERE title=? AND ABS(amount-?)<0.005 AND expiry_date=? AND status!='void'",
                  (title, ex["amount"], exp.isoformat()))
    base = {"ref_kind": "credit"}
    if same:
        return {**base, "ref_id": same["id"], "action": "existing", "stop": True,
                "detail": f"credit {title} {ex['amount']} (expires {exp}): already recorded (#{same['id']})"}
    body = {"title": title, "amount": ex["amount"], "currency": ex.get("currency"), "source": source,
            "issued_date": min(ref, exp).isoformat(), "expiry_date": exp.isoformat(), "account_id": aid,
            "note": (subject or "")[:200]}
    what = f"credit {title or '?'} {ex['amount']} ({source}), expires {exp}"
    if dry_run:
        credits._clean(db, body)  # the same checks as a real create
        return {**base, "action": "create", "stop": True, "detail": what}
    c = credits.create(db, body)
    return {**base, "ref_id": c["id"], "action": "created", "stop": True, "detail": what + f" (#{c['id']})"}


def _reward(db, ex, sender, subject, ref, mid, mailbox, dry_run):
    from . import rewards
    rid = ex.get("target_id")
    if not rid:
        raise ActionSkip("points: choose the miles & points program on the rule")
    r = db.one("SELECT * FROM rewards WHERE id=?", (int(rid),))
    if not r:
        raise ActionSkip(f"points: the rule's program #{rid} no longer exists")
    if ex.get("amount") is None:
        raise ActionSkip(f"points: no {r['unit']} balance found (set the amount regex on the rule)")
    exp = _date(ex, ref, EXPIRY_LABELS)
    base = {"ref_kind": "reward", "ref_id": r["id"]}
    bal = ex["amount"]
    if abs((r["balance"] or 0) - bal) < 0.5 and (not exp or exp.isoformat() == r["expiry_date"]):
        return {**base, "action": "unchanged", "stop": True, "detail": f"{r['name']}: still {bal:,.0f} {r['unit']}"}
    what = f"{r['name']}: {r['balance'] or 0:,.0f} → {bal:,.0f} {r['unit']}" + (f", expires {exp}" if exp else "")
    if dry_run:
        return {**base, "action": "update", "stop": True, "detail": what}
    rewards.set_balance(db, r["id"], {"balance": bal, "date": ref.isoformat(),
                                      "expiry_date": exp.isoformat() if exp else None, "note": ("email: " + (subject or ""))[:200]})
    return {**base, "action": "updated", "stop": True, "detail": what}
