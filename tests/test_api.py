"""API, auth, quick-add, email ingest, CSV import/export, backups (fake data only)."""
from fastapi.testclient import TestClient


def test_auth_flow(app):
    c = TestClient(app)
    assert c.get("/api/accounts").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert c.get("/setup").status_code == 200
    assert c.post("/api/auth/setup", json={"password": "short"}).status_code == 400
    assert c.post("/api/auth/setup", json={"password": "correct horse"}).status_code == 200
    assert c.get("/api/accounts").status_code == 200  # session cookie set by setup
    assert c.post("/api/auth/setup", json={"password": "another one"}).status_code == 409
    c.post("/api/auth/logout")
    assert c.get("/api/accounts").status_code == 401
    assert c.post("/api/auth/login", json={"password": "wrong pass"}).status_code == 401
    assert c.post("/api/auth/login", json={"password": "correct horse"}).status_code == 200
    assert c.get("/api/accounts").status_code == 200


def test_api_key(app):
    c = TestClient(app)
    assert c.get("/api/meta", headers={"X-API-Key": "nope"}).status_code == 401
    assert c.get("/api/meta", headers={"X-API-Key": app.state.db.get_setting("api_key")}).status_code == 200


def test_quick_add_posts_and_reviews(client, accounts):
    r = client.post("/api/quick-add", json={"text": "lunch 25.5 chase sapphire"}).json()
    assert r["status"] == "posted" and r["transaction"]["amount"] == 25.5
    assert r["transaction"]["account_id"] == accounts["sapphire"]["id"] and r["transaction"]["source"] == "api"
    r = client.post("/api/quick-add", json={"text": "午饭 35 支付宝", "source": "message"}).json()
    assert r["status"] == "posted" and r["transaction"]["currency"] == "CNY" and r["transaction"]["source"] == "message"
    r = client.post("/api/quick-add", json={"text": "salary 5000 to checking"}).json()
    assert r["transaction"]["kind"] == "income"
    r = client.post("/api/quick-add", json={"text": "paid 300 from checking to amex"}).json()
    assert r["transaction"]["kind"] == "transfer" and r["transaction"]["to_account_id"] == accounts["amex"]["id"]
    # low confidence -> review inbox
    r = client.post("/api/quick-add", json={"text": "dinner 40"}).json()
    assert r["status"] == "review"
    rev = client.get("/api/review").json()
    assert rev["total"] == 1
    tid = rev["items"][0]["id"]
    # approving without an account fails, with an account works
    assert client.post(f"/api/transactions/{tid}/approve", json={}).status_code == 400
    ok = client.post(f"/api/transactions/{tid}/approve", json={"account_id": accounts["cash"]["id"]}).json()
    assert ok["status"] == "posted"
    assert client.get("/api/review").json()["total"] == 0


def test_quick_add_plain_text_and_dry_run(client, accounts):
    r = client.post("/api/quick-add", content="coffee 4.5 cash".encode(), headers={"Content-Type": "text/plain"}).json()
    assert r["status"] == "posted"
    r = client.post("/api/quick-add", json={"text": "coffee 4.5 cash", "dry_run": True}).json()
    assert "transaction" not in r and r["parsed"]["amount"] == 4.5


def test_transaction_crud_search_export(client, accounts):
    t = client.post("/api/transactions", json={"kind": "expense", "amount": 12.3, "account_id": accounts["cash"]["id"],
                                               "merchant": "Fake Diner", "tags": ["food", "test"], "note": "unique-note-xyz",
                                               "member": "Me 本人", "project": "Test Project"}).json()
    assert t["merchant_name"] == "Fake Diner" and t["tags"] == "food,test" and t["project_name"] == "Test Project"
    assert client.get("/api/transactions", params={"q": "unique-note"}).json()["total"] == 1
    assert client.get("/api/transactions", params={"tag": "food"}).json()["total"] == 1
    t2 = client.put(f"/api/transactions/{t['id']}", json={"amount": 15}).json()
    assert t2["amount"] == 15
    csv = client.get("/api/transactions/export.csv").text
    assert "unique-note-xyz" in csv and csv.splitlines()[0].lstrip("\ufeff").startswith("id,date,kind")
    up = client.post(f"/api/transactions/{t['id']}/attachment", files={"file": ("receipt.txt", b"fake receipt", "text/plain")}).json()
    assert up["attachment"]
    assert client.get(f"/api/transactions/{t['id']}/attachment").content == b"fake receipt"
    assert client.delete(f"/api/transactions/{t['id']}").json()["ok"]
    assert client.get("/api/transactions", params={"q": "unique-note"}).json()["total"] == 0


def test_email_ingest_creates_review_and_dedupes(client, accounts):
    msg = {"from": "Chase <no.reply.alerts@chase.com>", "subject": "Your $18.40 transaction",
           "body": "You made a $18.40 transaction with FAKE COFFEE CO on your Chase Sapphire card ending in 9999.",
           "received_at": "2026-10-10T08:15:00", "message_id": "<fake-1@example.test>"}
    r = client.post("/api/ingest/email", json=msg).json()
    assert r["created"] and r["result"] == "review"
    t = r["transaction"]
    assert t["amount"] == 18.4 and t["account_id"] == accounts["sapphire"]["id"] and t["merchant_name"] == "FAKE COFFEE CO"
    assert t["date"].startswith("2026-10-10") and t["source"] == "email"
    assert client.post("/api/ingest/email", json=msg).json()["result"] == "duplicate"
    # auto_post when confident
    # the same purchase again under another message id: a likely duplicate, skipped
    same = client.post("/api/ingest/email", json=dict(msg, message_id="<fake-1b@example.test>")).json()
    assert same["result"] == "similar" and same["similar_to"] == t["id"]
    msg2 = dict(msg, message_id="<fake-2@example.test>", received_at="2026-10-11T08:15:00", auto_post=True)
    assert client.post("/api/ingest/email", json=msg2).json()["result"] == "posted"
    log = client.get("/api/ingest-log").json()
    assert [l["result"] for l in log][:4] == ["posted", "similar", "duplicate", "review"]
    # non-financial mail is skipped
    r = client.post("/api/ingest/email", json={"from": "x@y.test", "subject": "hello", "body": "no money here"}).json()
    assert r["created"] is False


