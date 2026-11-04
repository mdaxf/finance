"""Receipt photos library: browse saved receipts by year / month / day (transaction date), search, inline preview. FAKE data only."""
import struct
import zlib

from app import receipts as R


def _png():  # a 1x1 white PNG (no Pillow needed)
    chunk = lambda k, d: struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))


def _receipt(client, aid, date, merchant, amount):
    up = client.post("/api/receipts", files={"file": ("r.png", _png(), "image/png")}).json()
    t = client.post("/api/transactions", json={"kind": "expense", "amount": amount, "account_id": aid, "date": date,
                                               "merchant": merchant, "receipt_ids": up["receipt_ids"]}).json()
    return up["receipt_ids"][0], t


def test_library_tree_filters_search_and_inline_preview(client, monkeypatch):
    monkeypatch.setattr(R, "_tesseract_cmd", lambda: None)
    a = client.post("/api/accounts", json={"name": "Test Card", "type": "credit_card", "currency": "USD", "opening_balance": 0}).json()
    r1, _ = _receipt(client, a["id"], "2026-10-03 14:25", "Test Mart", 11.1)
    r2, _ = _receipt(client, a["id"], "2026-10-03 18:00", "Corner Cafe", 4.5)
    r3, t3 = _receipt(client, a["id"], "2026-11-02 09:00", "Test Mart", 20)
    client.post("/api/receipts", files={"file": ("d.png", _png(), "image/png")})  # a draft (never saved): not listed

    d = client.get("/api/receipts/library").json()
    assert d["total"] == 3 and [i["id"] for i in d["items"]] == [r3, r2, r1]
    assert d["tree"] == [{"y": "2026", "n": 3, "months": [{"m": "11", "n": 1, "days": [{"d": "02", "n": 1}]},
                                                          {"m": "10", "n": 2, "days": [{"d": "03", "n": 2}]}]}]
    it = d["items"][0]
    assert it["merchant"] == "Test Mart" and it["amount"] == 20 and it["account_name"] == "Test Card" and it["date"] == "2026-11-02"
    assert it["file_name"].endswith(f"_{r3}.png") and it["transaction_id"] == t3["id"]

    assert [i["id"] for i in client.get("/api/receipts/library?year=2026&month=10").json()["items"]] == [r2, r1]
    assert client.get("/api/receipts/library?year=2026&month=10&day=4").json()["total"] == 0
    s = client.get("/api/receipts/library?q=mart").json()
    assert s["total"] == 2 and s["tree"][0]["n"] == 2  # the search narrows the tree too
    assert [i["id"] for i in client.get("/api/receipts/library?q=4.50").json()["items"]] == [r2]
    assert client.get("/api/receipts/library?year=abc").status_code == 400
    assert client.get("/api/receipts/library?limit=1").json()["truncated"] is True

    # moving the transaction to another day files the receipt there
    client.put(f"/api/transactions/{t3['id']}", json={"date": "2026-09-30 10:00"})
    assert client.get("/api/receipts/library?year=2026&month=09&day=30").json()["items"][0]["id"] == r3

    img = client.get(f"/api/receipts/{r1}/image")
    assert img.status_code == 200 and img.headers["content-disposition"].startswith("inline")  # previewed, not downloaded
