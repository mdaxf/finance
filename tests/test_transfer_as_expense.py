"""A transfer can count as an expense 计入支出 (loan / mortgage principal): balances unchanged, reports / budgets /
month cards count it under its category; the report switch shows the accounting view. FAKE data only."""
from tests.test_loans import bal, chk, mortgage, ok, totals


def test_loan_principal_counts_as_expense_by_default(client):
    bank = chk(client)
    x = mortgage(client, bank)
    r = ok(client.post(f"/api/loans/{x['id']}/payments", json={"date": "2030-02-01"}))
    p = ok(client.get(f"/api/transactions/{r['plan'][0]['transaction_id']}"))
    assert p["kind"] == "transfer" and p["as_expense"] == 1 and p["category_name"] == "Interest Paid"
    assert bal(client, bank) == round(110000 - 599.55, 2) and bal(client, x["account_id"]) == -99900.45  # still a transfer
    assert totals(client, "2030-02") == (0, 599.55)  # principal 99.55 + interest 500
    rep = ok(client.get("/api/reports", params={"start": "2030-02-01", "end": "2030-02-28"}))
    assert rep["totals"]["expense"] == 599.55 and rep["rows"][0]["amount"] == 599.55
    acc = ok(client.get("/api/reports", params={"start": "2030-02-01", "end": "2030-02-28", "transfers": 0}))
    assert acc["totals"]["expense"] == 500.0  # accounting view
    items = ok(client.get("/api/reports/items", params={"group_by": "category", "key": rep["rows"][0]["key"],
                                                         "start": "2030-02-01", "end": "2030-02-28"}))
    assert items["total"] == 599.55 and items["count"] == 2


def test_loan_setting_off_updates_principal_already_paid(client):
    bank = chk(client)
    x = mortgage(client, bank)
    ok(client.post(f"/api/loans/{x['id']}/payments", json={"date": "2030-02-01"}))
    ok(client.put(f"/api/loans/{x['id']}", json={"principal_as_expense": False}))
    assert totals(client, "2030-02") == (0, 500.0)
    cats = ok(client.get("/api/meta"))["categories"]
    housing = next(c for c in cats if c["kind"] == "expense" and c["children"])["children"][0]["id"]
    ok(client.put(f"/api/loans/{x['id']}", json={"principal_as_expense": True, "principal_category_id": housing}))
    r = ok(client.post(f"/api/loans/{x['id']}/payments", json={"date": "2030-03-01"}))
    p = ok(client.get(f"/api/transactions/{r['plan'][0]['transaction_id']}"))
    assert p["as_expense"] == 1 and p["category_id"] == housing
    assert totals(client, "2030-02")[1] == 599.55  # the February principal follows the setting again


def test_any_transfer_can_count_as_expense(client):
    a = chk(client)
    b = ok(client.post("/api/accounts", json={"name": "Fake Payable", "type": "payable", "currency": "USD"}))["id"]
    cats = ok(client.get("/api/meta"))["categories"]
    cid = next(c for c in cats if c["kind"] == "expense")["id"]
    t = ok(client.post("/api/transactions", json={"kind": "transfer", "date": "2031-05-03 10:00", "amount": 120, "account_id": a,
                                                  "to_account_id": b, "as_expense": True, "category_id": cid}))
    assert t["as_expense"] == 1 and t["category_id"] == cid
    assert totals(client, "2031-05") == (0, 120)
    lst = ok(client.get("/api/transactions", params={"start": "2031-05-01", "end": "2031-05-31"}))
    assert lst["summary"]["expense"] == 120
    t = ok(client.put(f"/api/transactions/{t['id']}", json={"as_expense": False}))
    assert t["as_expense"] == 0 and totals(client, "2031-05") == (0, 0)
    # a plain transfer drops its category; kind changed away from transfer clears the flag
    t2 = ok(client.post("/api/transactions", json={"kind": "transfer", "date": "2031-05-04 10:00", "amount": 5, "account_id": a,
                                                   "to_account_id": b, "category_id": cid}))
    assert t2["category_id"] is None and t2["as_expense"] == 0
    bad = client.post("/api/transactions", json={"kind": "transfer", "date": "2031-05-04 10:00", "amount": 5, "account_id": a,
                                                 "to_account_id": b, "as_expense": True,
                                                 "category_id": next(c for c in cats if c["kind"] == "income")["id"]})
    assert bad.status_code == 400
