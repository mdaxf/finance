"""Email-driven bills 账单待付: bill-email parsing, payment-task (schedule) creation with a reminder before the due date,
and matching payment-confirmation emails (match / no match / ambiguous / tolerance / date window). FAKE data only."""
from datetime import date, timedelta

import pytest

from app import bills

REF = date(2026, 10, 24)


def P(subject, body, sender="Test Power Co <billing@testpower.example>", received=REF):
    return bills.parse_bill_email(sender, subject, body, received)


# ---------------------------------------------------------------- parsing
def test_parse_utility_bill_us_format():
    p = P("Your Test Power Co bill is ready", "Hello,\nAmount due: $123.45\nDue date: November 5, 2026\nAccount number: 4455-001\n")
    assert p["type"] == "bill" and p["payee"] == "Test Power Co" and p["amount"] == 123.45 and p["currency"] == "USD"
    assert p["due_date"] == "2026-11-05" and p["reference"] == "4455-001" and p["sender_domain"] == "testpower.example"


@pytest.mark.parametrize("body,due", [
    ("Total due $80.00. Payment due date: 11/15/2026", "2026-11-15"),
    ("Balance due: $80.00 - please pay by 2026-11-15", "2026-11-15"),
    ("Amount due $80.00, due on Nov 15", "2026-11-15"),
    ("Amount due $80.00, due by 15 November 2026", "2026-11-15"),
    ("Amount due $80.00 is due 11/15", "2026-11-15"),
    ("Amount due $80.00. Due: Jan 3", "2027-01-03"),  # no year: the next one after the email date
])
def test_parse_due_date_formats(body, due):
    p = P("Your statement is ready", body)
    assert p["type"] == "bill" and p["amount"] == 80.0 and p["due_date"] == due


def test_parse_payment_request_and_invoice():
    p = P("Test Landscaping requested a payment of $250.00", "Test Landscaping has requested $250.00 for October service.",
          sender="Pay App <requests@payapp.example>")
    assert p["type"] == "bill" and p["payee"] == "Test Landscaping" and p["amount"] == 250 and p["due_date"] is None
    p = P("Invoice INV-2026-104 from Test Design Studio", "Invoice number: INV-2026-104\nTotal: $1,200.00\nDue date: 2026-11-30",
          sender="Test Design Studio <billing@testdesign.example>")
    assert p["type"] == "bill" and p["payee"] == "Test Design Studio" and p["amount"] == 1200 and p["due_date"] == "2026-11-30"
    assert p["reference"] == "INV-2026-104"


def test_parse_chinese_bill_and_paid():
    p = P("测试银行信用卡电子账单", "本期应还金额：¥3,456.78\n到期还款日：2026年11月18日", sender="测试银行 <bill@testbank.example>")
    assert p["type"] == "bill" and p["amount"] == 3456.78 and p["currency"] == "CNY" and p["due_date"] == "2026-11-18"
    p = P("还款成功通知", "收款方：测试银行\n还款金额：3456.78元", sender="测试银行 <bill@testbank.example>")
    assert p["type"] == "paid" and p["payee"] == "测试银行" and p["amount"] == 3456.78 and p["currency"] == "CNY"


def test_parse_payment_confirmation():
    p = P("Payment confirmation", "Thank you for your payment to Test Power Co.\nPayment amount: $123.45\nPayment date: 11/03/2026\n"
          "Confirmation number: CNF8812")
    assert p["type"] == "paid" and p["payee"] == "Test Power Co" and p["amount"] == 123.45 and p["paid_date"] == "2026-11-03"
    assert p["reference"] == "CNF8812"
    p = P("We received your payment", "Hi, your payment of $50.00 was received.", received=date(2026, 11, 2))
    assert p["type"] == "paid" and p["paid_date"] == "2026-11-02" and p["payee"] == "Test Power Co"


