"""Loan Center 借贷中心: money lent / borrowed, repayments split into principal (transfer) + interest (expense /
income), expected schedule, loan schedules, reminders, and "principal never counts as spending". FAKE data only."""
from datetime import date, timedelta

T = date.today()
TD = T.isoformat()


def mk(c, **kw):
    r = c.post("/api/accounts", json=kw)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def bal(c, aid):
    return {a["id"]: a for a in c.get("/api/accounts").json()["accounts"]}[aid]["balance"]


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def chk(c, amount=10000):
    return mk(c, name="Test Checking", type="checking", currency="USD", opening_balance=amount)


def mortgage(c, bank, **kw):
    body = {"direction": "borrowed", "counterparty": "Test Bank", "kind": "bank", "principal": 100000, "pay_account_id": bank,
            "rate": 6, "freq": "monthly", "first_due": "2030-02-01", "term": 360, "start_date": "2030-01-01", **kw}
    return ok(c.post("/api/loans", json=body))


def totals(c, month):
    s = ok(c.get(f"/api/summary/month?month={month}"))
    return s["income"], s["expense"]


def test_borrowed_loan_disbursement_is_a_transfer_and_payment_splits(client):
    bank = chk(client)
    x = mortgage(client, bank)
    assert x["account_type"] == "loan" and x["currency"] == "USD"
    assert x["payment"] == 599.55  # 100k, 6 %, 30 years
    assert x["outstanding"] == 100000 and bal(client, x["account_id"]) == -100000 and bal(client, bank) == 110000
    assert x["projection"][0] == {"due_date": "2030-02-01", "payment": 599.55, "principal": 99.55, "interest": 500.0,
                                  "escrow": 0, "balance_after": 99900.45}
    assert x["payoff_date"] == "2060-01-01"
    assert totals(client, "2030-01") == (0, 0)  # receiving the loan is not income
    sp = ok(client.get(f"/api/loans/{x['id']}/split?date=2030-02-01"))
    assert (sp["principal"], sp["interest"], sp["due_date"]) == (99.55, 500.0, "2030-02-01")
    dry = ok(client.post(f"/api/loans/{x['id']}/payments", json={"date": "2030-02-01", "dry_run": True}))
    assert dry["dry_run"] and [p["role"] for p in dry["plan"]] == ["principal", "interest"]
    assert ok(client.get(f"/api/loans/{x['id']}"))["events"][-1]["kind"] == "disburse"  # dry run saved nothing
    r = ok(client.post(f"/api/loans/{x['id']}/payments", json={"date": "2030-02-01"}))
    kinds = {p["role"]: p["kind"] for p in r["plan"]}
    assert kinds == {"principal": "transfer", "interest": "expense"}
    y = r["loan"]
    assert y["outstanding"] == 99900.45 and y["next_due"] == "2030-03-01" and y["interest_total"] == 500
    assert bal(client, bank) == round(110000 - 599.55, 2)
    assert totals(client, "2030-02") == (0, 500.0)  # only the interest is spending
    tx = ok(client.get(f"/api/transactions/{r['plan'][1]['transaction_id']}"))
    assert tx["category_name"] == "Interest Paid" and tx["loans"][0]["role"] == "interest"


def test_existing_loan_tracked_from_now_on_changes_no_history(client, db):
    bank = chk(client)
    house = mk(client, name="House Loan Test", type="loan", currency="USD", opening_balance=-222065.97)
    n0 = db.one("SELECT COUNT(*) n FROM transactions")["n"]
    x = ok(client.post("/api/loans", json={"direction": "borrowed", "counterparty": "Servicer", "kind": "bank", "account_id": house,
                                           "pay_account_id": bank, "rate": 2.375, "freq": "monthly", "first_due": "2030-11-01", "payment": 1500}))
    assert db.one("SELECT COUNT(*) n FROM transactions")["n"] == n0
    assert x["outstanding"] == 222065.97 and x["account_id"] == house
    assert x["projection"][0]["interest"] == round(222065.97 * 0.02375 / 12, 2)
    # a second active loan can't use the same account; a non-liability account is refused
    r = client.post("/api/loans", json={"direction": "borrowed", "counterparty": "X", "account_id": house, "pay_account_id": bank})
    assert r.status_code == 400 and "already belongs" in r.text
    r = client.post("/api/loans", json={"direction": "borrowed", "counterparty": "X", "account_id": bank, "pay_account_id": bank})
    assert r.status_code == 400
    # deleting the account is refused while the loan exists
    assert client.delete(f"/api/accounts/{house}?force=true").status_code == 400


