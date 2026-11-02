"""Credits 抵用金 in the ledger: issuing records income / a refund in the holding account, each use records (or links)
spending, closing takes the forfeited part off, every transaction shows which credit it belongs to. FAKE data."""
from datetime import date, timedelta


def mk(c, **kw):
    r = c.post("/api/accounts", json=kw)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def credit(c, **kw):
    r = c.post("/api/credits", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def exp(c, n=90):
    return (date.fromisoformat(c.get("/api/credits").json()["today"]) + timedelta(days=n)).isoformat()


def bal(c, aid):
    return next(a["balance"] for a in c.get("/api/accounts").json()["accounts"] if a["id"] == aid)


def tx(c, tid):
    return c.get(f"/api/transactions/{tid}").json()


def cat(c, kind, name=None):
    flat = []
    def walk(xs):
        for x in xs:
            flat.append(x)
            walk(x.get("children") or [])
    walk(c.get("/api/meta").json()["categories"])
    return next(x["id"] for x in flat if x["kind"] == kind and (name is None or x["name"] == name))


def test_bonus_on_issuer_only_credit_uses_the_credits_account(client):
    c = credit(client, title="Fake Internet Co", source="bonus", amount=100, expiry_date=exp(client))
    assert c["ledger"] == 1 and c["issue_tx"] and c["hold_account_name"].startswith("Credits")
    hold = c["hold_account_id"]
    t = tx(client, c["issue_tx_id"])
    assert t["kind"] == "income" and t["amount"] == 100 and t["account_id"] == hold and t["category_name"] == "Cashback & Rewards"
    assert t["credits"] == [{"id": c["id"], "title": "Fake Internet Co", "role": "issue"}]
    assert bal(client, hold) == 100
    # use 30: spending from the holding account, in the chosen category
    food = cat(client, "expense")
    u = client.post(f"/api/credits/{c['id']}/use", json={"amount": 30, "category_id": food, "merchant": "Fake Internet Co"}).json()
    assert u["remaining"] == 70 and bal(client, hold) == 70
    sp = tx(client, u["uses"][0]["transaction_id"])
    assert sp["kind"] == "expense" and sp["amount"] == 30 and sp["category_id"] == food and sp["credits"][0]["role"] == "use"
    # undo the use: its spending goes away
    client.delete(f"/api/credits/{c['id']}/uses/{u['uses'][0]['id']}")
    assert client.get(f"/api/transactions/{sp['id']}").status_code == 404 and bal(client, hold) == 100


def test_refund_credit_on_its_account_and_forfeit(client):
    w = mk(client, name="Fake Airline Wallet", type="ewallet", currency="USD", opening_balance=0)
    travel = cat(client, "expense")
    c = credit(client, account_id=w, source="cancel", amount=500, category_id=travel, expiry_date=exp(client))
    t = tx(client, c["issue_tx_id"])
    assert t["kind"] == "refund" and t["account_id"] == w and t["category_id"] == travel and bal(client, w) == 500
    client.post(f"/api/credits/{c['id']}/use", json={"amount": 200})
    assert bal(client, w) == 300
    # close: the 300 left is forfeited, so only 200 of the refund was really received
    v = client.post(f"/api/credits/{c['id']}/void", json={}).json()
    assert tx(client, v["issue_tx_id"])["amount"] == 200 and bal(client, w) == 0 and not v["writeoff_tx_id"]
    r = client.post(f"/api/credits/{c['id']}/reopen").json()
    assert tx(client, r["issue_tx_id"])["amount"] == 500 and bal(client, w) == 300


def test_link_recorded_transaction_and_writeoff(client):
    w = mk(client, name="Fake Airline Wallet", type="ewallet", currency="USD", opening_balance=0)
    old = client.post("/api/transactions", json={"kind": "income", "amount": 80, "account_id": w, "date": "2026-01-05"}).json()
    c = credit(client, account_id=w, source="refund", amount=80, issue_mode="link", issue_tx_id=old["id"], expiry_date=exp(client))
    assert c["issue_tx_id"] == old["id"] and not c["issue_auto"] and bal(client, w) == 80  # nothing added twice
    assert tx(client, old["id"])["credits"][0]["role"] == "issue"
    v = client.post(f"/api/credits/{c['id']}/void", json={}).json()  # linked: the forfeit is a write-off expense
    wo = tx(client, v["writeoff_tx_id"])
    assert wo["kind"] == "expense" and wo["amount"] == 80 and bal(client, w) == 0
    client.post(f"/api/credits/{c['id']}/reopen")
    assert client.get(f"/api/transactions/{wo['id']}").status_code == 404 and bal(client, w) == 80
    # a link to a transaction on another account is refused
    other = mk(client, name="Fake Bank", type="checking", currency="USD", opening_balance=0)
    t2 = client.post("/api/transactions", json={"kind": "income", "amount": 5, "account_id": other}).json()
    r = client.post("/api/credits", json={"account_id": w, "source": "bonus", "amount": 5, "issue_mode": "link",
                                          "issue_tx_id": t2["id"], "expiry_date": exp(client)})
    assert r.status_code == 400 and "not on this credit's account" in r.json()["detail"]


def test_already_in_balance_and_card_statement_credit_link(client):
    card = mk(client, name="Fake Visa", type="credit_card", currency="USD", opening_balance=0)
    c = credit(client, account_id=card, source="bonus", amount=40, issue_mode="balance", expiry_date=exp(client))
    assert not c["issue_tx_id"] and bal(client, card) == 0
    charge = client.post("/api/transactions", json={"kind": "expense", "amount": 40, "account_id": card}).json()
    u = client.post(f"/api/credits/{c['id']}/use", json={"transaction_id": charge["id"]}).json()
    assert u["state"] == "used" and u["uses"][0]["transaction_id"] == charge["id"] and not u["uses"][0]["auto_tx"]
    assert tx(client, charge["id"])["credits"][0]["role"] == "use"
    # deleting the linked charge only unlinks it (the use stays)
    client.delete(f"/api/transactions/{charge['id']}")
    assert client.get(f"/api/credits/{c['id']}").json()["uses"][0]["transaction_id"] is None


def test_ledger_switch_and_delete(client):
    c = credit(client, title="Fake Store", source="bonus", amount=25, expiry_date=exp(client))
    client.post(f"/api/credits/{c['id']}/use", json={"amount": 10})
    c = client.get(f"/api/credits/{c['id']}").json()
    made = [c["issue_tx_id"], c["uses"][0]["transaction_id"]]
    off = client.put(f"/api/credits/{c['id']}", json={"ledger": False}).json()
    assert all(client.get(f"/api/transactions/{t}").status_code == 404 for t in made) and off["remaining"] == 15
    on = client.put(f"/api/credits/{c['id']}", json={"ledger": True}).json()
    assert on["issue_tx_id"] and on["uses"][0]["transaction_id"] and bal(client, on["hold_account_id"]) == 15
    # deleting the auto spending in the transaction list gives the money back to the credit
    client.delete(f"/api/transactions/{on['uses'][0]['transaction_id']}")
    assert client.get(f"/api/credits/{c['id']}").json()["remaining"] == 25
    ids = [on["issue_tx_id"]]
    client.delete(f"/api/credits/{c['id']}")
    assert all(client.get(f"/api/transactions/{t}").status_code == 404 for t in ids)


def test_bonus_category_must_be_income(client):
    r = client.post("/api/credits", json={"title": "Fake", "source": "bonus", "amount": 5, "category_id": cat(client, "expense"),
                                          "expiry_date": exp(client)})
    assert r.status_code == 400 and "income" in r.json()["detail"]
