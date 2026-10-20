"""Business logic: balances, holdings, transaction create/update, dashboard and reports."""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, date, timedelta
from typing import Any

from .db import ACCOUNT_TYPES, ADJ_REASONS, Database, now_iso

log = logging.getLogger(__name__)
KINDS = ["expense", "income", "transfer", "refund", "adjustment", "buy", "sell", "dividend", "invest_fee", "reimbursement"]
INVEST_KINDS = {"buy", "sell", "dividend", "invest_fee"}

# signed effect on the *source* account
EFFECT_SQL = """CASE kind WHEN 'expense' THEN -amount WHEN 'income' THEN amount WHEN 'refund' THEN amount
  WHEN 'transfer' THEN -amount WHEN 'adjustment' THEN amount WHEN 'buy' THEN -amount WHEN 'sell' THEN amount
  WHEN 'dividend' THEN amount WHEN 'invest_fee' THEN -amount WHEN 'reimbursement' THEN amount ELSE 0 END"""
# Reimbursements 报销 — the stats rule (Reports, KPIs, budgets, projects, list headers all use REPORT_AMOUNT_SQL):
# * a reimbursement (kind 'reimbursement') is never income, allocated or not;
# * a reimbursable expense counts 0 while it is OPEN or PARTIAL (the money is expected back);
# * once it is CLOSED it counts amount − reimbursed (never below 0). Closed = reimbursed ≥ expected amount (auto-close)
#   or written off by hand 核销 (manual close). So the reimbursed part never counts; what was not paid back does.
REIMB_SETTLED_SQL = "COALESCE((SELECT SUM(l.amount) FROM reimb_links l WHERE l.expense_id=t.id),0)"
REIMB_EXPECTED_SQL = "COALESCE(t.reimb_expected, t.amount)"
REIMB_CLOSED_SQL = f"(COALESCE(t.reimb_writeoff,0)=1 OR {REIMB_SETTLED_SQL} >= {REIMB_EXPECTED_SQL} - 0.005)"
REPORT_AMOUNT_SQL = f"""CASE WHEN t.kind='expense' AND COALESCE(t.reimbursable,0)=1 THEN
  (CASE WHEN {REIMB_CLOSED_SQL} THEN MAX(t.amount - {REIMB_SETTLED_SQL}, 0) ELSE 0 END) ELSE t.amount END"""


class ValidationError(ValueError):
    pass


# ---------------------------------------------------------------- balance-adjustment reasons (差额原因)
REASON_CODES = [r[0] for r in ADJ_REASONS]
REASON_LABELS = {c: f"{en} {zh}" for c, en, zh in ADJ_REASONS}
INVEST_INCOME_REASONS = {"investment_gain", "interest"}   # investment income (+/-), separate from income/expense
GAIN_REASONS = {"investment_gain", "interest", "fees"}     # part of an investment account's return (fees reduce it)
# SQL: reason of an adjustment row t joined with its category c (unclassified = correction, opening-balance rows = opening)
REASON_SQL = """CASE WHEN c.code IS NOT NULL AND c.code != 'adjustment' THEN c.code
  WHEN t.source = 'opening_balance' THEN 'opening' ELSE 'correction' END"""


def default_reason(acct_type: str | None) -> str:
    """Set balance default: investment gain/loss for investment-type accounts, correction for everything else."""
    return "investment_gain" if is_investment(acct_type or "") else "correction"


def reason_label(code: str, amount: float | None = None) -> str:
    if code == "investment_gain" and amount is not None:
        return "Investment gain 投资收益" if amount >= 0 else "Investment loss 投资亏损"
    return REASON_LABELS.get(code, REASON_LABELS["correction"])


def reason_category_id(con, code: str) -> int:
    code = (code or "").strip().lower()
    if code not in REASON_CODES:
        raise ValidationError(f"reason must be one of {REASON_CODES}")
    r = con.execute("SELECT id FROM categories WHERE code=?", (code,)).fetchone()
    if not r:
        raise ValidationError(f"Reason category '{code}' is missing")
    return r[0]


def is_liability(acct_type: str) -> bool:
    return ACCOUNT_TYPES.get(acct_type, ("", "", False, False))[2]


def is_investment(acct_type: str) -> bool:
    return ACCOUNT_TYPES.get(acct_type, ("", "", False, False))[3]


# ---------------------------------------------------------------- currencies
def fx_rates(db: Database) -> dict[str, float]:
    return {r["currency"]: r["rate"] for r in db.all("SELECT currency, rate FROM fx_rates")}


def convert(amount: float, from_cur: str, to_cur: str, rates: dict[str, float]) -> float:
    if from_cur == to_cur or amount == 0:
        return amount
    fr, tr = rates.get(from_cur), rates.get(to_cur)
    if not fr or not tr:
        return amount
    return amount * fr / tr


# ---------------------------------------------------------------- balances
def latest_checkpoints(db: Database, until: str | None = None, account_id: int | None = None) -> dict[int, dict]:
    """Latest reconciliation checkpoint per account with as_of <= until (all checkpoints when until is None)."""
    cond, params = "1=1", []
    if until:
        cond += " AND as_of <= ?"; params.append(until)
    if account_id:
        cond += " AND account_id = ?"; params.append(account_id)
    out: dict[int, dict] = {}
    for r in db.all(f"SELECT * FROM reconciliations WHERE {cond} ORDER BY as_of, id", params):
        out[r["account_id"]] = r
    return out


def effects_between(db: Database, account_id: int, after: str | None, until: str | None) -> float:
    """Sum of a single account's posted cash effects with after < date <= until (either bound optional)."""
    cond, params = "status='posted'", []
    if after:
        cond += " AND date > ?"; params.append(after)
    if until:
        cond += " AND date <= ?"; params.append(until)
    a = db.one(f"SELECT COALESCE(SUM({EFFECT_SQL}),0) s FROM transactions WHERE {cond} AND account_id=?", params + [account_id])["s"]
    b = db.one(f"SELECT COALESCE(SUM(COALESCE(to_amount, amount)),0) s FROM transactions WHERE {cond} AND kind='transfer' AND to_account_id=?",
               params + [account_id])["s"]
    return (a or 0) + (b or 0)


def cash_balances(db: Database, until: str | None = None, anchored: bool = True) -> dict[int, float]:
    """Cash balance per account (account currency) incl. opening balance, posted tx only.
    Reconciled accounts are anchored: balance(t) = latest checkpoint (as_of <= t) + transactions after it up to t,
    so changes to transactions dated on/before the checkpoint don't move later balances."""
    cond, params = "status='posted'", []
    if until:
        cond += " AND date <= ?"
        params.append(until)
    bal: dict[int, float] = {a["id"]: a["opening_balance"] or 0 for a in db.all("SELECT id, opening_balance FROM accounts")}
    for r in db.all(f"SELECT account_id, SUM({EFFECT_SQL}) s FROM transactions WHERE {cond} AND account_id IS NOT NULL GROUP BY account_id", params):
        bal[r["account_id"]] = bal.get(r["account_id"], 0) + (r["s"] or 0)
    for r in db.all(f"SELECT to_account_id, SUM(COALESCE(to_amount, amount)) s FROM transactions WHERE {cond} AND kind='transfer' AND to_account_id IS NOT NULL GROUP BY to_account_id", params):
        bal[r["to_account_id"]] = bal.get(r["to_account_id"], 0) + (r["s"] or 0)
    if anchored:
        for aid, cp in latest_checkpoints(db, until).items():
            bal[aid] = cp["cash_balance"] + effects_between(db, aid, cp["as_of"], until)
    return {k: round(v, 2) + 0.0 for k, v in bal.items()}