def test_new_account_with_balance_owed_today(client, db):
    bank = chk(client)
    x = ok(client.post("/api/loans", json={"direction": "borrowed", "counterparty": "Aunt Test", "kind": "person", "disburse": False,
                                           "balance": 7000, "currency": "CNY", "pay_account_id": bank}))
    assert x["account_type"] == "payable" and x["currency"] == "CNY" and x["outstanding"] == 7000
    rows = db.all("SELECT kind, source FROM transactions WHERE account_id=?", (x["account_id"],))
    assert rows == [{"kind": "adjustment", "source": "opening_balance"}]  # like a new account's opening balance


def test_lent_money_repayment_with_interest_is_income(client):
    bank = chk(client, 5000)
    x = ok(client.post("/api/loans", json={"direction": "lent", "counterparty": "Cousin Test", "principal": 3000, "pay_account_id": bank,
                                           "start_date": "2030-03-01", "maturity_date": "2030-12-31"}))
    assert x["account_type"] == "receivable" and bal(client, bank) == 2000 and x["outstanding"] == 3000
    assert totals(client, "2030-03") == (0, 0)  # lending is not spending
    r = ok(client.post(f"/api/loans/{x['id']}/payments", json={"date": "2030-06-01", "amount": 1100, "principal": 1000, "interest": 100}))
    assert {p["role"]: p["kind"] for p in r["plan"]} == {"principal": "transfer", "interest": "income"}
    assert r["loan"]["outstanding"] == 2000 and bal(client, bank) == 3100
    assert totals(client, "2030-06") == (100.0, 0)
    bad = client.post(f"/api/loans/{x['id']}/payments", json={"amount": 2500, "principal": 2500})
    assert bad.status_code == 400 and "more than what is still owed" in bad.text
    r = ok(client.post(f"/api/loans/{x['id']}/payments", json={"date": "2030-07-01"}))  # default: the rest, no interest (0 %)
    assert r["loan"]["outstanding"] == 0 and r["loan"]["state"] == "paid_off"
    assert ok(client.get("/api/loans"))["owed_to_me"]["by_currency"] == []


def test_simple_interest_accrues_daily_for_repay_any_time(client):
    bank = chk(client)
    x = ok(client.post("/api/loans", json={"direction": "lent", "counterparty": "Friend Test", "principal": 3650, "pay_account_id": bank,
                                           "start_date": "2030-01-01", "rate": 10}))
    sp = ok(client.get(f"/api/loans/{x['id']}/split?date=2030-01-11&amount=500"))
    assert sp["interest"] == 10.0 and sp["principal"] == 490.0  # 3650 × 10 % × 10 / 365


def test_convert_existing_expense_in_place(client, db):
    """An email-posted mortgage payment (one expense for the whole amount) becomes principal transfer + interest."""
    bank = chk(client)
    x = mortgage(client, bank)
    t = ok(client.post("/api/transactions", json={"kind": "expense", "amount": 599.55, "account_id": bank, "date": "2030-02-01 08:00",
                                                  "merchant": "Test Bank", "note": "mortgage autopay"}))
    sg = ok(client.get(f"/api/loans/suggest?transaction_id={t['id']}"))["items"]
    assert sg and sg[0]["id"] == x["id"] and sg[0]["convert"] and "amount" in sg[0]["match"]
    sg2 = ok(client.get(f"/api/loans/suggest?loan_id={x['id']}"))["items"]
    assert t["id"] in [s["id"] for s in sg2]
    r = ok(client.post(f"/api/transactions/{t['id']}/to-loan-payment", json={"loan_id": x["id"]}))
    assert r["plan"][0] == {**r["plan"][0], "role": "principal", "transaction_id": t["id"], "in_place": True}
    same = ok(client.get(f"/api/transactions/{t['id']}"))
    assert same["kind"] == "transfer" and same["to_account_id"] == x["account_id"] and same["amount"] == 99.55
    assert same["merchant_name"] == "Test Bank" and "mortgage autopay" in same["note"]
    assert totals(client, "2030-02") == (0, 500.0)
    assert r["loan"]["outstanding"] == 99900.45
    # already linked: can't convert twice
    assert client.post(f"/api/transactions/{t['id']}/to-loan-payment", json={"loan_id": x["id"]}).status_code == 400
    # the kind of a linked transaction is locked
    assert client.put(f"/api/transactions/{t['id']}", json={"kind": "expense"}).status_code == 400


