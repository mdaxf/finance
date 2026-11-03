"""Low balance 余额预警: a bank account's forecast from schedules, bills, card statements and pending transactions, and the
'low_balance' reminder while it may drop below the account's threshold. FAKE data only."""
from datetime import date

from app import cashflow as CF
from app import reminders as R

ON = date(2026, 3, 1)


def mk(c, **kw):
    r = c.post("/api/accounts", json=kw)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def tx(c, **kw):
    r = c.post("/api/transactions", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def sched(c, name, start, tpl, **kw):
    r = c.post("/api/schedules", json={"name": name, "freq": kw.pop("freq", "monthly"), "start_date": start, "template": tpl, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def chk(c, bal=1000, **kw):
    return mk(c, name="Fake Checking", type="checking", currency="USD", opening_balance=bal, start_date="2026-01-01", **kw)


def test_forecast_schedules_in_out_and_lowest(client, db):
    a = chk(client, 1000)
    sav = mk(client, name="Fake Savings", type="savings", currency="USD", opening_balance=5000, start_date="2026-01-01")
    sched(client, "Fake Rent", "2026-03-05", {"kind": "expense", "account_id": a, "amount": 900})
    sched(client, "Fake Pay", "2026-03-10", {"kind": "income", "account_id": a, "amount": 2000})
    sched(client, "To savings", "2026-03-12", {"kind": "transfer", "account_id": a, "to_account_id": sav, "amount": 300})
    sched(client, "From savings", "2026-03-03", {"kind": "transfer", "account_id": sav, "to_account_id": a, "amount": 50}, freq="once")
    f = CF.forecast(db, a, 14, ON, threshold=500)
    assert f["start_balance"] == 1000 and f["until"] == "2026-03-15"
    assert [(e["date"], e["amount"]) for e in f["events"]] == [
        ("2026-03-03", 50), ("2026-03-05", -900), ("2026-03-10", 2000), ("2026-03-12", -300)]
    assert f["lowest"] == {"date": "2026-03-05", "balance": 150}
    assert f["first_below"]["date"] == "2026-03-05" and f["below"] and f["end_balance"] == 1850
    assert len(f["daily"]) == 15
    # the savings side sees the same transfers the other way
    s = CF.forecast(db, sav, 14, ON)
    assert [e["amount"] for e in s["events"]] == [-50, 300] and s["first_below"] is None


def test_generated_occurrence_is_not_counted_twice_and_pending_counts(client, db):
    a = chk(client, 1000)
    sched(client, "Fake Gym", "2026-03-01", {"kind": "expense", "account_id": a, "amount": 40})
    client.post("/api/schedules/run", json={"date": "2026-03-01"})     # posted: in the balance now
    tx(client, kind="expense", account_id=a, amount=25, date="2026-02-27", status="review")
    tx(client, kind="expense", account_id=a, amount=60, date="2026-03-08")  # dated ahead
    f = CF.forecast(db, a, 14, ON)
    assert f["start_balance"] == 960
    assert [(e["source"], e["date"], e["amount"]) for e in f["events"]] == [("pending", "2026-03-01", -25), ("posted", "2026-03-08", -60)]


def test_bills_paid_from_account_default_account_and_schedule_dedupe(client, db):
    a = chk(client, 1000)
    other = mk(client, name="Fake Other", type="checking", currency="USD", opening_balance=0, start_date="2026-01-01")
    for b in ({"payee": "Fake Power", "amount": 120, "due_date": "2026-03-06", "account_id": a},
              {"payee": "Fake Water", "amount": 30, "due_date": "2026-02-20"},           # overdue, no account
              {"payee": "Fake Phone", "amount": 80, "due_date": "2026-03-09", "account_id": a},
              {"payee": "Fake Later", "amount": 999, "due_date": "2026-04-30", "account_id": a},
              {"payee": "Fake Elsewhere", "amount": 50, "due_date": "2026-03-04", "account_id": other}):
        assert client.post("/api/bills", json=b).status_code == 200
    sched(client, "Fake Phone autopay", "2026-03-10", {"kind": "expense", "account_id": a, "amount": 80})
    f = CF.forecast(db, a, 14, ON)
    assert [(e["source"], e["title"]) for e in f["events"]] == [("bill", "Fake Power"), ("schedule", "Fake Phone autopay")]
    db.set_setting("low_balance_bill_account_id", str(a))
    f = CF.forecast(db, a, 14, ON)
    w = next(e for e in f["events"] if e["title"] == "Fake Water")
    assert w["date"] == "2026-03-01" and w["overdue"] and w["amount"] == -30


def test_card_statement_paid_from_account(client, db):
    a = chk(client, 3000)
    card = mk(client, name="Fake Card", type="credit_card", currency="USD", opening_balance=0, start_date="2026-01-01",
              stmt_day=20, due_day=10, pay_from_account_id=a)
    tx(client, kind="expense", account_id=card, amount=400, date="2026-02-15")   # statement 02-20, due 03-10
    tx(client, kind="expense", account_id=card, amount=70, date="2026-02-25")    # next statement (due 04-10: outside)
    f = CF.forecast(db, a, 14, ON)
    assert [(e["source"], e["date"], e["amount"]) for e in f["events"]] == [("card", "2026-03-10", -400)]
    R.run(db, ON, notify=False)                                                  # with the reminder: same amount, minus paid
    tx(client, kind="transfer", account_id=a, to_account_id=card, amount=150, date="2026-02-28")
    f = CF.forecast(db, a, 14, ON)
    assert [(e["source"], e["amount"]) for e in f["events"]] == [("card", -250)] and f["start_balance"] == 2850
    # a schedule paying the card from this account replaces the statement estimate
    sched(client, "Fake Card autopay", "2026-03-10", {"kind": "transfer", "account_id": a, "to_account_id": card, "amount": 250})
    f = CF.forecast(db, a, 14, ON)
    assert [e["source"] for e in f["events"]] == ["schedule"]
    # pay-from must be a bank / cash account
    assert client.put(f"/api/accounts/{card}", json={"pay_from_account_id": card}).status_code == 400


def test_reminder_opens_updates_clears_and_comes_back(client, db):
    a = chk(client, 1000, low_bal_threshold=500)
    s = sched(client, "Fake Rent", "2026-03-05", {"kind": "expense", "account_id": a, "amount": 700}, freq="once")
    rows = lambda: [r for r in R.listing(db, "all", ON)["items"] if r["kind"] == "low_balance"]
    r = rows()[0]
    assert (r["state"], r["due_date"], r["lowest"], r["threshold"], r["title"]) == ("upcoming", "2026-03-05", 300, 500, "Fake Checking")
    d = R.digest(db, 7, True, "en", ON)
    assert any("may drop to" in (x["detail"] or "") for x in d["items"] if x["kind"] == "low_balance")
    # income scheduled before the rent: no longer low -> completes by itself
    inc = sched(client, "Fake Pay", "2026-03-04", {"kind": "income", "account_id": a, "amount": 400}, freq="once")
    r = rows()[0]
    assert r["status"] == "done" and r["done_reason"] == "lb_clear"
    client.delete(f"/api/schedules/{inc['id']}")
    r = rows()[0]
    assert r["status"] == "open" and r["notified_at"] is None
    # done by hand while still low: stays done
    client.post(f"/api/reminders/{r['id']}/done")
    assert rows()[0]["status"] == "done"
    # alert off: dismissed / not reopened; deleting the account removes it
    client.put(f"/api/accounts/{a}", json={"low_bal_threshold": ""})
    client.post(f"/api/reminders/{r['id']}/reopen")
    assert rows()[0]["status"] == "dismissed"
    client.put(f"/api/accounts/{a}", json={"low_bal_threshold": 200})        # 300 is above 200: nothing to alert
    assert rows()[0]["status"] == "dismissed"
    client.put(f"/api/accounts/{a}", json={"low_bal_threshold": 400})
    r = rows()[0]
    assert r["status"] == "open" and r["threshold"] == 400
    client.delete(f"/api/schedules/{s['id']}")
    assert rows()[0]["status"] == "done"


def test_forecast_endpoint_and_settings(client, db):
    a = chk(client, 800, low_bal_threshold=500, low_bal_days=30)
    f = client.get(f"/api/accounts/{a}/forecast").json()
    assert f["days"] == 30 and f["threshold"] == 500 and not f["below"] and f["alert_on"]
    assert client.get(f"/api/accounts/{a}/forecast?threshold=900").json()["below"]
    assert client.get("/api/accounts/999999/forecast").status_code == 404
    assert client.put("/api/settings", json={"low_balance_days": "0"}).status_code == 400
    assert client.put("/api/settings", json={"low_balance_days": "21", "low_balance_bill_account_id": str(a)}).status_code == 200
    assert client.put("/api/accounts/%d" % a, json={"low_bal_days": ""}).status_code == 200
    assert client.get(f"/api/accounts/{a}/forecast").json()["days"] == 21