def test_parse_html_body():
    html = "<html><body><table><tr><td>Amount Due</td><td><b>$64.10</b></td></tr><tr><td>Due Date</td><td>Dec 1, 2026</td></tr></table></body></html>"
    p = P("Your Test Water bill", html, sender="Test Water <no-reply@testwater.example>")
    assert p["type"] == "bill" and p["amount"] == 64.10 and p["due_date"] == "2026-12-01" and p["payee"] == "Test Water"


@pytest.mark.parametrize("subject,body", [
    ("Your receipt from Test Coffee", "Total $4.50. Thanks for your order!"),
    ("Your Test Ride invoice", "Trip total $18.20. Paid with Visa ••4321"),  # an "invoice" that is a receipt (no due date / request)
    ("Weekly newsletter", "Our new products are here."),
    ("支付宝交易提醒", "您在测试商户支付成功 25.00元"),  # a purchase alert, not a bill payment
])
def test_parse_not_a_bill(subject, body):
    assert P(subject, body)["type"] in (None, "paid") and P(subject, body)["type"] != "bill"


def test_subject_wins_over_body_boilerplate():
    p = P("Your Test Power Co bill is ready", "Amount due $90.00, due Nov 20. If you already paid, thank you for your payment.")
    assert p["type"] == "bill"
    p = P("Payment received", "Thank you. Your next bill amount due will be shown on your statement.")
    assert p["type"] == "paid"


def test_payee_matching():
    assert bills.payee_matches("Test Power Co", "TEST POWER COMPANY, Inc.")
    assert bills.payee_matches("Test Power", "Test Power Co")
    assert bills.payee_matches("PG&E", "pg and e")
    assert not bills.payee_matches("Test Power Co", "Test Water")
    assert not bills.payee_matches("", "Test Water")


# ---------------------------------------------------------------- email ingest helpers
def mail(c, subject, body, sender="Test Power Co <billing@testpower.example>", received=None, mid=None):
    m = {"from": sender, "subject": subject, "body": body, "received_at": received or c.get("/api/bills").json()["today"] + " 09:00"}
    if mid:
        m["message_id"] = mid
    r = c.post("/api/ingest/email", json=m)
    assert r.status_code == 200, r.text
    return r.json()


def today(c):
    return date.fromisoformat(c.get("/api/bills").json()["today"])


def d(c, n):
    return (today(c) + timedelta(days=n)).isoformat()


def bill_rems(c, view="open"):
    return [r for r in c.get(f"/api/reminders?view={view}").json()["items"] if r["kind"] == "bill"]


def bill_email(c, amount="123.45", due_in=10, payee="Test Power Co", **kw):
    return mail(c, f"Your {payee} bill is ready", f"Amount due: ${amount}\nDue date: {d(c, due_in)}", **kw)


# ---------------------------------------------------------------- schedule creation
def test_bill_email_creates_task_and_reminder(client):
    r = bill_email(client, due_in=10)
    assert r["result"] == "bill" and r["action"] == "created"
    b = r["bill"]
    assert b["payee"] == "Test Power Co" and b["amount"] == 123.45 and b["due_date"] == d(client, 10) and b["status"] == "open"
    assert b["source"] == "email" and b["message_id"] and b["sender_domain"] == "testpower.example"
    assert client.get("/api/transactions").json()["items"] == []  # nothing paid yet: no transaction
    # default lead 3 days: not shown yet 10 days out, but in "all upcoming"
    assert bill_rems(client) == []
    later = bill_rems(client, "all_upcoming")
    assert len(later) == 1 and later[0]["state"] == "later" and later[0]["lead_days"] == 3 and later[0]["payee"] == "Test Power Co"
    log = client.get("/api/ingest-log").json()
    assert any(x["result"] == "bill" and "Test Power Co" in x["detail"] for x in (log["items"] if isinstance(log, dict) else log))


