"""Check-out can flag the bill reimbursable 可报销; a reimbursement can be marked complete so its leftover is no longer
offered when linking. FAKE data only."""
from tests.test_reserves import TD, mk, res, settle, setup, stats, tx


def _reimb_items(c):
    return {x["id"]: x for x in c.get("/api/reimbursements?status=all").json()["items"]}


def test_checkout_reimbursable_or_expense(client):
    a = setup(client)
    s0 = stats(client)
    # hold, final bill recorded at check-out, reimbursable with a payer
    h = res(client, method="hold", account_id=a["card"], amount=300, merchant="Test Hotel", start_date=TD)
    dry = client.post(f"/api/reserves/{h['id']}/settle", json={"dry_run": True, "final": {"amount": 250}, "reimbursable": True}).json()
    assert dry["plan"][0]["reimbursable"] is True
    out = settle(client, h["id"], final={"amount": 250}, reimbursable=True, reimb_payer="Test Employer")
    bill = next(l["transaction_id"] for l in out["reserve"]["links"] if l["role"] == "charge")
    it = _reimb_items(client)[bill]
    assert it["reimb_payer"] == "Test Employer" and it["reimb_state"] == "open" and it["reimb_outstanding"] == 250
    assert stats(client)["month_exp"] == s0["month_exp"]  # an open reimbursable expense doesn't count

    # deposit: the kept part and the extra charge are reimbursable, the refund stays a transfer
    d = res(client, method="deposit", account_id=a["chk"], amount=200, title="Lodge", start_date=TD)
    x = settle(client, d["id"], forfeit={"amount": 50}, charge={"amount": 30, "account_id": a["card"]},
               refund={"account_id": a["chk"]}, reimbursable=True)["reserve"]
    items = _reimb_items(client)
    by_role = {l["role"]: l["transaction_id"] for l in x["links"]}
    assert by_role["forfeit"] in items and by_role["charge"] in items and by_role["refund"] not in items

    # Expense (the API default): a normal expense, as before
    e = res(client, method="hold", account_id=a["card"], amount=100, merchant="Test Inn", start_date=TD)
    out = settle(client, e["id"], final={"amount": 90})
    bill = next(l["transaction_id"] for l in out["reserve"]["links"] if l["role"] == "charge")
    assert bill not in _reimb_items(client)

    # linking an already posted charge flags it too
    p = tx(client, kind="expense", account_id=a["card"], amount=60, merchant="Test Motel")
    m = res(client, method="hold", account_id=a["card"], amount=80, merchant="Test Motel", start_date=TD)
    settle(client, m["id"], final={"transaction_id": p["id"]}, reimbursable=True, reimb_payer="Test Client")
    assert _reimb_items(client)[p["id"]]["reimb_payer"] == "Test Client"


def test_mark_reimbursement_complete(client):
    chk = mk(client, name="Test Checking", type="checking", currency="USD", opening_balance=0)
    e1 = tx(client, kind="expense", account_id=chk, amount=40, reimbursable=True)
    e2 = tx(client, kind="expense", account_id=chk, amount=25, reimbursable=True)
    r = tx(client, kind="reimbursement", account_id=chk, amount=50)
    assert client.post("/api/reimbursements/links", json={"reimbursement_id": r["id"], "expense_id": e1["id"]}).status_code == 200
    rc = client.get(f"/api/reimbursements/receipts/{r['id']}").json()
    assert rc["reimb_unallocated"] == 10 and rc["reimb_done"] is False
    assert r["id"] in [x["id"] for x in client.get(f"/api/reimbursements/suggest?expense_id={e2['id']}").json()["items"]]

    done = client.post(f"/api/reimbursements/receipts/{r['id']}/complete").json()
    assert done["reimb_done"] is True and done["reimb_unallocated"] == 0 and done["reimb_leftover"] == 10
    assert r["id"] not in [x["id"] for x in client.get(f"/api/reimbursements/suggest?expense_id={e2['id']}").json()["items"]]
    assert r["id"] not in [x["id"] for x in client.get("/api/reimbursements/receipts?unallocated=1").json()["items"]]
    assert client.get("/api/reimbursements").json()["unallocated"]["count"] == 0
    bad = client.post("/api/reimbursements/links", json={"reimbursement_id": r["id"], "expense_id": e2["id"]})
    assert bad.status_code == 400 and "complete" in bad.text
    assert client.get(f"/api/transactions/{r['id']}").json()["reimb_done"] is True  # shown on the transaction too

    back = client.post(f"/api/reimbursements/receipts/{r['id']}/complete", json={"undo": True}).json()
    assert back["reimb_done"] is False and back["reimb_unallocated"] == 10
    assert client.post(f"/api/reimbursements/receipts/{e1['id']}/complete").status_code == 400  # not a reimbursement
