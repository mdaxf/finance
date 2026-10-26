"""Email ingest: a transaction already in the system (same amount + account + merchant) is skipped within an hour,
or created in To review at low confidence on the same day. FAKE data only."""


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def setup(client, accounts):
    ok(client.post("/api/email-rules", json={"name": "Fake bank", "sender_pattern": r"fakebank\.test", "kind": "expense",
                                             "amount_regex": r"\$([\d.]+)", "merchant_regex": r"at (Fake \w+)", "priority": 1}))
    card = accounts["sapphire"]["id"]
    tx = ok(client.post("/api/transactions", json={"kind": "expense", "date": "2099-04-10 12:10", "amount": 23.45,
                                                    "account_id": card, "merchant": "Fake Bistro"}))
    return card, tx


def mail(client, body, when, n):
    return ok(client.post("/api/ingest/email", json={"from": "a@fakebank.test", "subject": "Chase Sapphire alert",
                                                     "body": body, "received_at": when, "message_id": f"<sim{n}@x>"}))


def test_within_an_hour_is_skipped(client, accounts):
    card, tx = setup(client, accounts)
    r = mail(client, "You spent $23.45 at Fake Bistro on your Chase Sapphire.", "2099-04-10 12:40", 1)
    assert r["result"] == "similar" and r["similar_to"] == tx["id"] and not r["created"]
    log = ok(client.get("/api/ingest-log"))[0]
    assert log["result"] == "similar" and log["transaction_id"] == tx["id"]
    n = ok(client.get("/api/transactions", params={"account_id": card}))
    assert len(n["items"] if isinstance(n, dict) else n) == 1


def test_same_day_goes_to_review_low_confidence(client, accounts):
    setup(client, accounts)
    r = mail(client, "You spent $23.45 at Fake Bistro on your Chase Sapphire.", "2099-04-10 19:30", 2)
    assert r["result"] == "review" and r["confidence"] <= 0.3
    assert any("possible duplicate" in x for x in r["reasons"])


def test_no_merchant_in_email_is_review_not_skip(client, accounts):
    setup(client, accounts)
    r = mail(client, "A charge of $23.45 was made on your Chase Sapphire.", "2099-04-10 12:10", 3)
    assert r["result"] == "review" and any("possible duplicate" in x for x in r["reasons"])


def test_different_merchant_amount_or_day_is_not_similar(client, accounts):
    setup(client, accounts)
    for i, (body, when) in enumerate([("You spent $23.45 at Fake Cinema on your Chase Sapphire.", "2099-04-10 12:05"),
                                      ("You spent $50.00 at Fake Bistro on your Chase Sapphire.", "2099-04-10 12:05"),
                                      ("You spent $23.45 at Fake Bistro on your Chase Sapphire.", "2099-04-11 12:05")]):
        r = mail(client, body, when, 10 + i)
        assert r["created"] and not any("possible duplicate" in x for x in r["reasons"]), (body, when, r["reasons"])


def test_import_without_time_counts_same_day_only(client, accounts):
    card = accounts["sapphire"]["id"]
    ok(client.post("/api/email-rules", json={"name": "Fake bank", "sender_pattern": r"fakebank\.test", "kind": "expense",
                                             "amount_regex": r"\$([\d.]+)", "merchant_regex": r"at (Fake \w+)", "priority": 1}))
    ok(client.post("/api/transactions", json={"kind": "expense", "date": "2099-04-10", "amount": 23.45,
                                              "account_id": card, "merchant": "FAKE BISTRO #12"}))
    r = mail(client, "You spent $23.45 at Fake Bistro on your Chase Sapphire.", "2099-04-10 00:20", 20)
    assert r["result"] == "review" and any("possible duplicate" in x for x in r["reasons"])


def test_meal_with_tip_added_later_is_review(client, accounts):
    card, tx = setup(client, accounts)  # 23.45 at Fake Bistro, first charge without the tip
    r = mail(client, "You spent $28.14 at Fake Bistro on your Chase Sapphire.", "2099-04-10 21:00", 30)
    assert r["result"] == "review" and r["confidence"] <= 0.3
    assert any(f"possible duplicate of #{tx['id']}" in x and "maybe a tip" in x for x in r["reasons"])
    r = mail(client, "You spent $40.00 at Fake Bistro on your Chase Sapphire.", "2099-04-10 21:30", 31)  # +70%: a new meal
    assert not any("possible duplicate" in x for x in r["reasons"])
