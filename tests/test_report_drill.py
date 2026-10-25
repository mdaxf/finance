"""Report drill-down: the transactions behind each /api/reports row add up to that row. FAKE data only."""
import pytest


@pytest.fixture()
def data(client, accounts):
    chk, card = accounts["checking"]["id"], accounts["sapphire"]["id"]
    cats = client.get("/api/meta").json()["categories"]
    food = next(c for c in cats if c["children"] and c["kind"] == "expense")
    sub = food["children"][0]["id"]
    tx = lambda **kw: client.post("/api/transactions", json={"date": "2026-03-05 12:00", **kw}).json()
    tx(kind="expense", amount=20, account_id=chk, category_id=sub, merchant="Fake Cafe", tags="trip")
    tx(kind="expense", amount=30, account_id=card, category_id=food["id"], merchant="Fake Cafe", date="2026-04-02 09:00")
    tx(kind="expense", amount=7, account_id=card, merchant="Fake Kiosk")            # uncategorized
    tx(kind="refund", amount=5, account_id=card, category_id=sub, merchant="Fake Cafe")
    tx(kind="income", amount=900, account_id=chk, merchant="Fake Employer")
    tx(kind="expense", amount=99, account_id=chk, date="2025-12-31 10:00")         # outside the period
    return food


@pytest.mark.parametrize("group_by", ["category", "subcategory", "month", "account", "merchant", "tag", "member", "project"])
@pytest.mark.parametrize("kind", ["expense", "income"])
def test_rows_add_up(client, data, group_by, kind):
    q = {"start": "2026-01-01", "end": "2026-12-31", "group_by": group_by, "kind": kind}
    rows = client.get("/api/reports", params=q).json()["rows"]
    assert rows
    for row in rows:
        d = client.get("/api/reports/items", params={**q, "key": "" if row["key"] is None else row["key"]}).json()
        assert d["count"] == row["count"] and abs(d["total"] - row["amount"]) < 0.01, (row, d)
        assert all(t["date"] >= "2026" for t in d["items"])


def test_category_row_includes_subcategories_and_refunds(client, data):
    d = client.get("/api/reports/items", params={"start": "2026-01-01", "end": "2026-12-31", "group_by": "category",
                                                 "kind": "expense", "key": data["id"]}).json()
    assert sorted(t["amount"] for t in d["items"]) == [5, 20, 30] and d["total"] == 45
    assert {t["kind"] for t in d["items"]} == {"expense", "refund"}
    assert client.get("/api/reports/items", params={"group_by": "nope"}).status_code == 400