def holdings(db: Database, account_id: int | None = None, until: str | None = None) -> list[dict]:
    cond, params = "status='posted' AND kind IN ('buy','sell','dividend') AND ticker IS NOT NULL", []
    if account_id:
        cond += " AND account_id=?"
        params.append(account_id)
    if until:
        cond += " AND date <= ?"
        params.append(until)
    rows = db.all(f"SELECT * FROM transactions WHERE {cond} ORDER BY date, id", params)
    prices = {p["ticker"]: p for p in db.all("SELECT * FROM prices")}
    pos: dict[tuple, dict] = {}
    for r in rows:
        key = (r["account_id"], r["ticker"].upper())
        h = pos.setdefault(key, {"account_id": r["account_id"], "ticker": key[1], "shares": 0.0, "cost": 0.0,
                                 "dividends": 0.0, "realized": 0.0, "last_trade_price": None})
        if r["kind"] == "buy":
            h["shares"] += r["shares"] or 0
            h["cost"] += r["amount"]
            h["last_trade_price"] = r["price"]
        elif r["kind"] == "sell":
            sh = r["shares"] or 0
            avg = h["cost"] / h["shares"] if h["shares"] else 0
            h["realized"] += r["amount"] - avg * sh
            h["cost"] -= avg * sh
            h["shares"] -= sh
            h["last_trade_price"] = r["price"]
        else:
            h["dividends"] += r["amount"]
    out = []
    for h in pos.values():
        if abs(h["shares"]) < 1e-9 and not h["dividends"] and not h["realized"]:
            continue
        p = prices.get(h["ticker"])
        price = p["price"] if p else h["last_trade_price"]
        h["price"] = price
        h["price_source"] = (p["source"] + " @ " + (p["updated_at"] or "")) if p else "last trade price"
        h["market_value"] = round((price or 0) * h["shares"], 2)
        h["cost"] = round(h["cost"], 2)
        h["avg_cost"] = round(h["cost"] / h["shares"], 4) if h["shares"] else 0
        h["unrealized"] = round(h["market_value"] - h["cost"], 2)
        h["unrealized_pct"] = round(h["unrealized"] / h["cost"] * 100, 2) if h["cost"] else 0
        h["realized"] = round(h["realized"], 2)
        h["dividends"] = round(h["dividends"], 2)
        out.append(h)
    out.sort(key=lambda x: (x["account_id"], x["ticker"]))
    return out


def accounts_with_balances(db: Database, include_archived: bool = True) -> list[dict]:
    base = db.get_setting("base_currency", "USD")
    rates = fx_rates(db)
    cash = cash_balances(db)
    hold = holdings(db)
    hv: dict[int, float] = defaultdict(float)
    hc: dict[int, float] = defaultdict(float)
    for h in hold:
        hv[h["account_id"]] += h["market_value"]
        hc[h["account_id"]] += h["cost"]
    accts = db.all("""SELECT a.*, g.name group_name, g.sort group_sort FROM accounts a
                      LEFT JOIN account_groups g ON g.id=a.group_id ORDER BY g.sort, a.sort, a.name""")
    from .reserves import holds_by_account
    holds = holds_by_account(db)
    recon = {r["account_id"]: r for r in db.all("""SELECT account_id, MAX(as_of) as_of, COUNT(*) n FROM reconciliations
                                                     GROUP BY account_id""")}
    out = []
    for a in accts:
        if a["archived"] and not include_archived:
            continue
        a["reconciled_at"] = (recon.get(a["id"]) or {}).get("as_of")
        a["reconciliation_count"] = (recon.get(a["id"]) or {}).get("n", 0)
        a["is_liability"] = is_liability(a["type"])
        a["is_investment"] = is_investment(a["type"])
        a["type_label"] = ACCOUNT_TYPES.get(a["type"], (a["type"],))[0]
        a["cash_balance"] = cash.get(a["id"], 0)
        a["holdings_value"] = round(hv.get(a["id"], 0), 2)
        a["holdings_cost"] = round(hc.get(a["id"], 0), 2)
        a["balance"] = round(a["cash_balance"] + a["holdings_value"], 2)
        a["balance_base"] = round(convert(a["balance"], a["currency"], base, rates), 2)
        if a["is_liability"]:
            a["owed"] = round(-a["balance"], 2)
        # open card holds 预授权 (Reserves) are memo only: the balance never includes them, available credit does
        a["on_hold"] = holds.get(a["id"], 0.0)
        if a["type"] == "credit_card":
            a["available_credit"] = round((a["credit_limit"] or 0) + a["balance"] - a["on_hold"], 2) if a["credit_limit"] else None
        out.append(a)
    return out


def account_tree(db: Database, accts: list[dict] | None = None, include_empty: bool = False) -> list[dict]:
    """Account groups as tree nodes with totals in the base currency.

    Groups are one level in the data model (no parent_id), so every node has children=[] for now; the shape is
    recursive so nesting can be added later. Totals include archived accounts so the groups add up to net worth.
    For a group made only of liability accounts, `owed_base` (= -total) is what the UI shows, like account rows."""
    accts = accts if accts is not None else accounts_with_balances(db)
    base = db.get_setting("base_currency", "USD")
    rates = fx_rates(db)
    groups = db.all("SELECT * FROM account_groups ORDER BY sort, id")
    known = {g["id"] for g in groups}
    nodes = [dict(id=g["id"], name=g["name"], sort=g["sort"]) for g in groups]
    nodes.append(dict(id=None, name="Ungrouped 未分组", sort=10 ** 6))
    out = []
    for n in nodes:
        members = [a for a in accts if (a.get("group_id") if a.get("group_id") in known else None) == n["id"]]
        if not members and not include_empty:
            continue
        by_cur: dict[str, float] = defaultdict(float)
        assets = liabilities = 0.0
        for a in members:
            by_cur[a["currency"]] += a["balance"]
            if a["is_liability"]:
                liabilities += -a["balance_base"]
            else:
                assets += a["balance_base"]
        total = round(sum(convert(v, c, base, rates) for c, v in by_cur.items()), 2) + 0.0
        active = [a for a in members if not a.get("archived")]
        liability_group = bool(members) and all(a["is_liability"] for a in members)
        n.update(
            account_ids=[a["id"] for a in members], count=len(members), active_count=len(active),
            archived_count=len(members) - len(active), zero_count=sum(1 for a in members if abs(a["balance"]) < 0.005),
            currency=base, total_base=total, assets_base=round(assets, 2) + 0.0, liabilities_base=round(liabilities, 2) + 0.0,
            liability_group=liability_group, owed_base=(round(-total, 2) + 0.0) if liability_group else None,
            by_currency=[{"currency": c, "total": round(v, 2) + 0.0, "total_base": round(convert(v, c, base, rates), 2) + 0.0}
                         for c, v in sorted(by_cur.items(), key=lambda kv: (kv[0] != base, kv[0]))],
            mixed_currency=len(by_cur) > 1, children=[])
        from .principal import group_totals
        inv = group_totals(members, base, rates)
        if inv:
            n["invest"] = inv
        out.append(n)
    return out


def net_worth(db: Database, accts: list[dict] | None = None) -> dict:
    accts = accts if accts is not None else accounts_with_balances(db)
    assets = liabilities = 0.0
    for a in accts:
        if a["is_liability"]:
            liabilities += -a["balance_base"]
        else:
            assets += a["balance_base"]
    return {"assets": round(assets, 2), "liabilities": round(liabilities, 2), "net_worth": round(assets - liabilities, 2),
            "currency": db.get_setting("base_currency", "USD")}


def month_end(y: int, m: int) -> str:
    if m == 12:
        return f"{y + 1}-01-01 00:00"
    return f"{y}-{m + 1:02d}-01 00:00"


def last_n_months(n: int, today: date | None = None) -> list[tuple[int, int]]:
    today = today or date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def networth_history(db: Database, months: int = 12) -> list[dict]:
    base = db.get_setting("base_currency", "USD")
    rates = fx_rates(db)
    accts = {a["id"]: a for a in db.all("SELECT id, type, currency FROM accounts")}
    out = []
    ms = last_n_months(months)
    for i, (y, m) in enumerate(ms):
        cutoff = month_end(y, m)
        if i == len(ms) - 1:  # current month: live values incl. market prices
            nw = net_worth(db)
            out.append({"month": f"{y}-{m:02d}", **nw})
            continue
        cash = cash_balances(db, until=cutoff)
        invest = defaultdict(float)
        for h in holdings(db, until=cutoff):
            invest[h["account_id"]] += h["cost"]  # historical holdings at cost basis
        assets = liabilities = 0.0
        for aid, a in accts.items():
            v = convert(cash.get(aid, 0) + invest.get(aid, 0), a["currency"], base, rates)
            if is_liability(a["type"]):
                liabilities += -v
            else:
                assets += v
        out.append({"month": f"{y}-{m:02d}", "assets": round(assets, 2), "liabilities": round(liabilities, 2),
                    "net_worth": round(assets - liabilities, 2), "currency": base})
    return out