def test_reminder_lead_time_default_setting_and_per_bill(client):
    bill_email(client, due_in=3)
    assert len(bill_rems(client)) == 1  # due in 3 days, lead 3 -> shown
    b2 = client.post("/api/bills", json={"payee": "Test Internet", "amount": 60, "due_date": d(client, 5)}).json()
    assert len(bill_rems(client)) == 1
    client.put("/api/settings", json={"bill_reminder_lead_days": "7"})
    assert len(bill_rems(client)) == 2
    client.put(f"/api/bills/{b2['id']}", json={"lead_days": 1})
    assert [r["title"] for r in bill_rems(client)] == ["Test Power Co"]
    assert client.put("/api/settings", json={"bill_reminder_lead_days": "abc"}).status_code == 400


def test_duplicate_bill_email_links_instead_of_duplicating(client):
    bill_email(client, due_in=10, mid="<bill-1@test>")
    r = bill_email(client, due_in=10, mid="<bill-1-reminder@test>")
    assert r["result"] == "bill" and r["action"] == "existing"
    assert len(client.get("/api/bills").json()["items"]) == 1
    # a different due date = next month's bill = a new task
    bill_email(client, due_in=40, mid="<bill-2@test>")
    assert len(client.get("/api/bills").json()["items"]) == 2


def test_bill_without_due_date_still_scheduled(client):
    r = mail(client, "Test Landscaping requested a payment of $250.00", "Please pay $250.00 for October service.",
             sender="Pay App <requests@payapp.example>")
    assert r["result"] == "bill" and r["bill"]["due_date"] is None and r["bill"]["amount"] == 250
    rem = bill_rems(client)
    assert len(rem) == 1 and rem[0]["due_date"] == today(client).isoformat()  # due "now" (the request date)


# ---------------------------------------------------------------- payment matching
def test_confirmation_matches_and_marks_paid(client):
    b = bill_email(client, due_in=2)["bill"]
    assert len(bill_rems(client)) == 1
    r = mail(client, "Payment confirmation", f"Thank you for your payment to Test Power Co.\nPayment amount: $123.45\nPayment date: {d(client, 1)}",
             mid="<paid-1@test>")
    assert r["result"] == "bill_paid" and r["bill"]["id"] == b["id"]
    b = client.get(f"/api/bills/{b['id']}").json()
    assert b["status"] == "paid" and b["paid_at"] == d(client, 1) and b["paid_amount"] == 123.45 and b["matched"] == "auto"
    assert b["paid_message_id"] == "<paid-1@test>" and b["paid_subject"] == "Payment confirmation"
    assert bill_rems(client) == [] and bill_rems(client, "done")[0]["done_reason"] == "paid"
    assert client.get("/api/transactions").json()["items"] == []  # no duplicate of the bank's own debit alert
    assert client.get("/api/bills/payments").json()["items"] == []


def test_amount_within_tolerance_and_outside(client):
    b = bill_email(client, amount="100.00", due_in=5)["bill"]
    r = mail(client, "Payment received", "We received your payment of $100.80 for Test Power Co.", mid="<p-tol@test>")
    assert r["result"] == "bill_paid" and r["bill"]["id"] == b["id"]  # within the default 1.00
    b2 = bill_email(client, amount="200.00", due_in=35, mid="<b2@test>")["bill"]
    r = mail(client, "Payment received", "We received your payment of $195.00 for Test Power Co.", mid="<p-off@test>")
    assert r["result"] != "bill_paid"
    assert client.get(f"/api/bills/{b2['id']}").json()["status"] == "open"
    pay = client.get("/api/bills/payments").json()["items"]
    assert len(pay) == 1 and pay[0]["status"] == "unmatched" and pay[0]["amount"] == 195
    assert [s["id"] for s in pay[0]["suggestions"]] == [b2["id"]]  # same payee offered for a manual match


def test_payment_outside_date_window_is_not_matched(client):
    b = client.post("/api/bills", json={"payee": "Test Power Co", "amount": 50, "due_date": d(client, -30),
                                        "issued_date": d(client, -50)}).json()
    r = mail(client, "Payment received", "We received your payment of $50.00 to Test Power Co.", mid="<late@test>")
    assert r["result"] != "bill_paid" and client.get(f"/api/bills/{b['id']}").json()["status"] == "open"
    client.put("/api/settings", json={"bill_match_days": "45"})
    r = mail(client, "Payment received", "We received your payment of $50.00 to Test Power Co.", mid="<late2@test>")
    assert r["result"] == "bill_paid"


