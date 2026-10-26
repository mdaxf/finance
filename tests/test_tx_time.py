"""A transaction sent with only a date gets a real time: the `time` field, the receipt's time, or now when it is today.
FAKE data only."""
from datetime import datetime

from app.agent_api import tx_when


def test_tx_when_rules():
    today = datetime.now().strftime("%Y-%m-%d")
    w = []
    assert tx_when("2099-01-02", "1:35 pm") == "2099-01-02 13:35"
    assert tx_when("2099-01-02T08:05:00") == "2099-01-02 08:05"
    assert tx_when("2099-01-02 7:15 PM") == "2099-01-02 19:15"
    assert tx_when("2099-01-02", None, "2099-01-02 18:42") == "2099-01-02 18:42"
    assert tx_when(today)[:10] == today and tx_when(today)[11:] != "12:00" or datetime.now().strftime("%H:%M") == "12:00"
    assert tx_when("2099-01-02", None, None, w) == "2099-01-02 12:00" and w and "12:00" in w[0]


def test_agent_api_uses_time_field_and_today(client):
    a = client.post("/api/accounts", json={"name": "Fake Card", "type": "credit_card", "currency": "USD"}).json()
    r = client.post("/api/agent/transactions", json={"account": a["id"], "amount": 9.5, "date": "2099-02-03", "time": "18:20"})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/transactions/{r.json()['id']}").json()["date"] == "2099-02-03 18:20"
    today = datetime.now().strftime("%Y-%m-%d")
    r = client.post("/api/agent/transactions", json={"account": a["id"], "amount": 4, "date": today}).json()
    d = client.get(f"/api/transactions/{r['id']}").json()["date"]
    assert d[:10] == today and abs(int(d[11:13]) * 60 + int(d[14:16]) - (datetime.now().hour * 60 + datetime.now().minute)) <= 2


def test_plain_api_today_gets_now(client):
    a = client.post("/api/accounts", json={"name": "Fake Cash", "type": "cash", "currency": "USD"}).json()
    today = datetime.now().strftime("%Y-%m-%d")
    t = client.post("/api/transactions", json={"kind": "expense", "amount": 3, "account_id": a["id"], "date": today}).json()
    assert t["date"][:10] == today and len(t["date"]) == 16
    t = client.post("/api/transactions", json={"kind": "expense", "amount": 3, "account_id": a["id"], "date": "2099-02-03"}).json()
    assert t["date"] == "2099-02-03 12:00"


def test_no_date_uses_server_now(client):
    a = client.post("/api/accounts", json={"name": "Fake Wallet", "type": "cash", "currency": "USD"}).json()
    before = datetime.now().strftime("%Y-%m-%d %H:%M")
    r = client.post("/api/agent/transactions", json={"account": a["id"], "amount": 2}).json()
    t = client.post("/api/transactions", json={"kind": "expense", "amount": 3, "account_id": a["id"]}).json()
    after = datetime.now().strftime("%Y-%m-%d %H:%M")
    for d in (client.get(f"/api/transactions/{r['id']}").json()["date"], t["date"]):
        assert before <= d <= after