def test_link_existing_transactions_unlink_and_undo(client, db):
    bank = chk(client)
    x = mortgage(client, bank)
    p = ok(client.post("/api/transactions", json={"kind": "transfer", "amount": 99.55, "account_id": bank, "to_account_id": x["account_id"], "date": "2030-02-01 09:00"}))
    i = ok(client.post("/api/transactions", json={"kind": "expense", "amount": 500, "account_id": bank, "date": "2030-02-01 09:00"}))
    y = ok(client.post(f"/api/loans/{x['id']}/links", json={"items": [{"transaction_id": p["id"]}, {"transaction_id": i["id"]}]}))
    ev = y["events"][-1]
    assert ev["kind"] == "payment" and ev["principal"] == 99.55 and ev["interest"] == 500 and ev["due_date"] == "2030-02-01"
    assert y["interest_total"] == 500
    bad = client.post(f"/api/loans/{x['id']}/links", json={"items": [{"transaction_id": i["id"], "role": "principal"}]})
    assert bad.status_code == 400
    lk = next(k for k in ev["links"] if k["role"] == "interest")
    y = ok(client.delete(f"/api/loans/links/{lk['id']}"))
    assert y["interest_total"] == 0 and ok(client.get(f"/api/transactions/{i['id']}"))["kind"] == "expense"
    y = ok(client.delete(f"/api/loans/events/{y['events'][-1]['id']}?delete_transactions=true"))
    assert y["payment_count"] == 0 and client.get(f"/api/transactions/{p['id']}").status_code == 404
    # deleting a linked transaction drops its link (and the empty event)
    r = ok(client.post(f"/api/loans/{x['id']}/payments", json={"date": "2030-02-01"}))
    for pl in r["plan"]:
        client.delete(f"/api/transactions/{pl['transaction_id']}")
    assert ok(client.get(f"/api/loans/{x['id']}"))["payment_count"] == 0


def test_loan_schedule_posts_split_payments_idempotently_and_stops_when_paid(client, db):
    bank = chk(client)
    x = ok(client.post("/api/loans", json={"direction": "borrowed", "counterparty": "Car Test", "kind": "bank", "principal": 1200, "pay_account_id": bank,
                                           "rate": 0, "freq": "monthly", "first_due": "2030-02-01", "term": 3, "start_date": "2030-01-01"}))
    assert x["payment"] == 400
    y = ok(client.post(f"/api/loans/{x['id']}/schedule", json={"post_mode": "post", "start_date": "2030-02-01"}))
    assert y["schedule"]["post_mode"] == "post"
    s = db.one("SELECT * FROM schedules WHERE id=?", (y["schedule_id"],))
    assert s["loan_id"] == x["id"]
    r = ok(client.post("/api/schedules/run", json={"date": "2030-03-02"}))
    assert r["generated"] == 2 and not r["errors"]
    ok(client.post("/api/schedules/run", json={"date": "2030-03-02"}))  # re-run: nothing new
    y = ok(client.get(f"/api/loans/{x['id']}"))
    assert y["outstanding"] == 400 and y["payment_count"] == 2
    assert all(t["schedule_id"] == s["id"] for t in db.all("SELECT schedule_id FROM transactions WHERE source='scheduled'"))
    ok(client.post("/api/schedules/run", json={"date": "2030-04-02"}))
    y = ok(client.get(f"/api/loans/{x['id']}"))
    assert y["outstanding"] == 0 and y["state"] == "paid_off"
    assert db.one("SELECT enabled FROM schedules WHERE id=?", (s["id"],))["enabled"] == 0


def test_adopt_existing_schedule(client, db):
    bank = chk(client)
    old = ok(client.post("/api/schedules", json={"name": "Pay mortgage", "template": {"kind": "expense", "amount": 599.55, "account_id": bank},
                                                 "freq": "monthly", "month_day": 1, "start_date": "2030-01-01", "time_of_day": "08:00"}))
    x = mortgage(client, bank)
    y = ok(client.post(f"/api/loans/{x['id']}/schedule", json={"post_mode": "post", "adopt_schedule_id": old["id"]}))
    assert db.one("SELECT enabled FROM schedules WHERE id=?", (old["id"],))["enabled"] == 0
    s = db.one("SELECT * FROM schedules WHERE id=?", (y["schedule_id"],))
    assert s["time_of_day"] == "08:00" and s["enabled"] == 1 and s["loan_id"] == x["id"]


def test_rest_of_month_counts_only_interest_of_loan_schedule(client):
    bank = chk(client)
    x = mortgage(client, bank)
    ok(client.post(f"/api/loans/{x['id']}/schedule", json={"post_mode": "post", "start_date": "2030-02-01"}))
    s = ok(client.get("/api/summary/month?month=2030-02"))["scheduled"]
    assert s["expense"] == 500.0 and s["items"][0]["loan_id"] == x["id"]


def test_escrow_is_spending_with_its_category(client, db):
    bank = chk(client)
    x = mortgage(client, bank, escrow=300)
    assert x["escrow_category_id"]  # defaults to Property Tax (an expense category)
    r = ok(client.post(f"/api/loans/{x['id']}/payments", json={"date": "2030-02-01"}))
    assert {p["role"]: p["amount"] for p in r["plan"]} == {"principal": 99.55, "interest": 500.0, "escrow": 300.0}
    assert totals(client, "2030-02") == (0, 800.0)
    assert r["loan"]["projection"][0]["payment"] == round(599.55 + 300, 2)


