"""Reserves 押金/预授权: card holds (memo only) and refundable deposits (transfer into the system Deposits account).
Covers the 13 test areas of the design + "reserves never change stats / KPIs". FAKE data only."""
import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent
T = date.today()
TD = T.isoformat()
M = T.strftime("%Y-%m")


def mk(c, **kw):
    r = c.post("/api/accounts", json=kw)
    assert r.status_code == 200, r.text
    return r.json()["id"] if "id" in r.json() else r.json()


def tx(c, **kw):
    kw.setdefault("date", TD + " 10:00")
    r = c.post("/api/transactions", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def setup(c):
    return {"chk": mk(c, name="Test Checking", type="checking", currency="USD", opening_balance=2000),
            "card": mk(c, name="Test Visa", type="credit_card", currency="USD", opening_balance=0, credit_limit=5000, last4="4321")}


def accts(c):
    return {a["id"]: a for a in c.get("/api/accounts").json()["accounts"]}


def bal(c, aid):
    return accts(c)[aid]["balance"]


def res(c, **kw):
    r = c.post("/api/reserves", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def settle(c, rid, **kw):
    r = c.post(f"/api/reserves/{rid}/settle", json=kw)
    assert r.status_code == 200, r.text
    return r.json()


def stats(c):
    """Every income / expense number and KPI we show (dashboard, month summary, reports, list header, budgets)."""
    d = c.get(f"/api/dashboard?month={M}").json()
    m = c.get(f"/api/summary/month?month={M}").json()
    rp = c.get(f"/api/reports?start={T.year}-01-01").json()
    s = c.get("/api/transactions?limit=5").json()["summary"]
    return {"dash_totals": d["totals"], "by_cat": [(r.get("name"), r["amount"]) for r in d["by_category"]],
            "trend": d["trend"], "month_inc": m["income"], "month_exp": m["expense"], "rep": rp["totals"],
            "rep_rows": [(r.get("name"), r["amount"]) for r in rp["rows"]], "list": (s["income"], s["expense"]),
            "budgets": [(b["category_id"], b["spent"]) for b in d["budgets"]]}


def networth(c):
    return c.get("/api/networth").json()["current"]


def nw_total(c):
    n = networth(c)
    return n["net_worth"] if isinstance(n, dict) else n


# 1 — holds never change balances / stats; a capture counts once
def test_hold_is_memo_only_and_capture_counts_once(client):
    a = setup(client)
    client.post("/api/budgets", json={"month": M, "amount": 1000, "category_id": None, "ledger_id": 1})
    tx(client, kind="expense", account_id=a["card"], amount=20, note="coffee")
    s0, nw0, b0 = stats(client), nw_total(client), bal(client, a["card"])
    n0 = client.get("/api/transactions", params={"status": "all"}).json()["total"]
    r = res(client, method="hold", account_id=a["card"], amount=300, merchant="Test Hotel", start_date=TD,
            end_date=(T + timedelta(days=2)).isoformat())
    assert r["state"] == "open" and r["on_hold"] == 300 and r["link_count"] == 0
    assert stats(client) == s0 and nw_total(client) == nw0 and bal(client, a["card"]) == b0
    card = accts(client)[a["card"]]
    assert card["on_hold"] == 300 and card["available_credit"] == round(5000 + b0 - 300, 2)
    assert client.get("/api/transactions", params={"status": "all"}).json()["total"] == n0  # no transaction for a hold
    out = settle(client, r["id"], final={"amount": 280})
    assert out["reserve"]["state"] == "settled" and out["reserve"]["captured"] == 280 and out["reserve"]["released"] == 20
    s1 = stats(client)
    assert s1["month_exp"] == s0["month_exp"] + 280 and s1["list"][1] == s0["list"][1] + 280  # counted once
    assert bal(client, a["card"]) == b0 - 280
    card = accts(client)[a["card"]]
    assert card["on_hold"] == 0 and card["available_credit"] == round(5000 + b0 - 280, 2)


# 2 — final bill lower → rest released; higher → over hold
def test_final_bill_lower_and_higher_than_hold(client):
    a = setup(client)
    lo = res(client, method="hold", account_id=a["card"], amount=300, title="Lower")
    r = settle(client, lo["id"], final={"amount": 250})["reserve"]
    assert r["state"] == "settled" and r["released"] == 50 and r["over_hold"] == 0 and r["outstanding"] == 0
    hi = res(client, method="hold", account_id=a["card"], amount=300, title="Higher")
    r = settle(client, hi["id"], final={"amount": 412.5})["reserve"]
    assert r["state"] == "settled" and r["over_hold"] == 112.5 and r["released"] == 0
    # partial capture keeps the rest on hold; release with no charge
    p = res(client, method="hold", account_id=a["card"], amount=200, title="Partial")
    e = tx(client, kind="expense", account_id=a["card"], amount=50, note="minibar")
    r = client.post(f"/api/reserves/{p['id']}/links", json={"transaction_id": e["id"]}).json()
    assert r["state"] == "partial" and r["on_hold"] == 150 and accts(client)[a["card"]]["on_hold"] == 150
    rel = res(client, method="hold", account_id=a["card"], amount=100, title="Released")
    r = settle(client, rel["id"], release=True)["reserve"]
    assert r["state"] == "released" and r["released"] == 100
    assert client.post(f"/api/reserves/{rel['id']}/reopen").json()["state"] == "open"
    assert client.post(f"/api/reserves/{rel['id']}/cancel").json()["state"] == "cancelled"


# 3 — bank deposit: −500 / +500, net worth the same, no expense
def test_deposit_is_a_transfer_and_net_worth_unchanged(client, db):
    a = setup(client)
    s0, nw0 = stats(client), nw_total(client)
    r = res(client, method="deposit", account_id=a["chk"], amount=500, title="Apartment", start_date=TD)
    dep = r["deposit_account_id"]
    A = accts(client)
    assert A[a["chk"]]["balance"] == 1500 and A[dep]["balance"] == 500
    assert A[dep]["type"] == "receivable" and A[dep]["name"] == "Deposits 押金"
    assert db.one("SELECT system_role FROM accounts WHERE id=?", (dep,))["system_role"] == "deposits"
    assert nw_total(client) == nw0 and stats(client) == s0
    assert r["state"] == "open" and r["deposited"] == 500 and r["outstanding"] == 500
    # the same Deposits account is reused (one per currency)
    r2 = res(client, method="deposit", account_id=a["chk"], amount=100, title="Gym")
    assert r2["deposit_account_id"] == dep
    out = settle(client, r["id"], refund={"account_id": a["chk"]})["reserve"]
    assert out["state"] == "settled" and out["refunded"] == 500 and out["outstanding"] == 0
    assert bal(client, dep) == 100 and bal(client, a["chk"]) == 1900 and nw_total(client) == nw0 and stats(client) == s0


# 4 — partial refund + forfeit (and a full refund above)
def test_partial_refund_and_forfeit(client):
    a = setup(client)
    s0, nw0 = stats(client), nw_total(client)
    r = res(client, method="deposit", account_id=a["card"], amount=400, title="Car rental")
    out = settle(client, r["id"], forfeit={"amount": 120, "note": "scratch"}, refund={"account_id": a["card"]})
    x = out["reserve"]
    assert x["state"] == "settled" and x["forfeited"] == 120 and x["refunded"] == 280 and x["outstanding"] == 0
    assert bal(client, x["deposit_account_id"]) == 0 and bal(client, a["card"]) == -120
    s1 = stats(client)
    assert s1["month_exp"] == s0["month_exp"] + 120 and s1["month_inc"] == s0["month_inc"]  # only the kept part is spending
    assert nw_total(client) == nw0 - 120
    # partial refund first, the rest later
    r = res(client, method="deposit", account_id=a["chk"], amount=300, title="Cabin")
    x = settle(client, r["id"], refund={"account_id": a["chk"], "amount": 100})["reserve"]
    assert x["state"] == "partial" and x["outstanding"] == 200
    x = settle(client, r["id"], refund={"account_id": a["chk"]})["reserve"]
    assert x["state"] == "settled" and x["refunded"] == 300


# 5 — deposit applied to the bill + extra charge on the card
def test_apply_deposit_and_extra_charge(client):
    a = setup(client)
    s0 = stats(client)
    r = res(client, method="deposit", account_id=a["chk"], amount=200, title="Lodge", start_date=TD)
    x = settle(client, r["id"], apply={"amount": 200}, charge={"amount": 75, "account_id": a["card"]})["reserve"]
    assert x["state"] == "settled" and x["applied"] == 200 and x["charges"] == 75 and x["outstanding"] == 0
    s1 = stats(client)
    assert s1["month_exp"] == s0["month_exp"] + 275  # the whole bill, once
    assert bal(client, a["chk"]) == 1800 and bal(client, a["card"]) == -75 and bal(client, x["deposit_account_id"]) == 0
    roles = sorted(l["role"] for l in x["links"])
    assert roles == ["apply", "charge", "deposit"]


# 6 — over-allocation rejected; interest paid on a deposit is ordinary income
def test_over_allocation_rejected_and_interest_is_income(client):
    a = setup(client)
    r = res(client, method="deposit", account_id=a["chk"], amount=100, title="Locker")
    bad = client.post(f"/api/reserves/{r['id']}/settle", json={"forfeit": {"amount": 80}, "refund": {"account_id": a["chk"], "amount": 50}})
    assert bad.status_code == 400 and "more than the remaining deposit" in bad.json()["detail"]
    assert client.post(f"/api/reserves/{r['id']}/settle", json={"dry_run": True, "forfeit": {"amount": 101}}).status_code == 400
    dep = r["deposit_account_id"]
    big = tx(client, kind="transfer", account_id=dep, to_account_id=a["chk"], amount=150)
    assert client.post(f"/api/reserves/{r['id']}/links", json={"transaction_id": big["id"]}).status_code == 400
    assert client.get(f"/api/reserves/{r['id']}").json()["outstanding"] == 100  # nothing changed (all or nothing)
    s0 = stats(client)
    tx(client, kind="income", account_id=a["chk"], amount=4.2, note="deposit interest")
    x = settle(client, r["id"], refund={"account_id": a["chk"]})["reserve"]
    assert x["state"] == "settled" and stats(client)["month_inc"] == s0["month_inc"] + 4.2
    # a link to one more than the transaction's unlinked part is refused
    h = res(client, method="hold", account_id=a["card"], amount=50)
    e = tx(client, kind="expense", account_id=a["card"], amount=30)
    assert client.post(f"/api/reserves/{h['id']}/links", json={"transaction_id": e["id"], "amount": 31}).status_code == 400


# 7 — EUR deposit paid in USD, refunded at another rate: FX adjustment, Deposits back to exactly 0
def test_multi_currency_deposit_fx_adjustment(client):
    a = setup(client)
    client.put("/api/fx", json={"currency": "EUR", "rate": 1.10})
    assert any(x["currency"] == "EUR" for x in client.get("/api/meta").json()["currencies"])
    s0 = stats(client)
    r = res(client, method="deposit", account_id=a["card"], amount=200, currency="EUR", title="Rental car Lisbon", paid_amount=220)
    dep = r["deposit_account_id"]
    A = accts(client)
    assert A[dep]["currency"] == "EUR" and A[dep]["name"] == "Deposits 押金 (EUR)" and A[dep]["balance"] == 200
    assert A[a["card"]]["balance"] == -220
    client.put("/api/fx", json={"currency": "EUR", "rate": 1.08})
    x = settle(client, r["id"], forfeit={"amount": 50}, refund={"account_id": a["card"], "received": 160})["reserve"]
    assert x["state"] == "settled" and x["outstanding"] == 0 and x["fx_adjusted"] != 0
    assert bal(client, dep) == 0
    fx = [l for l in x["links"] if l["role"] == "fx"]
    assert len(fx) == 1 and fx[0]["tx_kind"] == "adjustment"
    s1 = stats(client)
    assert s1["month_inc"] == s0["month_inc"]  # the FX adjustment is not income/expense
    assert round(s1["month_exp"] - s0["month_exp"], 2) == 54.0  # €50 forfeit at 1.08
    # a big difference is not swallowed as FX
    r2 = res(client, method="deposit", account_id=a["card"], amount=100, currency="EUR", title="Bike")
    bad = client.post(f"/api/reserves/{r2['id']}/settle", json={"refund": {"account_id": a["card"], "received": 150}})
    assert bad.status_code == 400
    # a hold in EUR on a USD card: on_hold converted for the card
    res(client, method="hold", account_id=a["card"], amount=100, currency="EUR")
    assert accts(client)[a["card"]]["on_hold"] == 108


# 8 — many-to-many: one transaction split over two reserves, two charges on one
def test_many_to_many_links(client):
    a = setup(client)
    h1 = res(client, method="hold", account_id=a["card"], amount=300, title="Room 1")
    h2 = res(client, method="hold", account_id=a["card"], amount=300, title="Room 2")
    bill = tx(client, kind="expense", account_id=a["card"], amount=500, note="group bill")
    r1 = client.post(f"/api/reserves/{h1['id']}/links", json={"transaction_id": bill["id"], "amount": 250, "final": True}).json()
    r2 = client.post(f"/api/reserves/{h2['id']}/links", json={"transaction_id": bill["id"]}).json()
    assert r1["captured"] == 250 and r1["state"] == "settled"
    assert r2["captured"] == 250 and r2["state"] == "partial"  # default = the unlinked rest
    assert client.post(f"/api/reserves/{h2['id']}/links", json={"transaction_id": tx(client, kind="expense", account_id=a["card"], amount=60)["id"]}).json()["state"] == "settled"
    t = client.get(f"/api/transactions/{bill['id']}").json()
    assert sorted(x["id"] for x in t["reserves"]) == sorted([h1["id"], h2["id"]])
    rows = client.get("/api/transactions?limit=10").json()["items"]
    assert any(r.get("reserves") for r in rows)
    assert client.post(f"/api/reserves/{h1['id']}/links", json={"transaction_id": bill["id"]}).status_code == 400  # already linked
    sug = client.get(f"/api/reserves/suggest?transaction_id={bill['id']}").json()
    assert all(x["id"] not in (h1["id"], h2["id"]) for x in sug["items"])
    lid = r1["links"][0]["id"]
    assert client.delete(f"/api/reserves/links/{lid}").json()["state"] == "open"


# 9 — deleting a linked transaction drops its link; linked transactions keep kind / currency
def test_delete_trigger_and_locked_edits(client, db):
    a = setup(client)
    h = res(client, method="hold", account_id=a["card"], amount=100)
    e = tx(client, kind="expense", account_id=a["card"], amount=100)
    assert client.post(f"/api/reserves/{h['id']}/links", json={"transaction_id": e["id"]}).json()["state"] == "settled"
    assert client.put(f"/api/transactions/{e['id']}", json={"kind": "income"}).status_code == 400
    # a whole-amount link follows an amount edit
    assert client.put(f"/api/transactions/{e['id']}", json={"amount": 80}).status_code == 200
    x = client.get(f"/api/reserves/{h['id']}").json()
    assert x["captured"] == 80 and x["state"] == "partial"
    client.delete(f"/api/transactions/{e['id']}")
    x = client.get(f"/api/reserves/{h['id']}").json()
    assert x["link_count"] == 0 and x["state"] == "open"
    d = res(client, method="deposit", account_id=a["chk"], amount=300)
    dep_tx = d["links"][0]["transaction_id"]
    other = mk(client, name="Other", type="checking", currency="USD", opening_balance=0)
    assert client.put(f"/api/transactions/{dep_tx}", json={"to_account_id": other}).status_code == 400
    assert client.put(f"/api/reserves/{d['id']}", json={"amount": 999}).status_code == 400  # comes from the transfer
    assert client.put(f"/api/reserves/{d['id']}", json={"title": "Renamed", "end_date": TD}).json()["title"] == "Renamed"
    # hold currency can't change once charges are linked
    h2 = res(client, method="hold", account_id=a["card"], amount=50)
    client.post(f"/api/reserves/{h2['id']}/links", json={"transaction_id": tx(client, kind="expense", account_id=a["card"], amount=10)["id"]})
    client.put("/api/fx", json={"currency": "EUR", "rate": 1.1})
    assert client.put(f"/api/reserves/{h2['id']}", json={"currency": "EUR"}).status_code == 400
    # delete: refused while linked unless unlink=1; the transactions stay
    assert client.delete(f"/api/reserves/{d['id']}").status_code == 400
    assert client.delete(f"/api/reserves/{d['id']}?unlink=1").json()["unlinked"] == 1
    assert client.get(f"/api/transactions/{dep_tx}").status_code == 200
    assert client.get(f"/api/reserves/{d['id']}").status_code == 404


# 10 — reminder: check-out + 7 days (check-in + 30 without check-out), email + Telegram digest, auto-complete
def test_reminder_job(client, db):
    from app import reminders
    a = setup(client)
    out_d = T - timedelta(days=9)
    h = res(client, method="hold", account_id=a["card"], amount=250, title="Old Hotel",
            start_date=(T - timedelta(days=12)).isoformat(), end_date=out_d.isoformat())
    assert h["release_due"] == (out_d + timedelta(days=7)).isoformat() and h["overdue_days"] == 2
    n = res(client, method="hold", account_id=a["card"], amount=80, title="No checkout", start_date=TD)
    assert n["release_due"] == (T + timedelta(days=30)).isoformat()
    res(client, method="deposit", account_id=a["chk"], amount=900, title="Quiet", remind=False,
        start_date=(T - timedelta(days=200)).isoformat())
    assert client.post("/api/reminders/run").json()["created"] >= 1
    rows = client.get("/api/reminders?view=open").json()["items"]
    mine = [r for r in rows if r["kind"] == "reserve"]
    assert len(mine) == 1 and mine[0]["reserve_id"] == h["id"] and mine[0]["state"] == "overdue"
    assert mine[0]["remaining"] == 250 and mine[0]["reserve_method"] == "hold"
    assert db.one("SELECT COUNT(*) n FROM reminders WHERE kind='reserve'")["n"] == 2  # idempotent: one per reserve (no-checkout one is 'later')
    reminders.run(db, notify=False)
    assert db.one("SELECT COUNT(*) n FROM reminders WHERE kind='reserve'")["n"] == 2
    d = reminders.digest(db, 7, True, "en")
    assert any("Old Hotel" in l and "not released" in l for l in d["message"].split("\n"))
    dz = reminders.digest(db, 7, True, "zh")
    assert any("预授权" in l and "未释放" in l for l in dz["message"].split("\n"))
    # the email report uses the same line
    from app import email_reports
    em = email_reports.build(db, "reminders")
    assert "Old Hotel" in em["html"] and "not released" in em["text"]
    # Telegram push (setting on, fake sender)
    from app import telegram_bot
    sent = []
    orig = telegram_bot.notifier
    telegram_bot.notifier = lambda: (lambda text: sent.append(text) or True)
    try:
        db.set_setting("reminder_notify", "telegram")
        assert reminders.notify_due(db) >= 1 and any("Old Hotel" in s for s in sent)
    finally:
        telegram_bot.notifier = orig
    assert client.post(f"/api/reminders/{mine[0]['id']}/paid", json={}).status_code == 400  # check out instead
    settle(client, h["id"], final={"amount": 240})
    client.get("/api/reminders?view=open")  # the list (and every digest) syncs reserve reminders
    assert client.get(f"/api/reminders/{mine[0]['id']}").json()["status"] == "done"
    client.post(f"/api/reserves/{h['id']}/reopen")
    client.get("/api/reminders?view=open")
    assert client.get(f"/api/reminders/{mine[0]['id']}").json()["status"] == "open"
    # settings validated 0–365 and move the due date
    assert client.put("/api/settings", json={"reserve_release_days": "400"}).status_code == 400
    assert client.put("/api/settings", json={"reserve_release_days": "3"}).status_code == 200
    assert client.get(f"/api/reserves/{h['id']}").json()["release_due"] == (out_d + timedelta(days=3)).isoformat()


# 11 — email authorization hold → a hold reserve to review (not a transaction); confirm / it's a real charge
def test_email_hold_goes_to_review(client, db):
    a = setup(client)
    s0 = stats(client)
    body = {"from": "alerts@bank.example", "subject": "Authorization hold placed",
            "body": "A pre-authorization of $150.00 was placed at Test Resort on your card ending in 4321.", "message_id": "<hold-1@x>"}
    n0 = client.get("/api/transactions", params={"status": "all"}).json()["total"]
    r = client.post("/api/ingest/email", json=body).json()
    assert r["kind"] == "hold" and r["result"] == "review" and r["reserve"]["needs_review"] == 1
    assert client.get("/api/transactions", params={"status": "all"}).json()["total"] == n0
    rv = client.get("/api/review").json()
    assert rv["total"] == 0 and len(rv["holds"]) == 1 and client.get("/api/meta").json()["reserve_review_count"] == 1
    assert accts(client)[a["card"]]["on_hold"] == 0  # not counted until confirmed
    rid = rv["holds"][0]["id"]
    assert client.post(f"/api/reserves/{rid}/confirm").json()["needs_review"] == 0
    assert accts(client)[a["card"]]["on_hold"] == 150 and stats(client) == s0
    # a rule with kind 'hold'
    rule = {"name": "Test hold rule", "sender_pattern": "hotel\\.example", "amount_regex": r"\$([\d.]+)", "kind": "hold",
            "last4_regex": r"ending (\d{4})", "enabled": 1, "priority": 1}
    assert client.post("/api/email-rules", json=rule).status_code == 200
    assert client.post("/api/email-rules", json={**rule, "name": "bad", "kind": "nope"}).status_code == 400
    r = client.post("/api/ingest/email", json={"from": "desk@hotel.example", "subject": "Your stay", "body": "We charged $75.00 card ending 4321",
                                                "message_id": "<hold-2@x>"}).json()
    assert r["kind"] == "hold"
    # "It's a real charge" → an expense in the review queue; the reserve goes away
    t = client.post(f"/api/reserves/{r['reserve']['id']}/to-transaction").json()
    assert t["kind"] == "expense" and t["status"] == "review" and t["amount"] == 75
    assert client.get(f"/api/reserves/{r['reserve']['id']}").status_code == 404
    # convert a review item into a reserve
    x = client.post(f"/api/review/{t['id']}/to-reserve", json={"method": "hold", "end_date": TD}).json()
    assert x["method"] == "hold" and x["amount"] == 75 and x["account_id"] == a["card"]
    assert client.get(f"/api/transactions/{t['id']}").status_code == 404
    posted = tx(client, kind="expense", account_id=a["card"], amount=5)
    assert client.post(f"/api/review/{posted['id']}/to-reserve", json={}).status_code == 400
    from app.email_rules import detect_kind
    assert detect_kind("您尾号4321的信用卡预授权交易人民币500.00元") == "hold"
    assert detect_kind("You spent $5 at Starbucks") == "expense"


# 12 — migration from an old database: backup first, additive, idempotent, no rows created
def test_migration_from_old_db_with_backup(tmp_path):
    path = tmp_path / "finance.db"
    app = create_app(path, start_background=False)
    db = app.state.db
    c = TestClient(app); c.headers["X-API-Key"] = db.get_setting("api_key")
    a = setup(c)
    tx(c, kind="expense", account_id=a["card"], amount=12)
    n_acc = len(c.get("/api/accounts").json()["accounts"])
    con = sqlite3.connect(path)
    con.executescript("""DROP TRIGGER trg_tx_delete_reserve; DROP TABLE reserve_links; DROP TABLE reserves;
        DROP INDEX ux_rem_reserve; ALTER TABLE reminders DROP COLUMN reserve_id; ALTER TABLE accounts DROP COLUMN system_role;""")
    con.commit(); con.close()
    assert db.pending_migrations() == ["reserves", "accounts.system_role", "reminders.reserve_id"]
    app2 = create_app(path, start_background=False)
    db2 = app2.state.db
    bk = db2.last_migration_backup
    assert bk and bk.exists() and "before-migrate" in bk.name
    old = sqlite3.connect(bk)
    assert "reserves" not in {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    old.close()
    assert db2.pending_migrations() == []
    assert db2.one("SELECT COUNT(*) n FROM reserves")["n"] == 0 and db2.one("SELECT COUNT(*) n FROM reserve_links")["n"] == 0
    assert db2.one("SELECT COUNT(*) n FROM accounts")["n"] == n_acc  # no Deposits account until first use
    assert db2.one("SELECT 1 x FROM sqlite_master WHERE name='trg_tx_delete_reserve'")
    assert create_app(path, start_background=False).state.db.last_migration_backup is None  # idempotent
    c2 = TestClient(app2); c2.headers["X-API-Key"] = db2.get_setting("api_key")
    assert c2.get("/api/reserves").json()["count"] == 0
    assert res(c2, method="deposit", account_id=a["chk"], amount=10)["state"] == "open"


# 13 — API validation, i18n strings, JS labels
def test_api_validation(client):
    a = setup(client)
    bad = [({"method": "loan", "account_id": a["card"], "amount": 5}, "method"),
           ({"method": "hold", "amount": 5}, "account"),
           ({"method": "hold", "account_id": a["card"]}, "amount"),
           ({"method": "hold", "account_id": a["card"], "amount": -5}, "greater than zero"),
           ({"method": "hold", "account_id": a["card"], "amount": "abc"}, "number"),
           ({"method": "hold", "account_id": a["card"], "amount": 5, "start_date": "2026-13-40"}, "YYYY-MM-DD"),
           ({"method": "hold", "account_id": a["card"], "amount": 5, "start_date": TD, "end_date": "2000-01-01"}, "before"),
           ({"method": "hold", "account_id": a["card"], "amount": 5, "currency": "XYZ"}, "exchange rate")]
    for body, msg in bad:
        r = client.post("/api/reserves", json=body)
        assert r.status_code == 400 and msg.lower() in r.json()["detail"].lower(), (body, r.text)
    d = res(client, method="deposit", account_id=a["chk"], amount=50)
    r = client.post("/api/reserves", json={"method": "hold", "account_id": d["deposit_account_id"], "amount": 5})
    assert r.status_code == 400  # not the Deposits account itself
    assert client.get("/api/reserves/999").status_code == 404
    assert client.post("/api/reserves/999/settle", json={}).status_code == 404
    h = res(client, method="hold", account_id=a["card"], amount=5)
    assert client.post(f"/api/reserves/{h['id']}/settle", json={}).status_code == 400
    assert client.post(f"/api/reserves/{d['id']}/release").status_code == 400  # deposits close via check-out
    assert client.post(f"/api/reserves/{d['id']}/links", json={"transaction_id": 424242}).status_code == 400
    # dry run changes nothing
    before = client.get("/api/transactions", params={"status": "all"}).json()["total"]
    p = client.post(f"/api/reserves/{d['id']}/settle", json={"dry_run": True, "refund": {"account_id": a["chk"]}}).json()
    assert p["dry_run"] and p["result"]["state"] == "settled" and p["plan"][0]["role"] == "refund"
    assert client.get("/api/transactions", params={"status": "all"}).json()["total"] == before
    assert client.get(f"/api/reserves/{d['id']}").json()["state"] == "open"
    # listing, filters, totals, export
    L = client.get("/api/reserves").json()
    assert L["count"] == 2 and L["on_hold"]["total_base"] == 5 and L["deposits"]["total_base"] == 50
    assert client.get("/api/reserves?method=deposit").json()["count"] == 1
    assert client.get("/api/reserves?state=all").json()["state_counts"]["open"] == 2
    csv = client.get("/api/reserves/export.csv").text
    assert csv.startswith("id,method,title") and "deposit" in csv
    sg = client.get(f"/api/reserves/suggest?reserve_id={h['id']}").json()
    assert "items" in sg


def test_i18n_and_js_labels():
    zh = json.loads((ROOT / "locales/zh-CN.json").read_text("utf-8"))
    js = (ROOT / "static/js/13_reserves.js").read_text("utf-8")
    keys = set(re.findall(r"\bt\('((?:[^'\\]|\\.)+)'", js))
    assert len(keys) > 40
    missing = sorted(k for k in keys if k.replace("\\'", "'") not in zh)
    assert not missing, missing
    for k in ("Reserves", "Reserve", "On hold", "Card hold", "Deposit", "Check out", "Deposits 押金"):
        assert k in zh, k
    core = (ROOT / "static/js/01_core.js").read_text("utf-8")
    assert "'reserves'" in core
    idx = (ROOT / "static/index.html").read_text("utf-8")
    assert "13_reserves.js" in idx
    tw = json.loads((ROOT / "locales/zh-TW.json").read_text("utf-8"))
    assert "Reserves" in tw


# demo data: labelled, cleared with everything else; stats neutral apart from the bills
def test_demo_reserves_load_and_clear(client, db):
    assert client.post("/api/demo/load").json()["ok"]
    L = client.get("/api/reserves?state=all").json()
    assert L["count"] == 5 and all(x["is_demo"] == 1 for x in L["items"])
    states = {x["title"]: x["state"] for x in L["items"]}
    assert states["[DEMO] Hilton Chicago"] == "settled" and states["[DEMO] 上海租车 car rental"] == "settled"
    car = next(x for x in L["items"] if x["title"].startswith("[DEMO] 上海租车"))
    assert car["fx_adjusted"] != 0 and car["outstanding"] == 0
    assert L["review_count"] == 1
    assert all(a["name"].startswith("[DEMO]") for a in client.get("/api/accounts").json()["accounts"])
    assert client.post("/api/demo/clear").json()["ok"]
    assert client.get("/api/reserves?state=all").json()["count"] == 0
    assert db.one("SELECT COUNT(*) n FROM reserve_links")["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM reminders WHERE kind='reserve'")["n"] == 0
    assert client.get("/api/accounts").json()["accounts"] == []


# posted expense (e.g. auto-posted from an email) → reserve, in place
def test_posted_expense_converts_to_hold(client):
    a = setup(client)
    before = bal(client, a["card"])
    t = tx(client, kind="expense", account_id=a["card"], amount=300, currency="USD", merchant="Test Hotel", note="pre-auth")
    assert bal(client, a["card"]) != before
    r = client.post(f"/api/transactions/{t['id']}/to-reserve", json={"method": "hold", "end_date": TD})
    assert r.status_code == 200, r.text
    x = r.json()
    assert x["method"] == "hold" and x["amount"] == 300 and x["account_id"] == a["card"]
    assert client.get(f"/api/transactions/{t['id']}").status_code == 404
    assert bal(client, a["card"]) == before  # a hold is not spending


def test_posted_expense_converts_to_deposit_in_place(client):
    a = setup(client)
    t = tx(client, kind="expense", account_id=a["chk"], amount=500, currency="USD", merchant="Test Rental")
    after_spend = bal(client, a["chk"])
    r = client.post(f"/api/transactions/{t['id']}/to-reserve", json={"method": "deposit", "amount": 1})
    assert r.status_code == 200, r.text
    x = r.json()
    assert x["method"] == "deposit" and x["outstanding"] == 500  # the transaction's amount wins over the body
    g = client.get(f"/api/transactions/{t['id']}").json()  # same transaction, now the transfer into Deposits
    assert g["kind"] == "transfer" and g["to_account_id"] == x["deposit_account_id"] and not g.get("category_id")
    assert [l["transaction_id"] for l in x["links"]] == [t["id"]] and x["links"][0]["role"] == "deposit"
    assert bal(client, a["chk"]) == after_spend  # money still left checking
    assert bal(client, x["deposit_account_id"]) == 500
    # converting twice / non-expenses are refused
    assert client.post(f"/api/transactions/{t['id']}/to-reserve", json={"method": "deposit"}).status_code == 400
    inc = tx(client, kind="income", account_id=a["chk"], amount=50)
    assert client.post(f"/api/transactions/{inc['id']}/to-reserve", json={"method": "hold"}).status_code == 400
