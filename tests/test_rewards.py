"""Miles & points 里程/积分 + issuer-only credits (no account). FAKE data only."""
import sqlite3
from datetime import date

from app import reminders as R
from app.db import Database


def rew(client, **kw):
    r = client.post("/api/rewards", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def test_program_balance_history_and_value(client):
    p = rew(client, name="Fake Air Miles", kind="airline", member_no="FA123", balance="45,000", balance_date="2026-10-01",
            expiry_date="2099-06-30", value_cents=1.2)
    assert p["unit"] == "miles" and p["balance"] == 45000 and p["value_per_point"] == 0.012 and p["est_value"] == 540
    p = client.post(f"/api/rewards/{p['id']}/balance", json={"change": 2500, "date": "2026-10-20", "note": "flight"}).json()
    assert p["balance"] == 47500 and p["history"][0]["change"] == 2500 and p["history"][0]["note"] == "flight"
    p = client.post(f"/api/rewards/{p['id']}/balance", json={"balance": 30000, "expiry_date": "2099-12-31"}).json()
    assert p["balance"] == 30000 and p["expiry_date"] == "2099-12-31" and len(p["history"]) == 3
    assert client.post(f"/api/rewards/{p['id']}/balance", json={"change": -40000}).status_code == 400
    assert client.post(f"/api/rewards/{p['id']}/balance", json={}).status_code == 400
    # undo the latest update only
    assert client.delete(f"/api/rewards/{p['id']}/history/{p['history'][1]['id']}").status_code == 400
    p = client.delete(f"/api/rewards/{p['id']}/history/{p['history'][0]['id']}").json()
    assert p["balance"] == 47500 and p["expiry_date"] == "2099-06-30"
    h = rew(client, name="Fake Hotel Points", kind="hotel", balance=80000)
    assert h["unit"] == "points" and h["est_value"] is None
    d = client.get("/api/rewards").json()
    assert {x["name"] for x in d["items"]} == {"Fake Air Miles", "Fake Hotel Points"} and d["est_value"] == [{"currency": "USD", "amount": 570.0}]
    client.post(f"/api/rewards/{h['id']}/close")
    assert [x["name"] for x in client.get("/api/rewards").json()["items"]] == ["Fake Air Miles"]
    assert [x["name"] for x in client.get("/api/rewards", params={"status": "closed"}).json()["items"]] == ["Fake Hotel Points"]
    assert client.post("/api/rewards", json={"name": ""}).status_code == 400
    assert client.post("/api/rewards", json={"name": "X", "kind": "boat"}).status_code == 400
    assert client.get("/api/rewards/9999").status_code == 404


def test_expiry_reminder_follows_the_program(client, db):
    p = rew(client, name="Fake Air Miles", balance=10000, expiry_date="2026-12-31")
    on = date(2026, 10, 25)
    rows = lambda: [r for r in R.listing(db, "all", on)["items"] if r["kind"] == "reward"]
    r = rows()
    assert len(r) == 1 and r[0]["due_date"] == "2026-12-31" and r[0]["reward_balance"] == 10000 and r[0]["reward_unit"] == "miles"
    assert r[0]["state"] in ("upcoming", "due") and r[0]["statement_amount"] is None
    assert client.post(f"/api/reminders/{r[0]['id']}/paid", json={}).status_code == 400
    # Done, then activity extends the expiry: the reminder comes back with the new date
    client.post(f"/api/reminders/{r[0]['id']}/done")
    assert R.get(db, r[0]["id"])["status"] == "done"
    client.post(f"/api/rewards/{p['id']}/balance", json={"change": 500, "expiry_date": "2027-06-30"})
    r = rows()
    assert len(r) == 1 and r[0]["status"] == "open" and r[0]["due_date"] == "2027-06-30"
    # using them all closes it; deleting the program dismisses it
    client.post(f"/api/rewards/{p['id']}/balance", json={"balance": 0})
    assert rows()[0]["status"] == "done"
    client.post(f"/api/rewards/{p['id']}/balance", json={"balance": 100})
    assert rows()[0]["status"] == "open"
    client.delete(f"/api/rewards/{p['id']}")
    assert rows()[0]["status"] == "dismissed"


def test_menu_badge_counts(client):
    rew(client, name="Fake Expired", balance=5000, expiry_date="2020-01-01")
    rew(client, name="Fake Soon", balance=5000, expiry_date=date.today().replace(year=date.today().year + 1, month=1, day=1).isoformat())
    rew(client, name="Fake Far", balance=5000, expiry_date="2099-01-01")
    rew(client, name="Fake Empty", balance=0, expiry_date="2020-01-01")
    m = client.get("/api/meta").json()
    assert m["reward_expired_count"] == 1
    assert m["reward_expiring_count"] in (0, 1)  # depends on today vs the 6-month window


def test_credit_without_account(client):
    r = client.post("/api/credits", json={"source": "cancel", "title": "Fake Air travel credit", "amount": 200,
                                          "expiry_date": "2099-03-01"})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["account_id"] is None and c["currency"] == "USD" and c["label"] == "Fake Air travel credit"
    c2 = client.post("/api/credits", json={"source": "refund", "title": "Fake Store credit", "amount": 50, "currency": "cny",
                                           "expiry_date": "2099-03-01", "account_id": ""}).json()
    assert c2["currency"] == "CNY"
    assert client.post("/api/credits", json={"source": "refund", "amount": 50, "expiry_date": "2099-03-01"}).status_code == 400
    assert client.post(f"/api/credits/{c['id']}/use", json={"amount": 80}).json()["remaining"] == 120
    assert {x["id"] for x in client.get("/api/credits").json()["items"]} >= {c["id"], c2["id"]}


def test_old_credits_table_is_rebuilt_keeping_rows(tmp_path):
    p = tmp_path / "old.db"
    Database(p).init()
    con = sqlite3.connect(p)
    # put back the old schema (account_id NOT NULL) with one row, as an existing install has it
    con.executescript("""DROP TABLE credits; CREATE TABLE credits (
        id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, source TEXT NOT NULL, title TEXT DEFAULT '',
        amount REAL NOT NULL, remaining REAL NOT NULL, currency TEXT NOT NULL, issued_date TEXT NOT NULL,
        expiry_date TEXT, status TEXT NOT NULL DEFAULT 'open', reference TEXT DEFAULT '', note TEXT DEFAULT '',
        closed_at TEXT, is_demo INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT);
        INSERT INTO accounts(name, type, currency) VALUES('Fake Card', 'credit_card', 'USD');
        INSERT INTO credits(id, account_id, source, title, amount, remaining, currency, issued_date, expiry_date)
          VALUES(7, (SELECT MAX(id) FROM accounts), 'refund', 'Fake old credit', 30, 30, 'USD', '2026-01-01', '2099-01-01');""")
    con.commit(); con.close()
    db = Database(p)
    assert "credits.account_optional" in db.pending_migrations()
    db.init()
    assert "credits.account_optional" not in db.pending_migrations()
    con = sqlite3.connect(p)
    assert con.execute("SELECT id, title, amount FROM credits").fetchall() == [(7, "Fake old credit", 30.0)]
    assert not [r for r in con.execute("PRAGMA table_info(credits)") if r[1] == "account_id" and r[3]]
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    assert {"ix_credit_acct", "trg_acct_delete_credits", "rewards", "reward_history"} <= names
    con.execute("INSERT INTO credits(source, amount, remaining, currency, issued_date) VALUES('bonus', 1, 1, 'USD', '2026-01-01')")
    con.close()