def test_overdue_and_loan_reminder_moves_with_payments(client, db):
    bank = chk(client)
    first = (T - timedelta(days=40)).replace(day=1)
    x = ok(client.post("/api/loans", json={"direction": "borrowed", "counterparty": "Uncle Test", "principal": 1000, "pay_account_id": bank,
                                           "rate": 0, "freq": "monthly", "first_due": first.isoformat(), "payment": 100,
                                           "start_date": (first - timedelta(days=30)).isoformat()}))
    assert x["overdue"] and x["overdue"][0] == first.isoformat() and x["overdue_days"] > 0
    assert ok(client.get("/api/loans"))["overdue_count"] == 1
    rem = [r for r in ok(client.get("/api/reminders?view=all"))["items"] if r["kind"] == "loan"]
    assert len(rem) == 1 and rem[0]["due_date"] == first.isoformat() and rem[0]["state"] == "overdue"
    ok(client.post(f"/api/loans/{x['id']}/payments", json={"amount": 100, "date": TD}))
    rem2 = [r for r in ok(client.get("/api/reminders?view=all"))["items"] if r["kind"] == "loan"]
    assert len(rem2) == 1 and rem2[0]["due_date"] > first.isoformat()
    # paying through the reminder records a split payment too
    out = ok(client.post(f"/api/reminders/{rem2[0]['id']}/paid", json={"create_payment": True, "amount": 100}))
    assert out["loan_payment"][0]["role"] == "principal"


def test_remind_only_loan_schedule_paid_from_reminder(client, db):
    bank = chk(client)
    x = mortgage(client, bank)
    y = ok(client.post(f"/api/loans/{x['id']}/schedule", json={"post_mode": "remind", "start_date": "2030-02-01"}))
    ok(client.post("/api/schedules/run", json={"date": "2030-01-25"}))
    rem = [r for r in ok(client.get("/api/reminders?view=all"))["items"] if r["kind"] == "schedule"]
    assert rem and rem[0]["loan_id"] == x["id"]
    out = ok(client.post(f"/api/reminders/{rem[0]['id']}/paid", json={"create_payment": True, "date": "2030-02-01"}))
    assert {p["role"] for p in out["loan_payment"]} == {"principal", "interest"}
    assert ok(client.get(f"/api/loans/{x['id']}"))["events"][-1]["due_date"] == "2030-02-01"
    assert y["schedule_id"]


def test_close_with_write_off_and_reopen(client):
    bank = chk(client)
    x = ok(client.post("/api/loans", json={"direction": "lent", "counterparty": "Bad Debt Test", "principal": 500, "pay_account_id": bank,
                                           "start_date": "2030-05-01"}))
    assert client.post(f"/api/loans/{x['id']}/close", json={}).status_code == 400  # still owed
    y = ok(client.post(f"/api/loans/{x['id']}/close", json={"write_off": True, "date": "2030-05-20"}))
    assert y["state"] == "closed" and y["outstanding"] == 0
    assert totals(client, "2030-05") == (0, 500.0)  # the loss is spending
    assert client.post(f"/api/loans/{x['id']}/payments", json={"amount": 1}).status_code == 400
    y = ok(client.post(f"/api/loans/{x['id']}/close", json={"action": "reopen"}))
    assert y["state"] in ("active", "paid_off")


def test_delete_loan_keeps_transactions(client, db):
    bank = chk(client)
    x = mortgage(client, bank)
    n = db.one("SELECT COUNT(*) n FROM transactions")["n"]
    assert client.delete(f"/api/loans/{x['id']}").status_code == 400
    ok(client.delete(f"/api/loans/{x['id']}?unlink=1"))
    assert db.one("SELECT COUNT(*) n FROM transactions")["n"] == n and bal(client, x["account_id"]) == -100000
    assert client.get(f"/api/loans/{x['id']}").status_code == 404


def test_validation(client):
    bank = chk(client)
    bad = [{"direction": "x", "counterparty": "a"}, {"direction": "lent"},
           {"direction": "lent", "counterparty": "a", "principal": 5}, {"direction": "lent", "counterparty": "a", "principal": 5, "pay_account_id": bank, "freq": "monthly"},
           {"direction": "lent", "counterparty": "a", "principal": 5, "pay_account_id": bank, "rate": 500}]
    for b in bad:
        assert client.post("/api/loans", json=b).status_code == 400, b
    assert client.get("/api/loans/999").status_code == 404
    assert client.get("/api/loans/export.csv").status_code == 200