def test_no_match_goes_to_review_and_manual_match(client, accounts):
    b = bill_email(client, amount="75.00", due_in=5)["bill"]
    r = mail(client, "Payment received", "We received your payment of $20.00. Payee: Test Power Co", mid="<nm@test>")
    assert r["result"] != "bill_paid"
    assert client.get("/api/meta").json()["bill_review_count"] == 1
    pay = client.get("/api/bills/payments").json()["items"][0]
    assert pay["status"] == "unmatched" and pay["bill_id"] is None and "no open bill" in pay["reason"]
    # the user decides: it was this bill
    out = client.post(f"/api/bills/payments/{pay['id']}/match", json={"bill_id": b["id"]}).json()
    assert out["status"] == "paid" and out["matched"] == "manual" and out["paid_message_id"] == "<nm@test>"
    assert client.get("/api/bills/payments").json()["items"] == []
    assert client.get("/api/bills/payments?status=matched").json()["items"][0]["bill_id"] == b["id"]


def test_unrelated_confirmation_is_not_queued(client, accounts):
    # a card payment confirmation with no bill from that payee: normal flow (transfer to review), no bill review entry
    r = mail(client, "Thank you for your payment", "Your payment of $500.00 to your Chase Sapphire Test card ending 9999 was received.",
             sender="Chase <no-reply@chase.example>")
    assert r["result"] == "review"
    assert client.get("/api/bills/payments?status=all").json()["items"] == []


def test_ambiguous_payment_left_for_review(client):
    b1 = client.post("/api/bills", json={"payee": "Test Power Co", "amount": 40, "due_date": d(client, 3)}).json()
    b2 = client.post("/api/bills", json={"payee": "Test Power Co", "amount": 40.5, "due_date": d(client, 6), "reference": "meter 2"}).json()
    r = mail(client, "Payment received", "We received your payment of $40.25 for Test Power Co.", mid="<amb@test>")
    assert r["result"] != "bill_paid"
    assert client.get(f"/api/bills/{b1['id']}").json()["status"] == "open" and client.get(f"/api/bills/{b2['id']}").json()["status"] == "open"
    pay = client.get("/api/bills/payments").json()["items"]
    assert len(pay) == 1 and pay[0]["status"] == "ambiguous" and sorted(pay[0]["candidates"]) == sorted([b1["id"], b2["id"]])
    # dismiss: it stays out of the queue, bills untouched
    client.post(f"/api/bills/payments/{pay[0]['id']}/dismiss")
    assert client.get("/api/bills/payments").json()["items"] == [] and client.get("/api/meta").json()["bill_review_count"] == 0


def test_match_payment_unit(db):
    from app import bills as B
    B.create(db, {"payee": "Test Gas", "amount": 30, "due_date": "2026-11-10", "issued_date": "2026-10-20"})
    base = {"payee": "Test Gas Co", "amount": 30, "paid_date": "2026-11-09", "sender_domain": None, "currency": None}
    assert B.match_payment(db, base)["status"] == "matched"
    assert B.match_payment(db, {**base, "amount": None})["status"] == "unmatched"
    assert B.match_payment(db, {**base, "payee": "Other Utility"})["status"] == "unmatched"
    assert B.match_payment(db, {**base, "paid_date": "2026-10-01"})["status"] == "unmatched"  # before the bill was issued
    assert B.match_payment(db, {**base, "paid_date": "2026-11-21"})["status"] == "unmatched"  # > 10 days after due
    assert B.match_payment(db, {**base, "amount": 30.99})["status"] == "matched"
    assert B.match_payment(db, {**base, "amount": 31.01})["status"] == "unmatched"
    B.create(db, {"payee": "Test Gas", "amount": 30.5, "due_date": "2026-11-12", "issued_date": "2026-10-20"})
    assert B.match_payment(db, base)["status"] == "ambiguous"
    # a bill without an amount is never auto-matched
    B.create(db, {"payee": "Test Trash", "due_date": "2026-11-10", "issued_date": "2026-10-20"})
    assert B.match_payment(db, {**base, "payee": "Test Trash"})["status"] == "unmatched"


