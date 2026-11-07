"""The same charge recorded twice: an email alert and your own entry (receipt / form / Shortcut) with the same amount on
the same account the same day, whatever the merchant. FAKE data only."""
from app import ingest


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


RULE = {"name": "Fake bank", "sender_pattern": r"fakebank\.test", "kind": "expense", "amount_regex": r"\$([\d.]+)",
        "merchant_regex": r"on your (Fake \w+ \w+) account", "priority": 1}


def mail(amount, when, n):
    return {"from": "a@fakebank.test", "subject": f"A ${amount} transaction was made on your Fake Anywhere card account",
            "body": f"A ${amount} transaction was made on your Fake Anywhere card account. Chase Sapphire.",
            "received_at": when, "message_id": f"<twin{n}@x>"}


def test_email_after_your_entry_goes_to_review_whatever_the_merchant(client, accounts, db):
    ok(client.post("/api/email-rules", json=RULE))
    card = accounts["sapphire"]["id"]
    mine = ok(client.post("/api/transactions", json={"kind": "expense", "date": "2099-04-10 09:00", "amount": 35.75,
                                                      "account_id": card, "merchant": "Fake Poultry Market"}))
    r = ok(client.post("/api/ingest/email", json=mail("35.75", "2099-04-10 18:30", 1)))
    assert r["result"] == "review" and r["confidence"] <= 0.3
    assert any(f"possible duplicate of #{mine['id']} (same amount, account and day)" in x for x in r["reasons"])
    rv = ok(client.get("/api/review"))["items"]
    it = next(x for x in rv if x["id"] == r["transaction"]["id"])
    assert it["dup_of"]["id"] == mine["id"] and it["dup_of"]["merchant_name"] == "Fake Poultry Market"

    # another day or another amount: a real purchase
    for i, (amt, when) in enumerate([("35.75", "2099-04-11 09:00"), ("99.00", "2099-04-10 10:00")]):
        r = ok(client.post("/api/ingest/email", json=mail(amt, when, 10 + i)))
        assert not any("possible duplicate" in x for x in r["reasons"]), (amt, when)

    # the mailbox switch off: posted / reviewed as before (merchant must match)
    ok(client.post("/api/transactions", json={"kind": "expense", "date": "2099-04-12 09:00", "amount": 35.75,
                                              "account_id": card, "merchant": "Fake Poultry Market"}))
    res = ingest.ingest_email(db, mail("35.75", "2099-04-12 18:30", 20), mailbox={"id": None, "dup_same_day": 0, "auto_post": 1})
    assert not any("possible duplicate" in x for x in res["reasons"])


def test_your_entry_after_the_email_sends_the_email_copy_to_review(client, accounts, db):
    ok(client.post("/api/email-rules", json=RULE))
    db.set_setting("review_threshold", "0.1")  # the fake email posts
    card = accounts["sapphire"]["id"]
    r = ok(client.post("/api/ingest/email", json={**mail("59.73", "2099-05-02 14:15", 30), "auto_post": True}))
    em = r["transaction"]
    assert em["status"] == "posted"
    mine = ok(client.post("/api/transactions", json={"kind": "expense", "date": "2099-05-02 14:18", "amount": 59.73,
                                                      "account_id": card, "merchant": "Fake Ranch Market", "source": "agent"}))
    assert mine["email_twins"] == [em["id"]] and mine["status"] == "posted"
    e2 = ok(client.get(f"/api/transactions/{em['id']}"))
    assert e2["status"] == "review" and e2["confidence"] <= 0.3  # nothing deleted
    assert ok(client.get("/api/review"))["items"][0]["dup_of"]["id"] == mine["id"]

    # an email transaction you already approved is left alone; other accounts / amounts too
    with db.tx() as con:  # created a while ago (the test runs within one second)
        con.execute("UPDATE transactions SET created_at='2020-01-01 00:00:00', updated_at='2020-01-01 00:00:00' WHERE id=?", (em["id"],))
    ok(client.post(f"/api/transactions/{em['id']}/approve", json={}))
    again = ok(client.post("/api/transactions", json={"kind": "expense", "date": "2099-05-02 19:00", "amount": 59.73,
                                                       "account_id": card, "merchant": "Fake Ranch Market"}))
    assert "email_twins" not in again
    assert ok(client.get(f"/api/transactions/{em['id']}"))["status"] == "posted"


def test_agent_reply_says_the_email_copy_moved(client, accounts, db):
    ok(client.post("/api/email-rules", json=RULE))
    db.set_setting("review_threshold", "0.1")
    r = ok(client.post("/api/ingest/email", json={**mail("12.34", "2099-06-01 08:00", 40), "auto_post": True}))
    out = ok(client.post("/api/agent/transactions", json={"amount": 12.34, "account": "Chase Sapphire", "merchant": "Fake Cafe",
                                                           "date": "2099-06-01", "time": "08:05"}))
    assert any(f"#{r['transaction']['id']}" in w and "To review" in w for w in out["warnings"]), out
