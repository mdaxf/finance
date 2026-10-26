"""Email parsing-rule actions: bills with rule fields, schedule post / new, loan payment, credit, miles & points balance,
the rule tester preview and the mailbox watch_rules filter. FAKE data only."""
from app import ingest


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def rule(client, **kw):
    return ok(client.post("/api/email-rules", json={"priority": 1, **kw}))


def mail(client, sender, subject, body, received="2099-03-02 08:00"):
    return ok(client.post("/api/ingest/email", json={"from": sender, "subject": subject, "body": body, "received_at": received}))


def test_bill_rule_fields_payee_due_date_account(client, accounts):
    chk = accounts["checking"]["id"]
    rule(client, name="Fake water", sender_pattern=r"fakewater\.test", kind="bill", payee="Fake Water Co",
         amount_regex=r"Total:\s*\$([\d.]+)", date_regex=r"Pay before\s+(\S+)", account_id=chk)
    body = "Hello. Total: $48.20. Pay before 2099-03-20. Ignore $5.00 fee notice."
    t = ok(client.post("/api/email-rules/test", json={"from": "x@fakewater.test", "subject": "Your statement", "body": body}))
    assert t["action_preview"]["action"] == "create" and "due 2099-03-20" in t["action_preview"]["detail"]
    r = mail(client, "Water <x@fakewater.test>", "Your statement", body)
    assert r["result"] == "bill" and r["action"] == "created"
    b = r["bill"]
    assert (b["payee"], b["amount"], b["due_date"], b["account_id"]) == ("Fake Water Co", 48.2, "2099-03-20", chk)
    log = ok(client.get("/api/ingest-log"))[0]
    assert (log["result"], log["ref_kind"], log["ref_id"]) == ("bill", "bill", b["id"])
    # the tester now sees the open bill
    t = ok(client.post("/api/email-rules/test", json={"from": "x@fakewater.test", "subject": "Your statement", "body": body}))
    assert t["action_preview"]["action"] == "existing"


def test_schedule_post_marks_occurrence_and_no_double_count(client, accounts, db):
    chk = accounts["checking"]["id"]
    s = ok(client.post("/api/schedules", json={"name": "Fake salary", "freq": "monthly", "start_date": "2099-01-01",
                                               "post_mode": "remind", "template": {"kind": "income", "amount": 3000, "account_id": chk}}))
    rule(client, name="Fake paid", sender_pattern=r"fakebank\.test", subject_pattern="got paid", kind="schedule_post",
         target_id=s["id"], amount_regex=r"\$([\d,.]+)")
    t = ok(client.post("/api/email-rules/test", json={"from": "a@fakebank.test", "subject": "You just got paid",
                                                       "body": "A deposit of $3,050.00", "received_at": "2099-03-02 08:00"}))
    assert t["action_preview"]["action"] == "post" and t["action_preview"]["occurrence_date"] == "2099-03-01"
    r = mail(client, "a@fakebank.test", "You just got paid", "A deposit of $3,050.00")
    assert r["result"] == "schedule_post" and r["action"] == "posted" and r["transaction"]["amount"] == 3050
    assert r["transaction"]["schedule_id"] == s["id"] and r["transaction"]["kind"] == "income"
    # the daily run won't post that date; a second alert for it changes nothing
    r2 = mail(client, "a@fakebank.test", "You just got paid", "A deposit of $3,050.00 (copy)")
    assert r2["action"] == "existing"
    occ = db.all("SELECT occurrence_date, transaction_id FROM schedule_occurrences WHERE schedule_id=?", (s["id"],))
    assert occ == [{"occurrence_date": "2099-03-01", "transaction_id": r["transaction"]["id"]}]
    # far from any date of the schedule -> a transaction to review instead, with the reason
    s2 = ok(client.post("/api/schedules", json={"name": "Fake yearly", "freq": "yearly", "start_date": "2099-12-01",
                                                "template": {"kind": "income", "amount": 10, "account_id": chk}}))
    rule(client, name="Fake yearly", sender_pattern=r"yearly\.test", kind="schedule_post", target_id=s2["id"])
    r3 = mail(client, "a@yearly.test", "Bonus", "You got $10.00")
    assert r3["result"] == "review" and any("no date of" in x for x in r3["reasons"])


def test_schedule_new_one_time_reminder(client, accounts):
    chk = accounts["checking"]["id"]
    rule(client, name="Fake autopay", sender_pattern=r"fakeisp\.test", kind="schedule_new", payee="Fake ISP", account_id=chk)
    body = "Your autopay of $79.99 is scheduled for March 15, 2099."
    r = mail(client, "bill@fakeisp.test", "Upcoming autopay", body)
    assert r["result"] == "schedule_new" and r["action"] == "created"
    s = ok(client.get(f"/api/schedules/{r['ref_id']}"))
    assert (s["freq"], s["start_date"], s["post_mode"], s["template"]["amount"]) == ("once", "2099-03-15", "remind", 79.99)
    assert mail(client, "bill@fakeisp.test", "Upcoming autopay (again)", body)["action"] == "existing"