def test_csv_import_chase_format(client, accounts):
    data = ("Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
            "10/01/2026,10/02/2026,FAKE GROCERY 001,Groceries,Sale,-54.20,\n"
            "10/03/2026,10/04/2026,FAKE STARBUCKS,Food & Drink,Sale,-5.75,\n"
            "10/05/2026,10/05/2026,Payment Thank You-Mobile,,Payment,200.00,\n"
            "10/06/2026,10/07/2026,FAKE SHOP RETURN,Shopping,Return,15.00,\n").encode()
    p = client.post("/api/import/csv/preview", files={"file": ("chase.csv", data, "text/csv")}).json()
    assert p["format"] == "Chase credit card" and p["row_count"] == 4
    body = {"token": p["token"], "account_id": accounts["sapphire"]["id"], "mapping": p["mapping"], "sign": p["sign"]}
    r = client.post("/api/import/csv/commit", json=body).json()
    assert r["imported"] == 4 and r["by_kind"] == {"expense": 2, "transfer": 1, "refund": 1} and r["review"] == 1
    r2 = client.post("/api/import/csv/commit", json=body).json()
    assert r2["imported"] == 0 and r2["duplicates"] == 4
    cc = next(a for a in client.get("/api/accounts").json()["accounts"] if a["id"] == accounts["sapphire"]["id"])
    assert cc["owed"] == round(54.20 + 5.75 - 15, 2)


def test_csv_import_alipay_gbk(client, accounts):
    text = ("支付宝交易记录明细查询\n账号:[fake@example.test]\n" + "-" * 20 + "\n"
            "交易时间,交易分类,交易对方,商品说明,收/支,金额,收/付款方式,交易状态\n"
            "2026-10-01 12:00:00,餐饮美食,测试餐厅,午饭,支出,35.00,余额,交易成功\n"
            "2026-10-02 09:00:00,转账红包,测试朋友,红包,收入,88.00,余额,交易成功\n"
            "2026-10-03 09:00:00,投资理财,余额宝,收益,不计收支,0.12,余额,交易成功\n")
    p = client.post("/api/import/csv/preview", files={"file": ("alipay.csv", text.encode("gb18030"), "text/csv")}).json()
    assert p["sign"] == "direction_column"
    r = client.post("/api/import/csv/commit", json={"token": p["token"], "account_id": accounts["alipay"]["id"],
                                                    "mapping": p["mapping"], "sign": p["sign"]}).json()
    assert r["imported"] == 2 and len(r["skipped"]) == 1


def test_budgets(client, accounts):
    from datetime import date
    month = date.today().strftime("%Y-%m")
    food = next(c for c in client.get("/api/categories").json() if c["name"] == "Food & Dining")
    client.post("/api/budgets", json={"month": month, "category_id": food["id"], "amount": 100})
    client.post("/api/budgets", json={"month": month, "amount": 1000})
    client.post("/api/quick-add", json={"text": "lunch 30 cash"})
    items = client.get("/api/budgets", params={"month": month}).json()["items"]
    fb = next(b for b in items if b["category_id"] == food["id"])
    assert fb["spent"] == 30 and fb["remaining"] == 70 and fb["pct"] == 30
    tot = next(b for b in items if b["category_id"] is None)
    assert tot["spent"] == 30


def test_backup_restore(client, accounts, app):
    r = client.post("/api/backups").json()
    assert r["ok"]
    client.post("/api/transactions", json={"kind": "expense", "amount": 1, "account_id": accounts["cash"]["id"]})
    n_before = client.get("/api/transactions").json()["total"]
    blob = client.get(f"/api/backups/file/{r['name']}").content
    res = client.post("/api/backups/restore", files={"file": ("b.db", blob, "application/octet-stream")}).json()
    assert res["ok"]
    assert client.get("/api/transactions").json()["total"] == n_before - 1
    bad = client.post("/api/backups/restore", files={"file": ("x.db", b"not a db", "application/octet-stream")})
    assert bad.status_code == 400


def test_demo_load_and_clear(client):
    assert client.post("/api/demo/load").json()["ok"]
    accts = client.get("/api/accounts").json()["accounts"]
    assert accts and all(a["name"].startswith("[DEMO]") for a in accts)
    assert client.post("/api/demo/clear").json()["ok"]
    assert client.get("/api/accounts").json()["accounts"] == []
    assert client.get("/api/transactions", params={"status": "all"}).json()["total"] == 0


def test_mailbox_password_encrypted(client, app):
    r = client.post("/api/mailboxes", json={"name": "Fake", "host": "127.0.0.1", "port": 9, "use_ssl": 0, "username": "u@example.test",
                                            "password": "s3cret-app-pw", "sender_allowlist": "chase.com"}).json()
    assert r["password_set"] is True and "password_enc" not in r and "s3cret" not in str(r)
    raw = app.state.db.one("SELECT password_enc FROM mailboxes WHERE id=?", (r["id"],))["password_enc"]
    assert "s3cret" not in raw and app.state.secret.decrypt(raw) == "s3cret-app-pw"
    assert "s3cret" not in str(client.get("/api/mailboxes").json())
    t = client.post(f"/api/mailboxes/{r['id']}/test").json()
    assert t["ok"] is False  # nothing listens on port 9
