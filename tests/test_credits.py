"""Expiring credits 抵用金 (refund / cancellation / bonus credits with an expiry date): CRUD, partial / full use,
expiry flagging, the configurable reminder window (default 6 months) and the Reminder Center integration. FAKE data."""
from datetime import date, timedelta

import pytest

from app import credits, reminders


def mk(c, **kw):
    r = c.post("/api/accounts", json=kw)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def acct(c):
    return mk(c, name="Test Airline Wallet", type="ewallet", currency="USD", opening_balance=0)


def credit(c, **kw):
    r = c.post("/api/credits", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def today(c):
    return date.fromisoformat(c.get("/api/credits").json()["today"])


def days(c, n):
    return (today(c) + timedelta(days=n)).isoformat()


def credit_rems(c, view="open"):
    return [r for r in c.get(f"/api/reminders?view={view}").json()["items"] if r["kind"] == "credit"]


# ---------------------------------------------------------------- pure window / expiry logic
def test_add_months_clamps_month_end():
    assert credits.add_months(date(2026, 8, 31), -6) == date(2026, 2, 28)
    assert credits.add_months(date(2028, 8, 31), -6) == date(2028, 2, 29)
    assert credits.add_months(date(2026, 3, 15), -6) == date(2025, 9, 15)
    assert credits.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_reminder_window_boundaries():
    exp = date(2027, 5, 31)
    assert credits.window_start(exp, 6) == date(2026, 11, 30)
    assert credits.lead_days_for(exp, 6) == (exp - date(2026, 11, 30)).days
    assert not credits.is_expiring(exp, date(2026, 11, 29), 6)  # one day before the window
    assert credits.is_expiring(exp, date(2026, 11, 30), 6)      # first day of the window
    assert credits.is_expiring(exp, exp, 6)                     # the expiry day itself is still usable
    assert not credits.is_expiring(exp, exp + timedelta(days=1), 6)
    assert credits.is_expiring(exp, date(2027, 3, 1), 3) and not credits.is_expiring(exp, date(2027, 2, 27), 3)
    assert credits.window_start(exp, 0) == exp


def test_expiry_flagging_states():
    on = date(2026, 10, 24)
    base = {"status": "open", "amount": 100, "remaining": 100}
    assert credits.state_of({**base, "expiry_date": "2026-10-24"}, on) == "active"
    assert credits.state_of({**base, "expiry_date": "2026-10-23"}, on) == "expired"
    assert credits.state_of({**base, "remaining": 40, "expiry_date": "2027-01-01"}, on) == "partial"
    assert credits.state_of({**base, "remaining": 0, "expiry_date": "2026-01-01"}, on) == "used"  # used up: not "expired"
    assert credits.state_of({**base, "status": "void", "expiry_date": "2026-01-01"}, on) == "void"


# ---------------------------------------------------------------- CRUD + use
def test_create_list_update_use_and_void(client):
    a = acct(client)
    c = credit(client, account_id=a, source="cancel", amount=250, expiry_date=days(client, 300), title="Test Air cancelled flight",
               reference="TKT-0001")
    assert c["state"] == "active" and c["remaining"] == 250 and c["currency"] == "USD" and c["issued_date"] == today(client).isoformat()
    assert c["account_name"] == "Test Airline Wallet" and c["source_label"] == "Cancellation"
    # partial use, then the rest
    c = client.post(f"/api/credits/{c['id']}/use", json={"amount": 100.5, "note": "Test trip"}).json()
    assert c["state"] == "partial" and c["remaining"] == 149.5 and c["used_amount"] == 100.5 and len(c["uses"]) == 1
    r = client.post(f"/api/credits/{c['id']}/use", json={"amount": 200})
    assert r.status_code == 400 and "149.50" in r.json()["detail"]
    c = client.post(f"/api/credits/{c['id']}/use", json={}).json()  # no amount = everything left
    assert c["state"] == "used" and c["remaining"] == 0 and c["status"] == "used"
    assert client.post(f"/api/credits/{c['id']}/use", json={}).status_code == 400
    # undo the last use -> partial again
    c = client.delete(f"/api/credits/{c['id']}/uses/{c['uses'][-1]['id']}").json()
    assert c["state"] == "partial" and c["remaining"] == 149.5
    # update: correcting the amount keeps what was used
    c = client.put(f"/api/credits/{c['id']}", json={"amount": 300}).json()
    assert c["amount"] == 300 and c["remaining"] == 199.5
    # list filters
    lst = client.get("/api/credits").json()
    assert [x["id"] for x in lst["items"]] == [c["id"]] and lst["available"] == [{"currency": "USD", "amount": 199.5}]
    # void and reopen
    c = client.post(f"/api/credits/{c['id']}/void", json={"note": "forfeited"}).json()
    assert c["state"] == "void" and "forfeited" in c["note"]
    assert client.get("/api/credits").json()["items"] == []
    assert len(client.get("/api/credits?state=void").json()["items"]) == 1
    assert client.post(f"/api/credits/{c['id']}/reopen").json()["state"] == "partial"
    assert client.delete(f"/api/credits/{c['id']}").json() == {"ok": True}
    assert client.get(f"/api/credits/{c['id']}").status_code == 404


def test_validation(client):
    a = acct(client)
    bad = [({"source": "refund", "amount": 10, "expiry_date": days(client, 30)}, "account"),
           ({"account_id": a, "source": "gift", "amount": 10, "expiry_date": days(client, 30)}, "source"),
           ({"account_id": a, "source": "refund", "amount": -5, "expiry_date": days(client, 30)}, "greater than zero"),
           ({"account_id": a, "source": "refund", "amount": 10}, "Expiry date"),
           ({"account_id": a, "source": "refund", "amount": 10, "remaining": 11, "expiry_date": days(client, 30)}, "Remaining"),
           ({"account_id": a, "source": "refund", "amount": 10, "issued_date": days(client, 5), "expiry_date": days(client, 1)}, "after"),
           ({"account_id": 9999, "source": "bonus", "amount": 10, "expiry_date": days(client, 30)}, "Unknown account")]
    for body, msg in bad:
        r = client.post("/api/credits", json=body)
        assert r.status_code == 400 and msg.lower() in r.json()["detail"].lower(), (body, r.text)
    assert credit(client, account_id=a, source="Cancellation", amount=10, expiry_date=days(client, 30))["source"] == "cancel"


def test_credits_never_touch_balances(client):
    a = acct(client)
    before = {x["id"]: x["balance"] for x in client.get("/api/accounts").json()["accounts"]}
    c = credit(client, account_id=a, source="bonus", amount=50, expiry_date=days(client, 60))
    client.post(f"/api/credits/{c['id']}/use", json={"amount": 20})
    after = {x["id"]: x["balance"] for x in client.get("/api/accounts").json()["accounts"]}
    assert before == after and client.get("/api/transactions").json()["items"] == []


def test_expired_credit_flagged_and_use_needs_force(client):
    a = acct(client)
    c = credit(client, account_id=a, source="refund", amount=80, issued_date=days(client, -400), expiry_date=days(client, -1))
    assert c["state"] == "expired" and c["expired"] and not c["expiring_soon"] and c["days_left"] == -1
    lst = client.get("/api/credits").json()
    assert lst["expired_count"] == 1 and lst["available"] == []  # expired money is not counted as available
    r = client.post(f"/api/credits/{c['id']}/use", json={"amount": 10})
    assert r.status_code == 400 and "expired" in r.json()["detail"]
    assert client.post(f"/api/credits/{c['id']}/use", json={"amount": 10, "force": True}).json()["remaining"] == 70
    # the reminder is overdue (= expired) until the credit is used up / closed or the reminder is Done
    rem = credit_rems(client)
    assert len(rem) == 1 and rem[0]["state"] == "overdue" and rem[0]["expired"] and rem[0]["credit_state"] == "expired"


# ---------------------------------------------------------------- reminders
def test_reminder_window_default_six_months(client):
    a = acct(client)
    t = today(client)
    inside = credit(client, account_id=a, source="cancel", amount=100, expiry_date=(credits.add_months(t, 6)).isoformat(), title="Inside")
    outside = credit(client, account_id=a, source="bonus", amount=30, expiry_date=(credits.add_months(t, 6) + timedelta(days=1)).isoformat(),
                     title="Outside")
    assert inside["expiring_soon"] and not outside["expiring_soon"]
    open_ = credit_rems(client)
    assert [r["credit_id"] for r in open_] == [inside["id"]] and open_[0]["state"] == "upcoming"
    later = credit_rems(client, "all_upcoming")
    assert {r["credit_id"]: r["state"] for r in later} == {inside["id"]: "upcoming", outside["id"]: "later"}
    exp = client.get("/api/credits/expiring").json()
    assert exp["months"] == 6 and [x["id"] for x in exp["items"]] == [inside["id"]]
    assert [x["id"] for x in client.get("/api/credits/expiring?months=12").json()["items"]] == [inside["id"], outside["id"]]
    assert client.get("/api/meta").json()["credit_expiring_count"] == 1


def test_reminder_window_is_configurable(client):
    a = acct(client)
    t = today(client)
    c = credit(client, account_id=a, source="refund", amount=100, expiry_date=(t + timedelta(days=75)).isoformat())
    assert len(credit_rems(client)) == 1
    assert client.put("/api/settings", json={"credit_expiry_lead_months": "2"}).status_code == 200
    assert credit_rems(client) == [] and not client.get(f"/api/credits/{c['id']}").json()["expiring_soon"]
    assert client.put("/api/settings", json={"credit_expiry_lead_months": "3"}).status_code == 200
    assert len(credit_rems(client)) == 1
    for bad in ("-1", "61", "x"):
        assert client.put("/api/settings", json={"credit_expiry_lead_months": bad}).status_code == 400


def test_reminder_follows_the_credit(client):
    a = acct(client)
    c = credit(client, account_id=a, source="cancel", amount=100, expiry_date=days(client, 20), title="Test Hotel credit")
    rem = credit_rems(client)
    assert len(rem) == 1 and rem[0]["remaining"] == 100 and rem[0]["title"] == "Test Hotel credit"
    client.post(f"/api/credits/{c['id']}/use", json={"amount": 60})
    assert credit_rems(client)[0]["remaining"] == 40
    # mark paid is refused (a credit is not a bill)
    assert client.post(f"/api/reminders/{rem[0]['id']}/paid", json={}).status_code == 400
    # used up -> the reminder completes by itself; undo -> it comes back
    c = client.post(f"/api/credits/{c['id']}/use", json={}).json()
    assert credit_rems(client) == []
    done = [r for r in credit_rems(client, "done")]
    assert done and done[0]["done_reason"] == "credit_used"
    client.delete(f"/api/credits/{c['id']}/uses/{c['uses'][-1]['id']}")
    assert len(credit_rems(client)) == 1
    # expiry moved -> reminder due date follows
    client.put(f"/api/credits/{c['id']}", json={"expiry_date": days(client, 25)})
    assert credit_rems(client)[0]["due_date"] == days(client, 25)
    # void -> done; delete -> dismissed (and never regenerated)
    client.post(f"/api/credits/{c['id']}/void", json={})
    assert credit_rems(client) == []
    client.post(f"/api/credits/{c['id']}/reopen")
    assert len(credit_rems(client)) == 1
    client.delete(f"/api/credits/{c['id']}")
    assert credit_rems(client) == [] and credit_rems(client, "done")[0]["done_reason"] == "credit_deleted"


def test_digest_and_email_report_list_credits(client, db):
    from app import email_reports
    a = acct(client)
    credit(client, account_id=a, source="cancel", amount=120, expiry_date=days(client, 100), title="Test Air voucher")
    d = client.get("/api/reminders/notify?lang=en").json()
    # listed for its whole window, not only the next 7 days
    assert d["count"] == 1 and "Test Air voucher" in d["message"] and "120" in d["message"] and "expires" in d["message"]
    assert d["items"][0]["kind"] == "credit" and d["items"][0]["credit_id"]
    rep = email_reports.build(db, "reminders", lng="en")
    assert "Test Air voucher" in rep["text"]
    zh = reminders.credit_text({**d["items"][0], "account_name": None}, True)
    assert "过期" in zh and "剩余" in zh


def test_account_delete_removes_its_credits(client):
    a = acct(client)
    credit(client, account_id=a, source="bonus", amount=10, expiry_date=days(client, 30))
    assert client.delete(f"/api/accounts/{a}").status_code == 200
    assert client.get("/api/credits?state=all").json()["items"] == [] and credit_rems(client) == []


@pytest.mark.parametrize("months", [1, 6, 12])
def test_lead_days_used_by_reminder_center(db, months):
    db.set_setting("credit_expiry_lead_months", str(months))
    exp = "2027-03-31"
    assert reminders.lead_days(db, {"kind": "credit", "due_date": exp}) == credits.lead_days_for(exp, months)