def test_loan_payment_direct_and_duplicate(client, accounts):
    bank = accounts["checking"]["id"]
    x = ok(client.post("/api/loans", json={"direction": "lent", "counterparty": "Fake Cousin", "principal": 500, "pay_account_id": bank,
                                           "start_date": "2099-01-01"}))
    rule(client, name="Fake repay", sender_pattern=r"fakepay\.test", kind="loan_payment", target_id=x["id"], amount_regex=r"\$([\d.]+)")
    r = mail(client, "p@fakepay.test", "You received money", "Fake Cousin sent you $200.00")
    assert r["result"] == "loan_payment" and r["action"] == "recorded" and r["ref_id"] == x["id"]
    assert ok(client.get(f"/api/loans/{x['id']}"))["outstanding"] == 300
    assert mail(client, "p@fakepay.test", "You received money", "Fake Cousin sent you $200.00 again")["action"] == "existing"


def test_credit_and_reward_balance(client, accounts):
    rule(client, name="Fake air credit", sender_pattern=r"fakeair\.test", subject_pattern="credit", kind="credit", payee="Fake Air")
    r = mail(client, "x@fakeair.test", "Your travel credit", "A travel credit of $210.00 for your cancelled trip. Expires 2099-12-31.")
    assert r["result"] == "credit" and r["action"] == "created"
    c = ok(client.get(f"/api/credits/{r['ref_id']}"))
    assert (c["title"], c["amount"], c["expiry_date"], c["source"], c["account_id"]) == ("Fake Air", 210, "2099-12-31", "cancel", None)
    p = ok(client.post("/api/rewards", json={"name": "Fake Air Miles", "kind": "airline", "balance": 1000}))
    rule(client, name="Fake miles", sender_pattern=r"fakeair\.test", subject_pattern="statement", kind="reward_balance", target_id=p["id"])
    r = mail(client, "x@fakeair.test", "Your miles statement", "Balance: 12,345 miles. Miles expire on 2100-06-30. Spend $5 to earn.")
    assert r["result"] == "reward_balance" and r["action"] == "updated"
    p = ok(client.get(f"/api/rewards/{p['id']}"))
    assert p["balance"] == 12345 and p["expiry_date"] == "2100-06-30"
    assert mail(client, "x@fakeair.test", "Your miles statement", "Balance: 12,345 miles.")["action"] == "unchanged"


def test_rule_validation(client):
    assert client.post("/api/email-rules", json={"name": "X", "sender_pattern": "x", "kind": "schedule_post", "target_id": 9999}).status_code == 400
    assert client.post("/api/email-rules", json={"name": "X", "sender_pattern": "x", "kind": "bill", "date_regex": "("}).status_code == 400
    assert client.post("/api/email-rules", json={"name": "X", "sender_pattern": "x", "kind": "boat"}).status_code == 400


def test_watch_rules_extra_reason():
    rules = [{"enabled": 1, "kind": "credit", "sender_pattern": r"fakeair\.test", "subject_pattern": ""},
             {"enabled": 1, "kind": "expense", "sender_pattern": r"shop\.test", "subject_pattern": ""}]
    assert ingest.extra_reason(rules, "x@fakeair.test", "Hello") == "rule"
    assert ingest.extra_reason(rules, "x@shop.test", "Order shipped") is None  # a plain-transaction rule doesn't widen the filter
    assert ingest.extra_reason(rules, "x@power.test", "Your bill is ready") == "bill"
    assert ingest.extra_reason([], "x@power.test", "Thank you for your payment") == "bill"


def test_bills_only_never_makes_a_transaction(client, db):
    out = ingest.ingest_email(db, {"from": "news@shop.test", "subject": "Your bill is ready", "body": "Sale! 20% off, $5.00 coupon",
                                   "received_at": "2099-03-02 08:00"}, bills_only=True)
    assert out["result"] in ("skipped", "bill") and not out.get("transaction")
    assert db.one("SELECT COUNT(*) n FROM transactions WHERE external_id IS NOT NULL AND source='email'")["n"] == 0


def test_loan_with_schedule_posts_that_installment(client, accounts, db):
    bank = accounts["checking"]["id"]
    x = ok(client.post("/api/loans", json={"direction": "borrowed", "counterparty": "Fake Mortgage Bank", "kind": "bank",
                                           "principal": 100000, "pay_account_id": bank, "rate": 6, "freq": "monthly",
                                           "first_due": "2099-02-01", "term": 360, "start_date": "2099-01-01"}))
    ok(client.post(f"/api/loans/{x['id']}/schedule", json={"post_mode": "remind"}))
    sid = db.one("SELECT schedule_id FROM loans WHERE id=?", (x["id"],))["schedule_id"]
    assert sid
    rule(client, name="Fake mortgage", sender_pattern=r"fakemortgage\.test", kind="loan_payment", target_id=x["id"])
    r = mail(client, "s@fakemortgage.test", "Payment received", "Thank you, we received your payment.", received="2099-02-02 09:00")
    assert r["result"] == "loan_payment" and r["action"] == "posted" and r["ref_kind"] == "schedule"
    assert db.one("SELECT COUNT(*) n FROM loan_events WHERE loan_id=? AND kind='payment' AND due_date='2099-02-01'", (x["id"],))["n"] == 1
    again = mail(client, "s@fakemortgage.test", "Payment received", "Thanks again, we received your payment.", received="2099-02-03 09:00")
    assert again["action"] == "existing"