# ---------------------------------------------------------------- lookups
def get_or_create(con, table: str, name: str | None) -> int | None:
    if not name or not str(name).strip():
        return None
    name = str(name).strip()
    r = con.execute(f"SELECT id FROM {table} WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if r:
        return r[0]
    return con.execute(f"INSERT INTO {table}(name) VALUES(?)", (name,)).lastrowid


def category_label(c: dict | None, parent: dict | None = None) -> str:
    if not c:
        return ""
    lab = c["name"] + (f" {c['name_zh']}" if c.get("name_zh") else "")
    if parent:
        plab = parent["name"] + (f" {parent['name_zh']}" if parent.get("name_zh") else "")
        return f"{plab} › {lab}"
    return lab


def categories_tree(db: Database) -> list[dict]:
    cats = db.all("SELECT * FROM categories ORDER BY kind, sort, id")
    by_id = {c["id"]: c for c in cats}
    for c in cats:
        c["label"] = category_label(c, by_id.get(c["parent_id"]))
        c["children"] = []
    roots = []
    for c in cats:
        if c["parent_id"] and c["parent_id"] in by_id:
            by_id[c["parent_id"]]["children"].append(c)
        else:
            roots.append(c)
    return roots


def parse_ctx(db: Database) -> dict:
    return {
        "accounts": db.all("SELECT * FROM accounts WHERE archived=0"),
        "categories": db.all("SELECT * FROM categories"),
        "merchants": db.all("SELECT * FROM merchants"),
        "members": db.all("SELECT * FROM members"),
        "projects": db.all("SELECT * FROM projects"),
        "plan_lines": db.all("SELECT id, project_id, name, sort FROM project_plan_lines ORDER BY sort, id"),
        "default_account_id": db.get_setting("default_account_id") or None,
        "now": datetime.now(),
    }


# ---------------------------------------------------------------- transactions
TX_FIELDS = ["ledger_id", "kind", "date", "amount", "currency", "account_id", "to_account_id", "to_amount",
             "orig_amount", "orig_currency", "category_id", "merchant_id", "member_id", "project_id", "tags", "note",
             "source", "attachment", "status", "confidence", "raw_text", "external_id", "ticker", "shares", "price",
             "fee", "is_demo", "pr_class", "reimbursable", "reimb_payer", "reimb_expected", "reimb_note", "plan_line_id",
             "tax_amount", "tip_amount", "tax_rate"]
TAXTIP_KINDS = ("expense", "income", "refund")


def _norm_date(d: Any) -> str:
    if not d:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
    s = str(d).strip().replace("T", " ")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            dt = datetime.strptime(s[:n], fmt)
            return dt.strftime("%Y-%m-%d %H:%M") if n > 10 else dt.strftime("%Y-%m-%d 12:00")
        except ValueError:
            continue
    raise ValidationError(f"Invalid date: {d}")


def normalize_tx(con, data: dict, rates: dict[str, float], existing: dict | None = None) -> dict:
    existing_cat = (existing or {}).get("category_id")
    tx = dict(existing or {})
    for k in TX_FIELDS:
        if k in data:
            tx[k] = data[k]
    # names -> ids
    if data.get("merchant") is not None and "merchant_id" not in data:
        tx["merchant_id"] = get_or_create(con, "merchants", data.get("merchant"))
    if data.get("member") is not None and "member_id" not in data:
        tx["member_id"] = get_or_create(con, "members", data.get("member"))
    if data.get("project") is not None and "project_id" not in data:
        tx["project_id"] = get_or_create(con, "projects", data.get("project"))
    kind = tx.get("kind") or "expense"
    if kind not in KINDS:
        raise ValidationError(f"Unknown kind {kind}")
    tx["kind"] = kind
    tx["date"] = _norm_date(tx.get("date"))
    tx["status"] = tx.get("status") or "posted"
    tx["source"] = tx.get("source") or "web"
    tags = tx.get("tags")
    if isinstance(tags, (list, tuple)):
        tags = ",".join(str(t).strip() for t in tags if str(t).strip())
    tx["tags"] = ",".join(t.strip().lstrip("#") for t in (tags or "").split(",") if t.strip())
    for k in ("account_id", "to_account_id", "category_id", "merchant_id", "member_id", "project_id", "ledger_id", "plan_line_id"):
        if tx.get(k) in ("", 0, "0"):
            tx[k] = None
        elif tx.get(k) is not None:
            tx[k] = int(tx[k])
    if not tx.get("ledger_id"):
        tx["ledger_id"] = 1
    # plan line 预算明细: must belong to the transaction's project; changing the project drops a stale line
    if tx.get("plan_line_id"):
        row = con.execute("SELECT project_id FROM project_plan_lines WHERE id=?", (tx["plan_line_id"],)).fetchone()
        if not row or row[0] != tx.get("project_id"):
            if "plan_line_id" in data:
                raise ValidationError("That plan line belongs to a different project (or no longer exists)")
            tx["plan_line_id"] = None
    for k in ("amount", "to_amount", "shares", "price", "fee", "orig_amount"):
        if tx.get(k) in ("", None):
            tx[k] = None
        else:
            try:
                tx[k] = float(tx[k])
            except (TypeError, ValueError):
                raise ValidationError(f"{k} must be a number")
    if "pr_class" in tx:  # principal/returns override for investment accounts ('' / 'auto' = automatic)
        pc = str(tx.get("pr_class") or "").strip().lower()
        if pc in ("", "auto", "none", "null"):
            tx["pr_class"] = None
        elif pc in ("principal", "returns", "unexplained", "other"):
            tx["pr_class"] = "unexplained" if pc == "other" else pc
        else:
            raise ValidationError("pr_class must be principal, returns, unexplained or auto")
    if "reimbursable" in tx or kind != "expense":  # 可报销 flag only on expenses
        flag = tx.get("reimbursable")
        flag = flag if isinstance(flag, bool) else str(flag or "").strip().lower() in ("1", "true", "yes", "on")
        tx["reimbursable"] = 1 if (flag and kind == "expense") else 0
        if not tx["reimbursable"]:
            tx["reimb_writeoff"] = 0
    for k in ("reimb_payer", "reimb_note"):
        if k in tx:
            tx[k] = (str(tx[k]).strip() or None) if tx[k] is not None else None
    if tx.get("reimb_expected") in ("", None):
        tx["reimb_expected"] = None
    elif "reimb_expected" in tx:
        try:
            tx["reimb_expected"] = round(abs(float(tx["reimb_expected"])), 2)
        except (TypeError, ValueError):
            raise ValidationError("reimb_expected must be a number")
    # tax / tip 税 / 小费: OPTIONAL breakouts of the charged total (amount). Blank stays NULL (never 0);
    # tax + tip must not exceed the amount; subtotal = amount - tax - tip.
    for k in ("tax_amount", "tip_amount", "tax_rate"):
        v = tx.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            tx[k] = None
            continue
        try:
            v = float(str(v).strip().rstrip("%"))
        except (TypeError, ValueError):
            raise ValidationError(f"{k} must be a number (or blank)")
        if v < 0:
            raise ValidationError(f"{k} cannot be negative")
        if k == "tax_rate" and v > 100:
            raise ValidationError("tax_rate is a percentage between 0 and 100")
        tx[k] = round(v, 4 if k == "tax_rate" else 2)
    if kind not in TAXTIP_KINDS:
        tx["tax_amount"] = tx["tip_amount"] = tx["tax_rate"] = None
    acct = con.execute("SELECT * FROM accounts WHERE id=?", (tx["account_id"],)).fetchone() if tx.get("account_id") else None
    if tx.get("account_id") and not acct:
        raise ValidationError("Account not found")
    if kind in INVEST_KINDS:
        tx["ticker"] = (tx.get("ticker") or "").upper().strip() or None
        if kind in ("buy", "sell"):
            if not tx["ticker"] or not tx.get("shares") or tx["shares"] <= 0 or tx.get("price") is None:
                raise ValidationError("Buy/sell needs ticker, shares > 0 and price")
            fee = tx.get("fee") or 0
            gross = tx["shares"] * tx["price"]
            tx["amount"] = round(gross + fee if kind == "buy" else gross - fee, 2)
    amount = tx.get("amount")
    if amount is None:
        amount = 0.0
    if kind != "adjustment":
        amount = abs(amount)
    tx["amount"] = round(amount, 2)
    parts = (tx.get("tax_amount") or 0) + (tx.get("tip_amount") or 0)
    if parts and tx["amount"] and parts > tx["amount"] + 0.005:
        raise ValidationError("Tax + tip can't be more than the amount (the amount is the total charged, tax and tip included)")
    posted = tx["status"] == "posted"
    if posted:
        if not acct:
            raise ValidationError("An account is required")
        if kind == "adjustment":
            if tx["amount"] == 0:
                raise ValidationError("Adjustment amount cannot be zero")
        elif tx["amount"] <= 0:
            raise ValidationError("Amount must be greater than zero")
        if kind == "transfer":
            if not tx.get("to_account_id") or tx["to_account_id"] == tx["account_id"]:
                raise ValidationError("A transfer needs a different destination account")
    # currency handling: store amounts in the account's currency
    acct_cur = acct["currency"] if acct else None
    cur = (tx.get("currency") or acct_cur or "USD").upper()
    if acct_cur and cur != acct_cur and kind not in INVEST_KINDS:
        tx["orig_amount"], tx["orig_currency"] = tx["amount"], cur
        tx["amount"] = round(convert(tx["amount"], cur, acct_cur, rates), 2)
        for k in ("tax_amount", "tip_amount"):  # entered in the same currency as the amount
            if tx.get(k) is not None:
                tx[k] = round(convert(tx[k], cur, acct_cur, rates), 2)
        cur = acct_cur
    tx["currency"] = cur
    if kind == "transfer" and tx.get("to_account_id"):
        to = con.execute("SELECT * FROM accounts WHERE id=?", (tx["to_account_id"],)).fetchone()
        if not to:
            raise ValidationError("Destination account not found")
        if to["currency"] != cur and not tx.get("to_amount"):
            tx["to_amount"] = round(convert(tx["amount"], cur, to["currency"], rates), 2)
        elif to["currency"] == cur:
            tx["to_amount"] = None
    else:
        tx["to_account_id"] = None if kind != "transfer" else tx.get("to_account_id")
    if kind == "adjustment":
        # the category of an adjustment is its reason (差额原因): a sub-category of 'Balance adjustment'
        reason = data.get("adj_reason") or data.get("reason")
        adj_cat = lambda cid: cid and con.execute("SELECT 1 FROM categories WHERE id=? AND kind='adjustment'", (cid,)).fetchone()
        if reason:
            tx["category_id"] = reason_category_id(con, reason)
        elif adj_cat(tx.get("category_id")):
            pass
        elif adj_cat(existing_cat):
            tx["category_id"] = existing_cat
        else:  # unclassified: the parent category (treated as Correction/other, or Opening balance for opening rows)
            r = con.execute("SELECT id FROM categories WHERE kind='adjustment' AND parent_id IS NULL ORDER BY id LIMIT 1").fetchone()
            tx["category_id"] = r[0] if r else None
    elif kind in ("transfer", "reimbursement") + tuple(INVEST_KINDS):
        tx["category_id"] = tx.get("category_id") if kind == "dividend" else None
    if kind != "adjustment" and tx.get("category_id") and con.execute(
            "SELECT 1 FROM categories WHERE id=? AND kind='adjustment'", (tx["category_id"],)).fetchone():
        tx["category_id"] = None  # a reason category only belongs on an adjustment
    return tx


def create_tx(db: Database, data: dict) -> dict:
    rates = fx_rates(db)
    with db.tx() as con:
        tx = normalize_tx(con, data, rates)
        tx["updated_at"] = tx["created_at"] = now_iso()
        cols = [k for k in TX_FIELDS + ["updated_at", "created_at"] if k in tx]
        cur = con.execute(f"INSERT INTO transactions({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                          [tx[k] for k in cols])
        tid = cur.lastrowid
    return get_tx(db, tid)


def update_tx(db: Database, tid: int, data: dict) -> dict:
    rates = fx_rates(db)
    with db.tx() as con:
        ex = con.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()
        if not ex:
            raise KeyError(tid)
        ex = dict(ex)
        orig = dict(ex)
        if "currency" not in data and ex.get("orig_currency"):
            # editing a converted row: keep the stored (account currency) amount unless a new one is sent
            ex["currency"] = None
        tx = normalize_tx(con, data, rates, existing=ex)
        linked = con.execute("SELECT COUNT(*) FROM reimb_links WHERE expense_id=? OR receipt_id=?", (tid, tid)).fetchone()[0]
        if linked:
            if ex["kind"] == "expense" and (tx["kind"] != "expense" or not tx.get("reimbursable")):
                raise ValidationError("This expense is linked to reimbursements: unlink them (or close it) first")
            if ex["kind"] == "reimbursement" and tx["kind"] != "reimbursement":
                con.execute("DELETE FROM reimb_links WHERE receipt_id=?", (tid,))  # no longer a receipt: unlink
            elif tx["currency"] != ex["currency"] and tx["kind"] == ex["kind"]:
                raise ValidationError("This transaction has reimbursement links in its currency: unlink them before moving it to an account in another currency")
            elif ex["kind"] == "reimbursement":
                alloc = con.execute("SELECT COALESCE(SUM(COALESCE(alloc_amount, amount)),0) FROM reimb_links WHERE receipt_id=?", (tid,)).fetchone()[0]
                if tx["amount"] < alloc - 0.005:
                    raise ValidationError(f"{alloc:.2f} {ex['currency']} of this reimbursement is linked to expenses: "
                                          f"the amount can't be lower (unlink or reduce the links first)")
        tx["updated_at"] = now_iso()
        cols = [k for k in TX_FIELDS + ["updated_at"] if k in tx]
        con.execute(f"UPDATE transactions SET {','.join(c + '=?' for c in cols)} WHERE id=?", [tx[k] for k in cols] + [tid])
        from .reserves import check_tx_update  # reserve 押金 links: kind/currency locked, whole-amount links follow
        check_tx_update(con, orig, tx)
        from .loans import check_tx_update as loan_check  # loan 借贷 links: kind locked, amounts follow
        loan_check(con, orig, tx)
        if not tx.get("reimbursable"):
            con.execute("UPDATE transactions SET reimb_writeoff=0 WHERE id=?", (tid,))
    return get_tx(db, tid)


TX_SELECT = """SELECT t.*, a.name account_name, a.currency account_currency, ta.name to_account_name,
  c.name category_name, c.name_zh category_name_zh, pc.name parent_category_name, pc.name_zh parent_category_name_zh,
  c.code category_code, a.type account_type,
  (SELECT MAX(rc.reconciliation_id) FROM reconciled_tx rc WHERE rc.transaction_id=t.id) reconciled_id,
  c.parent_id parent_category_id, m.name merchant_name, mb.name member_name, p.name project_name, l.name ledger_name, ppl.name plan_line_name
  FROM transactions t LEFT JOIN accounts a ON a.id=t.account_id LEFT JOIN accounts ta ON ta.id=t.to_account_id
  LEFT JOIN categories c ON c.id=t.category_id LEFT JOIN categories pc ON pc.id=c.parent_id
  LEFT JOIN merchants m ON m.id=t.merchant_id LEFT JOIN members mb ON mb.id=t.member_id
  LEFT JOIN projects p ON p.id=t.project_id LEFT JOIN ledgers l ON l.id=t.ledger_id
  LEFT JOIN project_plan_lines ppl ON ppl.id=t.plan_line_id"""


def decorate(t: dict) -> dict:
    if t.get("category_name"):
        lab = t["category_name"] + (f" {t['category_name_zh']}" if t.get("category_name_zh") else "")
        if t.get("parent_category_name"):
            lab = t["parent_category_name"] + (f" {t['parent_category_name_zh']}" if t.get("parent_category_name_zh") else "") + " › " + lab
        t["category_label"] = lab
    else:
        t["category_label"] = ""
    if t.get("kind") == "adjustment":
        code = t.get("category_code")
        t["adj_reason"] = code if code and code != "adjustment" else ("opening" if t.get("source") == "opening_balance" else "correction")
        t["adj_reason_label"] = reason_label(t["adj_reason"], t.get("amount"))
        if code and code != "adjustment":
            t["category_label"] = "Balance adjustment 余额调整 › " + t["adj_reason_label"]
    return t


def get_tx(db: Database, tid: int) -> dict | None:
    r = db.one(TX_SELECT + " WHERE t.id=?", (tid,))
    if not r:
        return None
    from .reimburse import annotate  # local import: reimburse imports this module
    from .reserves import annotate as reserve_badges
    from .loans import annotate as loan_badges
    return loan_badges(db, reserve_badges(db, annotate(db, [decorate(r)], links=True)))[0]


def _tx_where(f: dict) -> tuple[str, list]:
    where, params = ["1=1"], []
    status = f.get("status") or "posted"
    if status != "all":
        where.append("t.status=?"); params.append(status)
    if f.get("ledger_id"):
        where.append("t.ledger_id=?"); params.append(int(f["ledger_id"]))
    if f.get("account_id"):
        where.append("(t.account_id=? OR t.to_account_id=?)"); params += [int(f["account_id"])] * 2
    if f.get("kind"):
        kinds = str(f["kind"]).split(",")
        where.append(f"t.kind IN ({','.join('?' * len(kinds))})"); params += kinds
    if f.get("category_id"):
        cid = int(f["category_id"])
        where.append("(t.category_id=? OR c.parent_id=?)"); params += [cid, cid]
    for k in ("member_id", "merchant_id", "project_id", "schedule_id"):
        if f.get(k):
            where.append(f"t.{k}=?"); params.append(int(f[k]))
    if f.get("source"):
        where.append("t.source=?"); params.append(f["source"])
    for k, col in (("has_tax", "tax_amount"), ("has_tip", "tip_amount")):  # tax/tip recorded (yes) / not recorded (no)
        v = str(f.get(k) or "").lower()
        if v in ("1", "yes", "true"):
            where.append(f"t.{col} IS NOT NULL")
        elif v in ("0", "no", "false"):
            where.append(f"t.{col} IS NULL")
    if f.get("reconciled") in ("1", "yes", "true", 1, True):
        where.append("EXISTS (SELECT 1 FROM reconciled_tx rc WHERE rc.transaction_id=t.id)")
    elif f.get("reconciled") in ("0", "no", "false", 0, False):
        where.append("NOT EXISTS (SELECT 1 FROM reconciled_tx rc WHERE rc.transaction_id=t.id)")
    rb = [x for x in str(f.get("reimb") or "").lower().split(",") if x]  # reimbursement status filter
    if rb:
        conds = []
        if "open" in rb:
            conds.append(f"(NOT {REIMB_CLOSED_SQL} AND {REIMB_SETTLED_SQL} <= 0.005)")
        if "partial" in rb:
            conds.append(f"(NOT {REIMB_CLOSED_SQL} AND {REIMB_SETTLED_SQL} > 0.005)")
        if "closed" in rb:
            conds.append(REIMB_CLOSED_SQL)
        if "any" in rb or "all" in rb:
            conds.append("1=1")
        part = f"(t.kind='expense' AND COALESCE(t.reimbursable,0)=1 AND ({' OR '.join(conds)}))" if conds else None
        alloc = "COALESCE((SELECT SUM(COALESCE(l.alloc_amount, l.amount)) FROM reimb_links l WHERE l.receipt_id=t.id),0)"
        rc = None
        if "unallocated" in rb:
            rc = f"(t.kind='reimbursement' AND t.amount - {alloc} > 0.005)"
        elif "any" in rb or "all" in rb:
            rc = "t.kind='reimbursement'"
        where.append("(" + " OR ".join(x for x in (part, rc) if x) + ")" if (part or rc) else "0=1")
    if f.get("tag"):
        where.append("(',' || t.tags || ',') LIKE ?"); params.append(f"%,{f['tag']},%")
    if f.get("start"):
        where.append("t.date >= ?"); params.append(str(f["start"])[:10])
    if f.get("end"):
        where.append("t.date <= ?"); params.append(str(f["end"])[:10] + " 99")
    if f.get("min_amount"):
        where.append("t.amount >= ?"); params.append(float(f["min_amount"]))
    if f.get("max_amount"):
        where.append("t.amount <= ?"); params.append(float(f["max_amount"]))
    if f.get("q"):
        q = f"%{f['q']}%"
        where.append("(t.note LIKE ? OR m.name LIKE ? OR t.tags LIKE ? OR c.name LIKE ? OR c.name_zh LIKE ? OR a.name LIKE ? OR t.raw_text LIKE ? OR t.ticker LIKE ?)")
        params += [q] * 8
    return " WHERE " + " AND ".join(where), params


def query_tx(db: Database, f: dict) -> tuple[list[dict], int]:
    w, params = _tx_where(f)
    total = db.one(f"SELECT COUNT(*) n FROM ({TX_SELECT}{w})", params)["n"]
    limit = min(int(f.get("limit") or 100), 5000)
    offset = int(f.get("offset") or 0)
    rows = db.all(f"{TX_SELECT}{w} ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?", params + [limit, offset])
    from .reimburse import annotate
    from .reserves import annotate as reserve_badges
    from .loans import annotate as loan_badges
    return loan_badges(db, reserve_badges(db, annotate(db, [decorate(r) for r in rows], links=bool(f.get("with_links"))))), total


def encode_cursor(t: dict) -> str:
    return f"{t['date']}|{t['id']}"


def decode_cursor(c: str) -> tuple[str, int]:
    """Cursor 'YYYY-MM-DD HH:MM[:SS]|id' (newest-first order: the next page starts strictly after this row)."""
    try:
        d, i = str(c).rsplit("|", 1)
        return d, int(i)
    except (ValueError, AttributeError):
        raise ValidationError("bad cursor (expected 'date|id')")


def _summary(db: Database, w: str, params: list) -> dict:
    """Count and income/expense sums (base currency) of everything matching a filter (for list headers)."""
    base = db.get_setting("base_currency", "USD"); rates = fx_rates(db)
    inc = exp = 0.0; n = 0
    for r in db.all(f"SELECT t.kind, t.currency, SUM({REPORT_AMOUNT_SQL}) s, COUNT(*) n FROM ({TX_SELECT}{w}) t GROUP BY t.kind, t.currency", params):
        n += r["n"]
        v = convert(r["s"] or 0, r["currency"], base, rates)
        if r["kind"] == "income":
            inc += v
        elif r["kind"] == "expense":
            exp += v
        elif r["kind"] == "refund":
            exp -= v
    return {"count": n, "income": round(inc, 2), "expense": round(exp, 2), "net": round(inc - exp, 2), "currency": base}


def page_tx(db: Database, f: dict) -> dict:
    """Cursor (keyset) pagination, newest first: ?limit=50&before=<date|id>. The first page (no cursor) also returns
    total + summary of the whole filtered set; later pages skip those counts. offset= still works for old clients."""
    w, params = _tx_where(f)
    limit = max(1, min(int(f.get("limit") or 50), 500))
    cur = f.get("before")
    kw, kp = w, list(params)
    if cur:
        d, i = decode_cursor(cur)
        kw += " AND (t.date < ? OR (t.date = ? AND t.id < ?))"; kp += [d, d, i]
    offset = 0 if cur else int(f.get("offset") or 0)
    rows = [decorate(r) for r in db.all(f"{TX_SELECT}{kw} ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?", kp + [limit + 1, offset])]
    has_more = len(rows) > limit
    rows = rows[:limit]
    from .reimburse import annotate
    annotate(db, rows)  # reimbursement status badges
    from .reserves import annotate as reserve_badges
    reserve_badges(db, rows)  # reserve 押金/预授权 badges
    from .loans import annotate as loan_badges
    loan_badges(db, rows)  # loan 借贷 badges
    out = {"items": rows, "has_more": has_more, "next_cursor": encode_cursor(rows[-1]) if has_more and rows else None}
    if not cur or str(f.get("with_total", "")).lower() in ("1", "true"):
        s = _summary(db, w, params)
        out.update(total=s["count"], summary=s)
    return out


def tx_effect_for(t: dict, aid: int) -> float:
    """Signed cash effect of one transaction on account `aid` (same rules as EFFECT_SQL / transfers in)."""
    k, amt = t["kind"], t["amount"] or 0
    if t.get("account_id") == aid:
        return {"expense": -amt, "income": amt, "refund": amt, "transfer": -amt, "adjustment": amt, "buy": -amt,
                "sell": amt, "dividend": amt, "invest_fee": -amt, "reimbursement": amt}.get(k, 0.0)
    if k == "transfer" and t.get("to_account_id") == aid:
        return t["to_amount"] if t.get("to_amount") is not None else amt
    return 0.0


def running_balances(db: Database, aid: int) -> tuple[dict[int, float], list[tuple[str, int, float]]]:
    """Cash balance of account `aid` right after each posted transaction, oldest→newest by (date, id), following the
    reconciliation rule used everywhere: balance(t) = latest checkpoint (as_of < t) + the account's transactions after it.
    Returns ({tx_id: balance_after}, [(date, id, balance_after), ...] in order)."""
    acct = db.one("SELECT opening_balance FROM accounts WHERE id=?", (aid,)) or {}
    bal = float(acct.get("opening_balance") or 0)
    cps = db.all("SELECT as_of, cash_balance FROM reconciliations WHERE account_id=? ORDER BY as_of, id", (aid,))
    rows = db.all("""SELECT id, date, kind, amount, to_amount, account_id, to_account_id FROM transactions
        WHERE status='posted' AND (account_id=? OR to_account_id=?) ORDER BY date, id""", (aid, aid))
    after, seq, ci = {}, [], 0
    for r in rows:
        while ci < len(cps) and cps[ci]["as_of"] < r["date"]:
            bal = cps[ci]["cash_balance"]; ci += 1
        bal += tx_effect_for(r, aid)
        b = round(bal, 2) + 0.0
        after[r["id"]] = b
        seq.append((r["date"], r["id"], b))
    return after, seq


def balance_before(seq: list[tuple[str, int, float]], cps: list[dict], when: str, opening: float) -> float:
    """Cash balance just before `when` (all rows with date < when), from running_balances' sequence + checkpoints."""
    import bisect
    i = bisect.bisect_left(seq, (when, -1, float("-inf")))
    bal, last_date = (seq[i - 1][2], seq[i - 1][0]) if i else (opening, "")
    for cp in cps:  # a checkpoint after the last row but before `when` re-anchors
        if last_date < cp["as_of"] < when:
            bal = cp["cash_balance"]
    return round(bal, 2) + 0.0


HISTORY_PERIODS = {"1M": 1, "3M": 3, "6M": 6, "1Y": 12, "3Y": 36}


def period_start(period: str, today: date | None = None) -> str | None:
    today = today or date.today()
    p = (period or "1M").upper()
    if p == "ALL":
        return None
    if p == "YTD":
        return f"{today.year}-01-01"
    months = HISTORY_PERIODS.get(p, 1)
    y, m = today.year, today.month - months
    while m < 1:
        m += 12; y -= 1
    from calendar import monthrange
    return f"{y}-{m:02d}-{min(today.day, monthrange(y, m)[1]):02d}"


def account_history(db: Database, aid: int, f: dict, today: date | None = None) -> dict:
    """Account detail page: the account's transactions (newest first, cursor pages) with a running balance column.
    Default period: the last month. Running balance is the account's real balance after each row (filters like
    search/category only hide rows; they don't change the balances)."""
    a = next((x for x in accounts_with_balances(db) if x["id"] == aid), None)
    if not a:
        raise KeyError(aid)
    period = (f.get("period") or ("CUSTOM" if f.get("start") or f.get("end") else "1M")).upper()
    start = (f.get("start") or None) if period == "CUSTOM" else period_start(period, today)
    end = (f.get("end") or None) if period == "CUSTOM" else None
    q = {k: v for k, v in f.items() if k in ("q", "category_id", "kind", "member_id", "merchant_id", "project_id", "tag",
                                               "reconciled", "before", "limit", "with_total")}
    q.update(account_id=aid, start=start, end=end)
    page = page_tx(db, q)
    after, seq = running_balances(db, aid)
    sign = -1 if a["is_liability"] else 1
    for t in page["items"]:
        b = after.get(t["id"])
        t["running_balance"] = b
        t["running_shown"] = None if b is None else round(b * sign, 2) + 0.0
        t["account_effect"] = round(tx_effect_for(t, aid), 2) + 0.0
    out = {"account": a, "period": period, "start": start, "end": end, "cash_only": bool(a.get("is_investment")), **page}
    from .principal import history_extras
    inv = history_extras(db, a, page["items"], start, end, not f.get("before"), today)
    if inv:
        out["invest"] = inv
    if not f.get("before"):
        cps = db.all("SELECT as_of, cash_balance FROM reconciliations WHERE account_id=? ORDER BY as_of, id", (aid,))
        opening = float(db.one("SELECT opening_balance FROM accounts WHERE id=?", (aid,))["opening_balance"] or 0)
        o = balance_before(seq, cps, start, opening) if start else opening
        c = balance_before(seq, cps, (end + " 99") if end else "9999", opening)
        inflow = outflow = 0.0
        for r in db.all(f"""SELECT id, kind, amount, to_amount, account_id, to_account_id FROM transactions
                WHERE status='posted' AND (account_id=? OR to_account_id=?) AND date >= ? AND date <= ?""",
                        (aid, aid, start or "", (end + " 99") if end else "9999")):
            e = tx_effect_for(r, aid)
            inflow += max(e, 0); outflow += max(-e, 0)
        out["balances"] = {"opening": round(o * sign, 2) + 0.0, "closing": round(c * sign, 2) + 0.0,
                           "inflow": round(inflow, 2), "outflow": round(outflow, 2), "liability": bool(a["is_liability"]),
                           "currency": a["currency"]}
    return out


def month_summary(db: Database, month: str | None = None, ledger_id: int | None = None) -> dict:
    """Home page cards: this month's income / expense / net / investment income (same rules as Reports)."""
    month = month or date.today().strftime("%Y-%m")
    y, m = map(int, month.split("-"))
    if not (1 <= m <= 12):
        raise ValidationError("month must be YYYY-MM")
    s, e = month_range(month)  # e = first instant of the next month (exclusive)
    t = totals(db, s, e, ledger_id)
    py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
    ny, nm = (y, m + 1) if m < 12 else (y + 1, 1)
    cond, params = "status='posted' AND date >= ? AND date < ?", [s, e]
    if ledger_id:
        cond += " AND ledger_id=?"; params.append(ledger_id)
    n = db.one(f"SELECT COUNT(*) n FROM transactions WHERE {cond}", params)["n"]
    last_day = (date(ny, nm, 1) - timedelta(days=1)).isoformat()
    sched = scheduled_remaining(db, f"{y}-{m:02d}", ledger_id)
    return {"month": f"{y}-{m:02d}", "start": s, "end": last_day, "prev": f"{py}-{pm:02d}", "next": f"{ny}-{nm:02d}",
            "income": t["income"], "expense": t["expense"], "net": t["net"], "investment_income": t["investment_income"],
            "currency": t["currency"], "count": n,
            # scheduled income/expense still to come this month + projected month-end = actual so far + scheduled
            "scheduled": sched, "projected": _projected(t, sched)}


def scheduled_remaining(db: Database, month: str, ledger_id: int | None = None) -> dict | None:
    from .schedules import month_remaining
    try:
        return month_remaining(db, month, ledger_id)
    except Exception:  # noqa: BLE001 - the KPI cards must render even if a schedule is broken
        log.exception("scheduled remaining failed")
        return None


def _projected(t: dict, sched: dict | None) -> dict:
    si, se = (sched or {}).get("income", 0.0), (sched or {}).get("expense", 0.0)
    inc, exp = round(t["income"] + si, 2), round(t["expense"] + se, 2)
    return {"income": inc, "expense": exp, "net": round(inc - exp, 2)}


# ---------------------------------------------------------------- reports
INVEST_TYPES_SQL = "(" + ",".join(f"'{k}'" for k, v in ACCOUNT_TYPES.items() if v[3]) + ")"


def _flow_rows(db: Database, start: str, end: str, ledger_id: int | None, project_id: int | None = None):
    """Rows that count as income/expense. Besides expense/income/refund this includes balance adjustments whose reason
    is Unrecorded spending (→ expense), Unrecorded income (→ income) or Fees on a non-investment account (→ expense).
    Their amount is signed so that a balance *decrease* is a positive expense."""
    cond = "t.status='posted' AND t.date >= ? AND t.date < ?"
    params: list[Any] = [start, end]
    if ledger_id:
        cond += " AND t.ledger_id=?"; params.append(ledger_id)
    if project_id:
        cond += " AND t.project_id=?"; params.append(project_id)
    rows = [r for r in db.all(f"""SELECT t.kind, {REPORT_AMOUNT_SQL} amount, t.currency, t.date, t.category_id, c.parent_id, t.id, t.plan_line_id,
        t.member_id, t.merchant_id, t.project_id, t.account_id, t.tags FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
        WHERE {cond} AND t.kind IN ('expense','income','refund')""", params) if not (r["kind"] == "expense" and not r["amount"])]
    for r in db.all(f"""SELECT t.amount, t.currency, t.date, t.category_id, t.member_id, t.merchant_id, t.project_id,
        t.account_id, t.tags, t.id, t.plan_line_id, {REASON_SQL} reason FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
        LEFT JOIN accounts a ON a.id=t.account_id
        WHERE {cond} AND t.kind='adjustment' AND (c.code IN ('unrecorded_spending','unrecorded_income')
          OR (c.code='fees' AND a.type NOT IN {INVEST_TYPES_SQL}))""", params):
        reason = r.pop("reason")
        income = reason == "unrecorded_income"
        rows.append({**r, "kind": "income" if income else "expense", "amount": r["amount"] if income else -r["amount"],
                     "parent_id": None, "adjustment": reason})
    return rows


def investment_income(db: Database, start: str, end: str, ledger_id: int | None = None) -> dict:
    """Investment income 投资收益 (base currency), shown separately from income/expense:
    adjustments classified Investment gain/loss or Interest, Fees adjustments on investment accounts (negative),
    dividends, and investment fees (negative)."""
    base = db.get_setting("base_currency", "USD"); rates = fx_rates(db)
    cond = "t.status='posted' AND t.date >= ? AND t.date < ?"
    params: list[Any] = [start, end]
    if ledger_id:
        cond += " AND t.ledger_id=?"; params.append(ledger_id)
    out = {"gains": 0.0, "losses": 0.0, "interest": 0.0, "dividends": 0.0, "fees": 0.0}
    for r in db.all(f"""SELECT t.kind, t.amount, t.currency, {REASON_SQL} reason, a.type atype FROM transactions t
        LEFT JOIN categories c ON c.id=t.category_id LEFT JOIN accounts a ON a.id=t.account_id
        WHERE {cond} AND t.kind IN ('adjustment','dividend','invest_fee')""", params):
        v = convert(r["amount"], r["currency"], base, rates)
        if r["kind"] == "dividend":
            out["dividends"] += v
        elif r["kind"] == "invest_fee":
            out["fees"] -= v
        elif r["reason"] == "investment_gain":
            out["gains" if v >= 0 else "losses"] += v
        elif r["reason"] == "interest":
            out["interest"] += v
        elif r["reason"] == "fees" and is_investment(r["atype"] or ""):
            out["fees"] += v
    out = {k: round(v, 2) + 0.0 for k, v in out.items()}
    out["total"] = round(sum(out.values()), 2) + 0.0
    out["currency"] = base
    return out


def totals(db: Database, start: str, end: str, ledger_id: int | None = None, project_id: int | None = None) -> dict:
    base = db.get_setting("base_currency", "USD"); rates = fx_rates(db)
    inc = exp = 0.0
    for r in _flow_rows(db, start, end, ledger_id, project_id):
        v = convert(r["amount"], r["currency"], base, rates)
        if r["kind"] == "income":
            inc += v
        elif r["kind"] == "expense":
            exp += v
        else:
            exp -= v
    return {"income": round(inc, 2), "expense": round(exp, 2), "net": round(inc - exp, 2), "currency": base,
            "investment_income": 0.0 if project_id else investment_income(db, start, end, ledger_id)["total"]}


def breakdown(db: Database, start: str, end: str, group_by: str = "category", kind: str = "expense",
              ledger_id: int | None = None, project_id: int | None = None, parent_id: int | None = None) -> list[dict]:
    """parent_id: only rows in that top-level category (drill-down; use with group_by='subcategory')."""
    base = db.get_setting("base_currency", "USD"); rates = fx_rates(db)
    sums: dict[Any, float] = defaultdict(float)
    counts: dict[Any, int] = defaultdict(int)
    for r in _flow_rows(db, start, end, ledger_id, project_id):
        if parent_id and (r.get("parent_id") or r["category_id"]) != parent_id:
            continue
        if kind == "expense" and r["kind"] not in ("expense", "refund"):
            continue
        if kind == "income" and r["kind"] != "income":
            continue
        v = convert(r["amount"], r["currency"], base, rates) * (-1 if r["kind"] == "refund" else 1)
        if group_by == "category":
            keys = [r["parent_id"] or r["category_id"]]
        elif group_by == "subcategory":
            keys = [r["category_id"]]
        elif group_by == "month":
            keys = [r["date"][:7]]
        elif group_by == "tag":
            keys = [t for t in (r["tags"] or "").split(",") if t] or [None]
        else:
            keys = [r.get(group_by + "_id")]
        for k in keys:
            sums[k] += v
            counts[k] += 1
    names: dict[Any, str] = {}
    if group_by in ("category", "subcategory"):
        cats = {c["id"]: c for c in db.all("SELECT * FROM categories")}
        for k in sums:
            c = cats.get(k)
            names[k] = category_label(c, cats.get(c["parent_id"]) if c and group_by == "subcategory" else None) if c else "Uncategorized 未分类"
            if c and c["kind"] == "adjustment":
                names[k] = f"{c['name']} {c['name_zh']} (balance adj. 余额调整)"
    elif group_by in ("member", "merchant", "project", "account"):
        tbl = {"member": "members", "merchant": "merchants", "project": "projects", "account": "accounts"}[group_by]
        nm = {r["id"]: r["name"] for r in db.all(f"SELECT id, name FROM {tbl}")}
        for k in sums:
            names[k] = nm.get(k, "(none 无)")
    else:
        for k in sums:
            names[k] = k or "(none 无)"
    total = sum(sums.values()) or 1
    rows = [{"key": k, "name": names[k], "amount": round(v, 2), "count": counts[k], "pct": round(v / total * 100, 1)}
            for k, v in sums.items()]
    rows.sort(key=lambda x: -x["amount"])
    return rows


def month_range(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    return f"{y}-{m:02d}-01", month_end(y, m)


def trend(db: Database, months: int = 12, ledger_id: int | None = None) -> list[dict]:
    out = []
    for y, m in last_n_months(months):
        s, e = month_range(f"{y}-{m:02d}")
        out.append({"month": f"{y}-{m:02d}", **totals(db, s, e, ledger_id)})
    return out


def budget_progress(db: Database, month: str, ledger_id: int | None = None) -> list[dict]:
    lid = ledger_id or 1
    s, e = month_range(month)
    spent = {r["key"]: r["amount"] for r in breakdown(db, s, e, "category", "expense", lid)}
    spent_sub = {r["key"]: r["amount"] for r in breakdown(db, s, e, "subcategory", "expense", lid)}
    total_spent = totals(db, s, e, lid)["expense"]
    cats = {c["id"]: c for c in db.all("SELECT * FROM categories")}
    out = []
    for b in db.all("SELECT * FROM budgets WHERE month=? AND (ledger_id=? OR ledger_id IS NULL) ORDER BY category_id IS NOT NULL, id", (month, lid)):
        c = cats.get(b["category_id"])
        if c is None:
            used, name = total_spent, "Total budget 总预算"
        elif c["parent_id"]:
            used, name = spent_sub.get(c["id"], 0), category_label(c, cats.get(c["parent_id"]))
        else:
            used, name = spent.get(c["id"], 0), category_label(c)
        out.append({**b, "name": name, "spent": round(used, 2), "remaining": round(b["amount"] - used, 2),
                    "pct": round(used / b["amount"] * 100, 1) if b["amount"] else 0})
    return out


def dashboard(db: Database, month: str | None = None, ledger_id: int | None = None) -> dict:
    month = month or date.today().strftime("%Y-%m")
    s, e = month_range(month)
    accts = accounts_with_balances(db, include_archived=False)
    recent, _ = query_tx(db, {"limit": 10, "ledger_id": ledger_id})
    return {
        "month": month,
        "totals": totals(db, s, e, ledger_id),
        "scheduled_remaining": scheduled_remaining(db, month, ledger_id),
        "investment_income": investment_income(db, s, e, ledger_id),
        "upcoming": _upcoming(db),
        "by_category": breakdown(db, s, e, "category", "expense", ledger_id),
        "trend": trend(db, 12, ledger_id),
        "net_worth": net_worth(db, accts),
        "networth_history": networth_history(db, 12),
        "accounts": accts,
        "recent": recent,
        "budgets": budget_progress(db, month, ledger_id),
        "review_count": db.one("SELECT COUNT(*) n FROM transactions WHERE status='review'")["n"],
        "has_demo": bool(db.one("SELECT 1 x FROM accounts WHERE is_demo=1 LIMIT 1")),
    }


def _upcoming(db: Database) -> list[dict]:
    from .schedules import upcoming
    try:
        return upcoming(db, 30, limit=8)
    except Exception:  # noqa: BLE001 - the dashboard must render even if a schedule is broken
        return []


def tx_dedupe_key(date_s: str, amount: float, account: str, note: str) -> str:
    import hashlib
    return hashlib.sha1(f"{date_s[:16]}|{round(abs(amount or 0), 2)}|{account}|{(note or '').strip()}".encode()).hexdigest()[:20]


# ---------------------------------------------------------------- set balance (随手记-style 余额变更)
def _fmt_amt(v: float, cur: str) -> str:
    return f"{v:,.2f} {cur}"


def account_balance_at(db: Database, account_id: int, date: str | None = None) -> float:
    """Signed balance (asset +, liability -) incl. opening balance and posted tx up to `date` (inclusive).
    Investment accounts add the current market value of holdings."""
    cash = cash_balances(db, until=date).get(account_id, 0.0)
    hv = sum(h.get("market_value") or 0 for h in holdings(db, account_id, until=date))
    return round(cash + hv, 2)


def set_balance(db: Database, account_id: int, balance: float, date: str | None = None, note: str | None = None,
                source: str = "balance_set", ledger_id: int | None = None, as_owed: bool | None = None,
                reason: str | None = None, status: str = "posted", extra: dict | None = None,
                dry_run: bool = False) -> dict:
    """Record an adjustment so the account's balance as of `date` equals `balance`.
    status='review' parks it in the review inbox; extra = more transaction fields (external_id, confidence…);
    dry_run computes the difference without writing.
    For liability accounts `balance` is the amount owed (positive) unless as_owed=False."""
    acct = db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not acct:
        raise ValidationError("Account not found")
    from .db import ACCOUNT_TYPES
    liability = bool(ACCOUNT_TYPES.get(acct["type"], ("", "", 0, 0))[2])
    owed_mode = liability if as_owed is None else bool(as_owed)
    try:
        balance = float(str(balance).replace(",", ""))
    except (TypeError, ValueError):
        raise ValidationError("balance must be a number")
    when = None
    if date:
        when = str(date).replace("T", " ").strip()
        when = when[:16] if len(when) >= 16 else (when[:10] + " 23:59" if len(when) == 10 else when)
    else:
        when = datetime.now().strftime("%Y-%m-%d %H:%M")
    reason = (reason or "").strip().lower() or default_reason(acct["type"])
    if reason not in REASON_CODES:
        raise ValidationError(f"reason must be one of {REASON_CODES}")
    target = -balance if owed_mode else balance
    before = account_balance_at(db, account_id, when)
    diff = round(target - before, 2)
    shown_before = -before if owed_mode else before
    word = "Amount owed" if owed_mode else "Balance"
    info = {"account_id": account_id, "account_name": acct["name"], "currency": acct["currency"], "date": when,
            "liability": liability, "entered": balance, "previous": round(shown_before, 2) + 0.0,
            "previous_signed": before, "target_signed": round(target, 2) + 0.0, "difference": diff + 0.0,
            "reason": reason, "reason_label": reason_label(reason, diff)}
    if diff == 0:
        info.update(created=False, transaction=None,
                    message=f"{word} of {acct['name']} is already {_fmt_amt(balance, acct['currency'])} on {when[:10]}; nothing recorded.")
        return info
    auto = f"{word} set to {_fmt_amt(balance, acct['currency'])} (was {_fmt_amt(shown_before, acct['currency'])}) 余额调整"
    if dry_run:
        info.update(created=False, transaction=None, preview=True,
                    message=f"Would record an adjustment of {diff:+,.2f} {acct['currency']} on {acct['name']} "
                            f"({reason_label(reason, diff)}): {auto}.")
        return info
    tx = create_tx(db, {"kind": "adjustment", "amount": diff, "account_id": account_id, "date": when,
                        "note": (note.strip() + " · " + auto) if note and note.strip() else auto, "source": source,
                        "adj_reason": reason,
                        "ledger_id": ledger_id or int(db.get_setting("default_ledger_id", "1") or 1), "status": status,
                        **(extra or {})})
    info.update(created=True, transaction=tx,
                message=f"Recorded an adjustment of {diff:+,.2f} {acct['currency']} on {acct['name']} "
                        f"({reason_label(reason, diff)}): {auto}.")
    return info


def record_opening_balance(db: Database, account_id: int, amount: float, date: str | None = None) -> dict | None:
    """Opening balance of a NEW account as an adjustment transaction dated at the account's start (signed amount)."""
    amount = round(float(amount or 0), 2)
    if not amount:
        return None
    when = (str(date).replace("T", " ")[:10] if date else datetime.now().strftime("%Y-%m-%d")) + " 00:00"
    return create_tx(db, {"kind": "adjustment", "amount": amount, "account_id": account_id, "date": when,
                          "note": "Opening balance 期初余额", "source": "opening_balance", "status": "posted",
                          "adj_reason": "opening",
                          "ledger_id": int(db.get_setting("default_ledger_id", "1") or 1)})


# ---------------------------------------------------------------- user preferences (stored in settings as JSON)
PREF_KEYS = {"tx_last", "quickadd_enter", "ui_lang"}
TX_LAST_FIELDS = ("account_id", "to_account_id", "category_id", "currency", "member_id")


def get_prefs(db: Database) -> dict:
    try:
        p = json.loads(db.get_setting("prefs") or "{}")
    except ValueError:
        p = {}
    p.setdefault("tx_last", {})
    p.setdefault("quickadd_enter", "add")
    return p


def _save_prefs(db: Database, p: dict) -> dict:
    db.set_setting("prefs", json.dumps(p, ensure_ascii=False))
    return p


def update_prefs(db: Database, body: dict) -> dict:
    p = get_prefs(db)
    for k, v in (body or {}).items():
        if k not in PREF_KEYS:
            raise ValidationError(f"Unknown preference '{k}'")
        if k == "quickadd_enter":
            if v not in ("add", "form"):
                raise ValidationError("quickadd_enter must be 'add' or 'form'")
            p[k] = v
        elif k == "ui_lang":  # saved UI language ('' = follow the browser)
            if v and not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]{2,4})?", str(v)):
                raise ValidationError("ui_lang must be a language code like 'en' or 'zh-CN'")
            p[k] = v or ""
        elif k == "tx_last":
            if not isinstance(v, dict):
                raise ValidationError("tx_last must be an object keyed by transaction type")
            for kind, vals in v.items():
                remember_tx(db, {"kind": kind, **(vals or {})}, prefs=p)
    return _save_prefs(db, p)


def remember_tx(db: Database, tx: dict, prefs: dict | None = None) -> dict:
    """Remember the last account(s)/category/currency/member used for this transaction type."""
    kind = tx.get("kind")
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {KINDS}")
    p = prefs if prefs is not None else get_prefs(db)
    cur = dict(p["tx_last"].get(kind) or {})
    for f in TX_LAST_FIELDS:
        if f not in tx:
            continue
        v = tx[f]
        if v in ("", None):
            cur.pop(f, None)
        elif f == "currency":
            cur[f] = str(v).upper()[:8]
        else:
            try:
                cur[f] = int(v)
            except (TypeError, ValueError):
                raise ValidationError(f"{f} must be an id")
            table = {"account_id": "accounts", "to_account_id": "accounts", "category_id": "categories",
                     "member_id": "members"}[f]
            if not db.one(f"SELECT id FROM {table} WHERE id=?", (cur[f],)):
                raise ValidationError(f"{f} {cur[f]} does not exist")
    p["tx_last"][kind] = cur
    return p if prefs is not None else _save_prefs(db, p)
