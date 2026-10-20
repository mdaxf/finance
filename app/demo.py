"""Clearly-labelled demo data ([DEMO] prefix, is_demo=1) and its removal."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from . import ledger
from .db import ACCOUNT_TYPES, Database
from .quotes import set_price

DEMO_ACCOUNTS = [
    # name, type, currency, opening, extra
    ("[DEMO] Chase Checking", "checking", "USD", 4200, {"aliases": "checking, chase checking", "institution": "Chase", "last4": "0101"}),
    ("[DEMO] Ally Savings", "savings", "USD", 15000, {"aliases": "savings", "institution": "Ally"}),
    ("[DEMO] Chase Sapphire", "credit_card", "USD", -350, {"aliases": "chase sapphire, sapphire", "credit_limit": 12000, "last4": "4242", "institution": "Chase"}),
    ("[DEMO] Amex Gold", "credit_card", "USD", 0, {"aliases": "amex, amex gold", "credit_limit": 8000, "last4": "31005", "institution": "American Express"}),
    ("[DEMO] Cash Wallet", "cash", "USD", 120, {"aliases": "cash, 现金"}),
    ("[DEMO] Alipay 支付宝", "ewallet", "CNY", 2600, {"aliases": "支付宝, alipay"}),
    ("[DEMO] WeChat Pay 微信", "ewallet", "CNY", 800, {"aliases": "微信, wechat, 微信支付"}),
    ("[DEMO] Brokerage", "brokerage", "USD", 9000, {"aliases": "brokerage, broker", "institution": "Demo Brokerage"}),
    ("[DEMO] Car Loan", "loan", "USD", -9000, {"aliases": "car loan"}),
    ("[DEMO] Lent to Friend", "receivable", "USD", 500, {}),
]


def _cat(db: Database, kind: str, sub: str) -> int | None:
    r = db.one("SELECT id FROM categories WHERE kind=? AND parent_id IS NOT NULL AND name=?", (kind, sub))
    return r["id"] if r else None


def load_demo(db: Database) -> dict:
    if db.one("SELECT 1 x FROM accounts WHERE is_demo=1"):
        return {"ok": False, "message": "Demo data already loaded"}
    groups = {g["name"]: g["id"] for g in db.all("SELECT * FROM account_groups")}
    ids = {}
    with db.tx() as con:
        for i, (name, t, cur, opening, extra) in enumerate(DEMO_ACCOUNTS):
            ids[name] = con.execute(
                """INSERT INTO accounts(group_id,name,type,currency,opening_balance,credit_limit,institution,last4,aliases,note,sort,is_demo)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
                (groups[ACCOUNT_TYPES[t][1]], name, t, cur, opening, extra.get("credit_limit"), extra.get("institution", ""),
                 extra.get("last4", ""), extra.get("aliases", ""), "Demo account - safe to delete", i)).lastrowid
        partner = con.execute("INSERT OR IGNORE INTO members(name,is_demo) VALUES('[DEMO] Partner',1)").lastrowid
        trip = con.execute("INSERT OR IGNORE INTO projects(name,is_demo) VALUES('[DEMO] Japan trip 日本旅行',1)").lastrowid
        for m in ("[DEMO] Whole Foods", "[DEMO] Starbucks", "[DEMO] Shell", "[DEMO] Amazon", "[DEMO] 星巴克"):
            con.execute("INSERT OR IGNORE INTO merchants(name,is_demo) VALUES(?,1)", (m,))
    A = lambda n: ids["[DEMO] " + n]
    rnd = random.Random(42)
    today = date.today()
    txs = []

    def add(kind, d, amount, acct, cat=None, merchant=None, note="", to=None, **kw):
        ts = f"{d:%Y-%m-%d} {rnd.randint(8, 21):02d}:{rnd.randint(0, 59):02d}"
        ts = min(ts, (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M"))  # never in the future
        txs.append({"kind": kind, "date": ts, "amount": amount,
                    "account_id": acct, "to_account_id": to, "category_id": cat, "merchant": merchant,
                    "note": ("[DEMO] " + note).strip(), "source": "web", "is_demo": 1, "ledger_id": 1, **kw})

    start = (today.replace(day=1) - timedelta(days=170)).replace(day=1)
    d = start
    while d <= today:
        if d.day in (1, 15):
            add("income", d, 3800, A("Chase Checking"), _cat(db, "income", "Salary"), "[DEMO] Acme Corp payroll", "salary")
        if d.day == 2:
            add("expense", d, 2100, A("Chase Checking"), _cat(db, "expense", "Rent"), None, "rent")
            add("expense", d, 95, A("Chase Sapphire"), _cat(db, "expense", "Internet & Phone"), None, "phone + internet")
            add("transfer", d, 500, A("Chase Checking"), to=A("Ally Savings"), note="monthly savings")
            add("transfer", d, 320, A("Chase Checking"), to=A("Car Loan"), note="car loan payment")
        if d.day == 20:
            add("transfer", d, 650, A("Chase Checking"), to=A("Chase Sapphire"), note="credit card payment")
            add("transfer", d, 600, A("Chase Checking"), to=A("Amex Gold"), note="credit card payment")
        if d.day == 8:
            add("transfer", d, 100, A("Chase Checking"), to=A("Cash Wallet"), note="ATM withdrawal 取现")
        if d.weekday() == 5:
            add("expense", d, round(rnd.uniform(60, 160), 2), A("Amex Gold"), _cat(db, "expense", "Groceries"), "[DEMO] Whole Foods", "groceries")
        if rnd.random() < 0.55:
            add("expense", d, round(rnd.uniform(11, 32), 2), A("Chase Sapphire"), _cat(db, "expense", "Meals"), None, "lunch")
        if rnd.random() < 0.35:
            add("expense", d, round(rnd.uniform(4, 7), 2), A("Chase Sapphire"), _cat(db, "expense", "Coffee & Drinks"), "[DEMO] Starbucks", "coffee")
        if rnd.random() < 0.12:
            add("expense", d, round(rnd.uniform(35, 60), 2), A("Chase Sapphire"), _cat(db, "expense", "Fuel"), "[DEMO] Shell", "gas")
        if rnd.random() < 0.10:
            add("expense", d, round(rnd.uniform(15, 120), 2), A("Amex Gold"), _cat(db, "expense", "Online Shopping"), "[DEMO] Amazon", "amazon order")
        if rnd.random() < 0.15:
            add("expense", d, round(rnd.uniform(18, 90), 0), A("Alipay 支付宝"), _cat(db, "expense", "Meals"), None, "午饭")
        if rnd.random() < 0.08:
            add("expense", d, round(rnd.uniform(15, 45), 0), A("WeChat Pay 微信"), _cat(db, "expense", "Coffee & Drinks"), "[DEMO] 星巴克", "咖啡")
        if rnd.random() < 0.05:
            add("expense", d, round(rnd.uniform(20, 60), 2), A("Cash Wallet"), _cat(db, "expense", "Snacks & Fruit"), None, "farmers market")
        d += timedelta(days=1)
    # one refund, a trip, investments
    add("refund", today - timedelta(days=12), 42.99, A("Amex Gold"), _cat(db, "expense", "Online Shopping"), "[DEMO] Amazon", "returned headphones")
    trip_day = today - timedelta(days=60)
    for i, (amt, sub, note) in enumerate([(820, "Flights", "flight to Tokyo"), (640, "Travel & Hotels", "hotel"), (85, "Meals", "ramen"),
                                          (46, "Taxi & Rideshare", "taxi from station"), (128, "Meals", "sushi dinner"),
                                          (64, None, "teamLab tickets"), (72, "Gifts", "souvenirs"), (18, "Snacks & Fruit", "convenience store")]):
        add("expense", trip_day + timedelta(days=i), amt, A("Chase Sapphire"), _cat(db, "expense", sub), None, note, project_id=trip, tags="travel,japan")
    add("expense", today - timedelta(days=3), 68, A("Chase Sapphire"), _cat(db, "expense", "Gifts"), None, "birthday gift", member_id=partner)
    inv0 = start + timedelta(days=5)
    for kind, d_, t, sh, px in [("buy", inv0, "VTI", 20, 232.5), ("buy", inv0 + timedelta(days=30), "AAPL", 10, 191.2),
                                ("buy", inv0 + timedelta(days=60), "BTC-USD", 0.01, 61000)]:
        txs.append({"kind": kind, "date": f"{d_:%Y-%m-%d} 10:00", "account_id": A("Brokerage"), "ticker": t, "shares": sh,
                    "price": px, "fee": 0, "note": "[DEMO] trade", "is_demo": 1, "ledger_id": 1})
    txs.append({"kind": "dividend", "date": f"{today - timedelta(days=40):%Y-%m-%d} 10:00", "account_id": A("Brokerage"),
                "ticker": "VTI", "amount": 18.4, "note": "[DEMO] dividend", "is_demo": 1, "ledger_id": 1,
                "category_id": _cat(db, "income", "Dividends")})
    for t in txs:
        ledger.create_tx(db, t)
    # demo plan lines for the trip (FAKE): category-matched lines, one explicit assignment, a catch-all line
    cat_ids = {c: _cat(db, "expense", c) for c in ("Flights", "Travel & Hotels", "Taxi & Rideshare", "Meals", "Gifts")}
    with db.tx() as con:
        trip_id = con.execute("SELECT id FROM projects WHERE name='[DEMO] Japan trip 日本旅行'").fetchone()[0]
        if not con.execute("SELECT 1 FROM project_plan_lines WHERE project_id=?", (trip_id,)).fetchone():
            for i, (nm, amt, cats, note, catch) in enumerate([
                    ("Flights", 900, ["Flights"], "2 seats, economy", 0), ("Hotel", 600, ["Travel & Hotels"], "4 nights", 0),
                    ("Transportation", 150, ["Taxi & Rideshare"], None, 0), ("Meals", 300, ["Meals"], None, 0),
                    ("Activities / Tickets", 120, [], None, 0), ("Shopping", 200, ["Gifts"], None, 0), ("Other", 50, [], None, 1)]):
                lid = con.execute("INSERT INTO project_plan_lines(project_id,name,amount,note,sort,catch_all) VALUES(?,?,?,?,?,?)",
                                  (trip_id, nm, amt, note, (i + 1) * 10, catch)).lastrowid
                for c in cats:
                    cid = cat_ids.get(c)
                    if cid:
                        con.execute("INSERT OR IGNORE INTO project_plan_line_cats(line_id, category_id) VALUES(?,?)", (lid, cid))
                if nm.startswith("Activities"):
                    con.execute("UPDATE transactions SET plan_line_id=? WHERE project_id=? AND note='[DEMO] teamLab tickets'", (lid, trip_id))
    # review-inbox examples
    for text, src in (("[DEMO] dinner 58 maybe card?", "message"), ("[DEMO] 打车 32", "bot")):
        ledger.create_tx(db, {"kind": "expense", "date": datetime.now().strftime("%Y-%m-%d %H:%M"), "amount": 58 if "dinner" in text else 32,
                              "status": "review", "confidence": 0.45, "note": text, "raw_text": text, "source": src,
                              "is_demo": 1, "category_id": _cat(db, "expense", "Meals" if "dinner" in text else "Taxi & Rideshare")})
    month = today.strftime("%Y-%m")
    with db.tx() as con:
        for sub_parent, amt in ((None, 4000), ("Food & Dining", 800), ("Transportation", 250), ("Entertainment", 300), ("Shopping", 300)):
            cid = None
            if sub_parent:
                cid = con.execute("SELECT id FROM categories WHERE parent_id IS NULL AND kind='expense' AND name=?", (sub_parent,)).fetchone()[0]
            con.execute("INSERT OR REPLACE INTO budgets(ledger_id,category_id,month,amount,is_demo) VALUES(1,?,?,?,1)", (cid, month, amt))
    for t, p in (("VTI", 268.4), ("AAPL", 228.1), ("BTC-USD", 64000)):
        set_price(db, t, p, "USD", "demo")
    demo_reserves(db, A, today)
    return {"ok": True, "transactions": len(txs) + 2, "accounts": len(DEMO_ACCOUNTS)}


def demo_reserves(db: Database, A, today: date) -> None:
    """FAKE Reserves 押金/预授权: card holds (settled over the hold, overdue, from an email to review), a rent deposit
    and a CNY car-rental deposit partly kept, refunded at a different rate (FX adjustment)."""
    from . import reserves
    hotel = _cat(db, "expense", "Travel & Hotels")
    car = _cat(db, "expense", "Car Rental") or hotel
    ds = lambda n: (today - timedelta(days=n)).isoformat()
    h = reserves.create(db, {"method": "hold", "account_id": A("Chase Sapphire"), "amount": 300, "merchant": "[DEMO] Hilton Chicago",
                             "title": "[DEMO] Hilton Chicago", "start_date": ds(20), "end_date": ds(17), "category_id": hotel,
                             "ref": "CONF-DEMO-1001", "is_demo": True})
    reserves.settle(db, h["id"], {"final": {"amount": 412.50, "date": ds(17), "note": "[DEMO] Hilton final bill"}, "date": ds(17)})
    reserves.create(db, {"method": "hold", "account_id": A("Amex Gold"), "amount": 250, "merchant": "[DEMO] Marriott Downtown",
                         "title": "[DEMO] Marriott Downtown", "start_date": ds(14), "end_date": ds(10), "category_id": hotel,
                         "ref": "CONF-DEMO-2002", "is_demo": True})
    reserves.create(db, {"method": "deposit", "account_id": A("Chase Checking"), "amount": 1500, "title": "[DEMO] Apartment security deposit",
                         "merchant": "[DEMO] Lakeside Apartments", "start_date": ds(150), "remind": False,
                         "note": "[DEMO] returned when the lease ends", "is_demo": True})
    c = reserves.create(db, {"method": "deposit", "account_id": A("Chase Sapphire"), "amount": 2000, "currency": "CNY",
                             "title": "[DEMO] 上海租车 car rental", "merchant": "[DEMO] 上海租车", "start_date": ds(30), "end_date": ds(26),
                             "category_id": car, "is_demo": True})
    rates = ledger.fx_rates(db)
    recv = round(ledger.convert(1700, "CNY", "USD", rates) * 1.012, 2)  # the refund arrived at a slightly better rate
    reserves.settle(db, c["id"], {"forfeit": {"amount": 300, "note": "[DEMO] scratch repair"},
                                  "refund": {"account_id": A("Chase Sapphire"), "received": recv, "date": ds(22)}, "date": ds(24)})
    reserves.create(db, {"method": "hold", "account_id": A("Chase Sapphire"), "amount": 200, "merchant": "[DEMO] Hyatt Regency",
                         "title": "[DEMO] Hyatt Regency", "start_date": ds(1), "needs_review": True, "source": "email",
                         "note": "[DEMO] Authorization hold placed on your card", "is_demo": True})


def clear_demo(db: Database) -> dict:
    with db.tx() as con:
        con.execute("DELETE FROM reserve_links WHERE reserve_id IN (SELECT id FROM reserves WHERE is_demo=1)")
        con.execute("DELETE FROM reminders WHERE kind='reserve' AND reserve_id IN (SELECT id FROM reserves WHERE is_demo=1)")
        con.execute("DELETE FROM reserves WHERE is_demo=1")
        demo_accts = [r[0] for r in con.execute("SELECT id FROM accounts WHERE is_demo=1")]
        for tbl in ("loan_links", "loan_events"):
            con.execute(f"DELETE FROM {tbl} WHERE loan_id IN (SELECT id FROM loans WHERE is_demo=1 OR account_id IN (SELECT id FROM accounts WHERE is_demo=1))")
        con.execute("DELETE FROM reminders WHERE kind='loan' AND loan_id IN (SELECT id FROM loans WHERE is_demo=1 OR account_id IN (SELECT id FROM accounts WHERE is_demo=1))")
        con.execute("DELETE FROM schedules WHERE loan_id IN (SELECT id FROM loans WHERE is_demo=1 OR account_id IN (SELECT id FROM accounts WHERE is_demo=1))")
        con.execute("DELETE FROM loans WHERE is_demo=1 OR account_id IN (SELECT id FROM accounts WHERE is_demo=1)")
        q = ",".join("?" * len(demo_accts)) or "NULL"
        n = con.execute(f"DELETE FROM transactions WHERE is_demo=1 OR note LIKE '[DEMO]%' OR account_id IN ({q}) OR to_account_id IN ({q})",
                        demo_accts + demo_accts).rowcount
        con.execute("DELETE FROM budgets WHERE is_demo=1")
        con.execute("DELETE FROM accounts WHERE is_demo=1")
        con.execute("DELETE FROM project_plan_line_cats WHERE line_id IN (SELECT l.id FROM project_plan_lines l "
                    "JOIN projects p ON p.id=l.project_id WHERE p.is_demo=1)")
        con.execute("DELETE FROM project_plan_lines WHERE project_id IN (SELECT id FROM projects WHERE is_demo=1)")
        for tbl in ("members", "merchants", "projects"):
            con.execute(f"DELETE FROM {tbl} WHERE is_demo=1")
        # merchants auto-created from demo-labelled quick-adds/emails, if nothing else uses them
        con.execute("DELETE FROM merchants WHERE name LIKE '%DEMO%' AND id NOT IN "
                    "(SELECT merchant_id FROM transactions WHERE merchant_id IS NOT NULL)")
        con.execute("DELETE FROM prices WHERE source='demo'")
        if con.execute("SELECT value FROM settings WHERE key='default_account_id'").fetchone() and demo_accts:
            con.execute(f"UPDATE settings SET value='' WHERE key='default_account_id' AND value IN ({','.join(repr(str(x)) for x in demo_accts)})")
    return {"ok": True, "deleted_transactions": n, "deleted_accounts": len(demo_accts)}