def test_sender_domain_match(client):
    b = mail(client, "Your bill is ready", "Amount due: $33.00\nDue date: " + d(client, 4), sender="billing@testwater.example")["bill"]
    r = mail(client, "Payment received", "We received your payment of $33.00.", sender="Water Payments <payments@testwater.example>",
             mid="<w@test>")
    assert r["result"] == "bill_paid" and r["bill"]["id"] == b["id"]


# ---------------------------------------------------------------- manual task actions
def test_manual_paid_creates_payment_and_reopen(client, accounts):
    b = client.post("/api/bills", json={"payee": "Test Internet", "amount": 60, "due_date": d(client, 1),
                                        "account_id": accounts["checking"]["id"]}).json()
    rem = bill_rems(client)[0]
    # mark paid from the Reminder Center -> the bill is paid, an expense recorded from the checking account
    out = client.post(f"/api/reminders/{rem['id']}/paid", json={"create_payment": True}).json()
    assert out["status"] == "done" and out["transaction"]["amount"] == 60 and out["transaction"]["kind"] == "expense"
    b = client.get(f"/api/bills/{b['id']}").json()
    assert b["status"] == "paid" and b["transaction_id"] == out["transaction"]["id"] and b["matched"] == "manual"
    assert client.post(f"/api/bills/{b['id']}/paid", json={}).status_code == 400
    client.post(f"/api/bills/{b['id']}/reopen")
    assert len(bill_rems(client)) == 1
    client.post(f"/api/bills/{b['id']}/cancel")
    assert bill_rems(client) == [] and client.get("/api/bills?status=cancelled").json()["items"][0]["id"] == b["id"]
    client.delete(f"/api/bills/{b['id']}")
    assert client.get(f"/api/bills/{b['id']}").status_code == 404


def test_bill_validation_and_parse_endpoint(client):
    assert client.post("/api/bills", json={"amount": 5}).status_code == 400
    assert client.post("/api/bills", json={"payee": "X Test", "amount": -1}).status_code == 400
    assert client.post("/api/bills", json={"payee": "X Test", "due_date": "soon"}).status_code == 400
    p = client.post("/api/bills/parse", json={"from": "Test Power Co <b@testpower.example>", "subject": "Your bill is ready",
                                              "body": "Amount due $10.00 due 12/01/2026"}).json()
    assert p["parsed"]["type"] == "bill" and p["parsed"]["due_date"] == "2026-12-01"
    assert client.get("/api/bills").json()["items"] == []  # dry run saved nothing


def test_rule_kind_bill_forces_type(client):
    r = client.post("/api/email-rules", json={"name": "Test HOA", "sender_pattern": "hoa\\.example", "amount_regex": r"Assessment:\s*\$([\d.]+)",
                                              "kind": "bill"})
    assert r.status_code == 200, r.text
    out = mail(client, "October statement", "Assessment: $310.00\nDue 2026-12-01", sender="Test HOA <mgr@hoa.example>")
    assert out["result"] == "bill" and out["bill"]["amount"] == 310 and out["bill"]["payee"] == "Test HOA"


@pytest.mark.parametrize("body,payee", [
    ("Thank you for your payment of $80.00 to Comcast on 10/24/2026.", "Comcast"),
    ("Your payment of 1,234.50 USD to City Water was processed.", "City Water"),
    ("Thank you for paying Comcast.", "Comcast"),
    ("Payment to PG&E of $10.00 was received.", "PG&E"),
])
def test_confirmation_payee_phrasings(body, payee):
    p = bills.parse_bill_email("", "Payment confirmation", body, "2026-10-24")
    assert p["type"] == "paid" and p["payee"] == payee
