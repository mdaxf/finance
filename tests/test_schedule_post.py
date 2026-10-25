"""Post one schedule occurrence now (✓ Post on Scheduled / Paid on a schedule reminder). FAKE data only."""
from datetime import date

from app import reminders as R


def mk_sched(client, **kw):
    r = client.post("/api/schedules", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def tx_of(client, sid):
    return client.get("/api/transactions", params={"schedule_id": sid, "status": "all", "limit": 500}).json()["items"]


def test_post_early_marks_done_and_schedule_continues(client, accounts):
    chk = accounts["checking"]["id"]
    s = mk_sched(client, name="Fake gym", template={"kind": "expense", "amount": 40, "account_id": chk, "merchant": "Fake Gym"},
                 freq="monthly", start_date="2099-01-05")
    r = client.post(f"/api/schedules/{s['id']}/post", json={"occurrence_date": "2099-01-05", "amount": 42.5, "date": "2098-12-30"})
    assert r.status_code == 200, r.text
    tx = r.json()["transaction"]
    assert tx["amount"] == 42.5 and tx["date"].startswith("2098-12-30") and tx["status"] == "posted" and tx["kind"] == "expense"
    # the daily run never posts that date again; the next month still posts
    run = client.post("/api/schedules/run", json={"date": "2099-02-05"}).json()
    assert [i["date"][:10] for i in run["items"]] == ["2099-02-05"]
    assert sorted(t["amount"] for t in tx_of(client, s["id"])) == [40, 42.5]
    nxt = client.get(f"/api/schedules/{s['id']}").json()["next_dates"]
    assert "2099-01-05" not in [d[:10] for d in nxt]
    # twice, or a date that is not one of the schedule's, is refused
    assert client.post(f"/api/schedules/{s['id']}/post", json={"occurrence_date": "2099-01-05"}).status_code == 400
    assert client.post(f"/api/schedules/{s['id']}/post", json={"occurrence_date": "2099-03-06"}).status_code == 400
    assert client.post("/api/schedules/99999/post", json={"occurrence_date": "2099-03-05"}).status_code == 404


def test_post_defaults_and_account_override(client, accounts):
    s = mk_sched(client, name="Fake rent", template={"kind": "expense", "amount": 1200, "account_id": accounts["checking"]["id"]},
                 freq="once", start_date="2099-05-01")
    tx = client.post(f"/api/schedules/{s['id']}/post", json={"occurrence_date": "2099-05-01", "account_id": accounts["sapphire"]["id"]}).json()["transaction"]
    assert tx["amount"] == 1200 and tx["account_id"] == accounts["sapphire"]["id"] and tx["schedule_id"] == s["id"]
    assert client.get(f"/api/schedules/{s['id']}").json()["next_dates"] == []


def test_variable_needs_amount(client, accounts):
    s = mk_sched(client, name="Fake power", template={"kind": "expense", "account_id": accounts["checking"]["id"]},
                 freq="monthly", start_date="2099-01-10", variable_amount=True)
    assert client.post(f"/api/schedules/{s['id']}/post", json={"occurrence_date": "2099-01-10"}).status_code == 400
    assert client.post(f"/api/schedules/{s['id']}/post", json={"occurrence_date": "2099-01-10", "amount": 88}).json()["transaction"]["amount"] == 88


def test_post_closes_open_reminder_and_reminder_paid_links_occurrence(client, db, accounts):
    chk = accounts["checking"]["id"]
    s = mk_sched(client, name="Fake insurance", post_mode="remind", template={"kind": "expense", "amount": 300, "account_id": chk},
                 freq="monthly", start_date="2026-10-15")
    client.post("/api/schedules/run", json={"date": "2026-10-12"})
    rem = [r for r in R.listing(db, "all", date(2026, 10, 12))["items"] if r["kind"] == "schedule" and r["schedule_id"] == s["id"]]
    assert len(rem) == 1 and rem[0]["status"] == "open"
    # posting it from Scheduled closes the reminder
    tx = client.post(f"/api/schedules/{s['id']}/post", json={"occurrence_date": "2026-10-15", "amount": 310}).json()["transaction"]
    after = R.get(db, rem[0]["id"])
    assert after["status"] == "done" and after["transaction_id"] == tx["id"]
    occ = db.one("SELECT transaction_id FROM schedule_occurrences WHERE schedule_id=? AND occurrence_date='2026-10-15'", (s["id"],))
    assert occ["transaction_id"] == tx["id"]
    # next month: Paid on the reminder posts through the schedule and links the occurrence
    client.post("/api/schedules/run", json={"date": "2026-11-12"})
    rem2 = [r for r in R.listing(db, "all", date(2026, 11, 12))["items"] if r["kind"] == "schedule" and r["occurrence_date"] == "2026-11-15"]
    r = client.post(f"/api/reminders/{rem2[0]['id']}/paid", json={"create_payment": True, "amount": 305, "date": "2026-11-14"}).json()
    assert r["status"] == "done" and r["transaction"]["amount"] == 305 and r["transaction"]["schedule_id"] == s["id"]
    occ = db.one("SELECT transaction_id FROM schedule_occurrences WHERE schedule_id=? AND occurrence_date='2026-11-15'", (s["id"],))
    assert occ["transaction_id"] == r["transaction"]["id"]
    assert client.post(f"/api/schedules/{s['id']}/post", json={"occurrence_date": "2026-11-15"}).status_code == 400


def test_post_loan_schedule_records_split_payment(client, accounts):
    chk = accounts["checking"]["id"]
    house = client.post("/api/accounts", json={"name": "Fake House Loan", "type": "loan", "currency": "USD", "opening_balance": -100000}).json()["id"]
    loan = client.post("/api/loans", json={"direction": "borrowed", "counterparty": "Fake Servicer", "name": "Fake mortgage", "kind": "bank",
                                           "account_id": house, "pay_account_id": chk, "rate": 6, "freq": "monthly", "first_due": "2099-01-01",
                                           "payment": 599.55}).json()
    assert client.post(f"/api/loans/{loan['id']}/schedule", json={"post_mode": "post"}).status_code == 200
    sid = client.get(f"/api/loans/{loan['id']}").json()["schedule_id"]
    r = client.post(f"/api/schedules/{sid}/post", json={"occurrence_date": "2099-01-01", "date": "2098-12-28"})
    assert r.status_code == 200, r.text
    plan = r.json()["loan_payment"]
    roles = {p["role"]: p["amount"] for p in plan}
    assert set(roles) == {"principal", "interest"} and abs(sum(roles.values()) - 599.55) < 0.01
    run = client.post("/api/schedules/run", json={"date": "2099-01-01"}).json()
    assert not [i for i in run["items"] if i["schedule_id"] == sid]
