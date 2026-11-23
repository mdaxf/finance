"""Reimbursements 报销.

* An expense can be flagged reimbursable 可报销 (transactions.reimbursable=1), with optional payer (reimb_payer: who
  pays it back), expected amount (reimb_expected, default = the expense amount, in the expense's currency) and note.
* A reimbursement is a transaction of kind 'reimbursement' (money in to an account; balances change normally). It can
  be recorded from the Reimbursement Center (already linked) or entered anywhere else (form, quick add, import, API) and
  linked later, from either side.
* Links reimb_links(receipt_id = the reimbursement, expense_id, amount, alloc_amount): many-to-many with partial
  amounts. `amount` = how much of the expense the link pays back (expense currency); `alloc_amount` = how much of the
  reimbursement it uses (reimbursement currency; the same number when both currencies match).
  A link can't exceed the expense's outstanding amount nor the reimbursement's unallocated amount.
* Expense:  reimbursed = Σ link amounts, outstanding = expected − reimbursed.
  State open (nothing reimbursed) → partial → closed. It closes by itself when outstanding reaches 0, or by hand
  (close = write-off 核销: the rest won't be reimbursed); reopen undoes a manual close.
  Reimbursement: allocated = Σ alloc amounts, unallocated = amount − allocated. A reimbursement with money left can be
  marked complete 完成 (reimb_done=1): the rest is left as is (reimb_leftover), unallocated shows 0 and it is no longer
  offered when linking; reopen undoes it.
* Stats (ledger.REPORT_AMOUNT_SQL): a reimbursement is never income; a reimbursable expense counts 0 while open/partial
  and amount − reimbursed once closed, i.e. the reimbursed part never counts and only the written-off remainder does.
* Legacy status names are kept in `reimb_status`: pending (= open) / partial / settled (closed: fully reimbursed) /
  written_off (closed by hand). `reimb_state` is open / partial / closed.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .db import Database, distribute_alloc, now_iso
from .ledger import TX_FIELDS, TX_SELECT, ValidationError, convert, decorate, fx_rates, normalize_tx

STATUSES = {"pending": "待报销 Pending", "partial": "部分报销 Partial", "settled": "已报销 Settled",
            "written_off": "已核销 Written off"}
STATES = {"open": "Open", "partial": "Partial", "closed": "Closed"}
STATE_OF = {"pending": "open", "partial": "partial", "settled": "closed", "written_off": "closed"}
EPS = 0.005


def _r2(v: float) -> float:
    return round(v or 0.0, 2) + 0.0


# ------------------------------------------------------------------ sums
def _settled(db: Database, ids: list[int]) -> dict[int, float]:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r["expense_id"]: r["s"] for r in db.all(
        f"SELECT expense_id, SUM(amount) s FROM reimb_links WHERE expense_id IN ({q}) GROUP BY expense_id", ids)}


def _allocated(db: Database, ids: list[int]) -> dict[int, tuple[float, int]]:
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r["receipt_id"]: (r["s"], r["n"]) for r in db.all(
        f"SELECT receipt_id, SUM(COALESCE(alloc_amount, amount)) s, COUNT(*) n FROM reimb_links WHERE receipt_id IN ({q}) "
        "GROUP BY receipt_id", ids)}


def _expected(t: dict) -> float:
    return t["reimb_expected"] if t.get("reimb_expected") is not None else (t["amount"] or 0.0)


def _decorate(t: dict, settled: float, links: list[dict] | None) -> dict:
    expected = _expected(t)
    wo = bool(t.get("reimb_writeoff"))
    left = max(expected - settled, 0.0)
    if wo:
        st = "written_off"
    elif left <= EPS:
        st = "settled"
    elif settled > EPS:
        st = "partial"
    else:
        st = "pending"
    state = STATE_OF[st]
    t.update(reimb_expected_amount=_r2(expected), reimb_settled=_r2(settled),
             reimb_outstanding=0.0 if state == "closed" else _r2(left), reimb_status=st, reimb_status_label=STATUSES[st],
             reimb_state=state, reimb_closed_by=("written_off" if wo else "reimbursed") if state == "closed" else None,
             # the part that counts as a personal expense in stats (see ledger.REPORT_AMOUNT_SQL)
             reimb_counted=_r2(max((t["amount"] or 0) - settled, 0.0)) if state == "closed" else 0.0,
             reimb_writeoff_amount=_r2(left) if wo else 0.0)
    if links is not None:
        t["reimb_links"] = links
    return t


def _decorate_receipt(r: dict, allocated: float, n: int, links: list[dict] | None) -> dict:
    left, done = _r2(max((r["amount"] or 0) - allocated, 0.0)), bool(r.get("reimb_done"))
    r.update(reimb_allocated=_r2(allocated), reimb_unallocated=0.0 if done else left, reimb_leftover=left,
             reimb_done=done, reimb_link_count=n)
    if links is not None:
        r["links"] = links
    return r


# ------------------------------------------------------------------ link rows
LINK_SELECT = """SELECT l.id, l.expense_id, l.receipt_id, l.receipt_id reimbursement_id, l.amount,
  COALESCE(l.alloc_amount, l.amount) alloc_amount, l.created_at,
  e.date expense_date, e.amount expense_amount, e.currency expense_currency, e.note expense_note, e.reimb_payer expense_payer,
  em.name expense_merchant, ec.name expense_category, ec.name_zh expense_category_zh, ea.name expense_account,
  r.date reimbursement_date, r.amount reimbursement_amount, r.currency reimbursement_currency, r.note reimbursement_note,
  r.reimb_payer reimbursement_payer, ra.name reimbursement_account
  FROM reimb_links l JOIN transactions e ON e.id=l.expense_id JOIN transactions r ON r.id=l.receipt_id
  LEFT JOIN merchants em ON em.id=e.merchant_id LEFT JOIN categories ec ON ec.id=e.category_id
  LEFT JOIN accounts ea ON ea.id=e.account_id LEFT JOIN accounts ra ON ra.id=r.account_id"""


def _link_rows(db: Database, where: str, params) -> list[dict]:
    rows = db.all(f"{LINK_SELECT} WHERE {where} ORDER BY r.date, e.date, l.id", params)
    for x in rows:
        # legacy keys used by the expense / receipt views
        x.update(date=x["reimbursement_date"], receipt_amount=x["reimbursement_amount"],
                 receipt_currency=x["reimbursement_currency"], account_name=x["reimbursement_account"],
                 currency=x["expense_currency"], merchant_name=x["expense_merchant"], category_name=x["expense_category"],
                 category_name_zh=x["expense_category_zh"], note=x["expense_note"], reimb_payer=x["expense_payer"])
    return rows


def _links_for(db: Database, ids: list[int]) -> dict[int, list[dict]]:
    if not ids:
        return {}
    out: dict[int, list] = defaultdict(list)
    for r in _link_rows(db, f"l.expense_id IN ({','.join('?' * len(ids))})", ids):
        out[r["expense_id"]].append(r)
    return out


def _receipt_links_for(db: Database, ids: list[int]) -> dict[int, list[dict]]:
    if not ids:
        return {}
    out: dict[int, list] = defaultdict(list)
    for r in _link_rows(db, f"l.receipt_id IN ({','.join('?' * len(ids))})", ids):
        r["date"] = r["expense_date"]  # receipt view: a link row describes the expense
        out[r["receipt_id"]].append(r)
    return out


def list_links(db: Database, expense_id: int | None = None, reimbursement_id: int | None = None) -> dict:
    where, params = ["1=1"], []
    if expense_id:
        where.append("l.expense_id=?"); params.append(int(expense_id))
    if reimbursement_id:
        where.append("l.receipt_id=?"); params.append(int(reimbursement_id))
    return {"items": _link_rows(db, " AND ".join(where), params)}


def annotate(db: Database, rows: list[dict], links: bool = False) -> list[dict]:
    """Add reimbursement fields to transaction rows: reimbursable expenses get reimb_state / reimb_settled /
    reimb_outstanding (…), reimbursements get reimb_allocated / reimb_unallocated. links=True also lists the links."""
    eids = [r["id"] for r in rows if r.get("kind") == "expense" and r.get("reimbursable")]
    rids = [r["id"] for r in rows if r.get("kind") == "reimbursement"]
    if eids:
        st = _settled(db, eids)
        lk = _links_for(db, eids) if links else {}
        for r in rows:
            if r.get("kind") == "expense" and r.get("reimbursable"):
                _decorate(r, st.get(r["id"], 0.0), lk.get(r["id"], []) if links else None)
    if rids:
        al = _allocated(db, rids)
        lk = _receipt_links_for(db, rids) if links else {}
        for r in rows:
            if r.get("kind") == "reimbursement":
                a, n = al.get(r["id"], (0.0, 0))
                _decorate_receipt(r, a, n, lk.get(r["id"], []) if links else None)
                if links:
                    r["reimb_links"] = r["links"]
    return rows


# ------------------------------------------------------------------ expenses
def expense_status(db: Database, tid: int) -> dict:
    t = db.one(TX_SELECT + " WHERE t.id=?", (tid,))
    if not t or t["kind"] != "expense" or not t["reimbursable"]:
        raise ValidationError("Not a reimbursable expense")
    return _decorate(decorate(t), _settled(db, [tid]).get(tid, 0.0), _links_for(db, [tid]).get(tid, []))


def _status_filter(f: dict) -> set[str]:
    raw = [s.strip().lower() for s in str(f.get("status") or "").split(",") if s.strip()]
    return set(raw or ["open", "partial"])  # default: everything not closed


def _matches(t: dict, wanted: set[str]) -> bool:
    return "all" in wanted or t["reimb_state"] in wanted or t["reimb_status"] in wanted


def list_items(db: Database, f: dict) -> dict:
    """Reimbursable expenses (newest first) with state and outstanding amount, plus totals by currency / payer.
    status = any of open, partial, closed (or the legacy pending, settled, written_off), comma separated, or all.
    Default open,partial (closed ones hidden)."""
    where, params = ["t.kind='expense'", "COALESCE(t.reimbursable,0)=1", "t.status='posted'"], []
    if f.get("start"):
        where.append("t.date >= ?"); params.append(f["start"])
    if f.get("end"):
        where.append("t.date <= ?"); params.append(str(f["end"]) + " 99")
    if f.get("account_id"):
        where.append("t.account_id=?"); params.append(int(f["account_id"]))
    if f.get("payer"):
        if f["payer"] == "(none)":
            where.append("COALESCE(t.reimb_payer,'')=''")
        else:
            where.append("t.reimb_payer=?"); params.append(f["payer"])
    rows = [decorate(r) for r in db.all(TX_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY t.date DESC, t.id DESC", params)]
    ids = [r["id"] for r in rows]
    st, links = _settled(db, ids), _links_for(db, ids)
    rows = [_decorate(r, st.get(r["id"], 0.0), links.get(r["id"], [])) for r in rows]
    state_counts = {k: 0 for k in STATES}
    for r in rows:
        state_counts[r["reimb_state"]] += 1
    wanted = _status_filter(f)
    rows = [r for r in rows if _matches(r, wanted)]
    by_cur, by_payer = defaultdict(float), defaultdict(lambda: defaultdict(float))
    counts = defaultdict(int)
    for r in rows:
        counts[r["reimb_status"]] += 1
        if r["reimb_outstanding"] > EPS:
            by_cur[r["currency"]] += r["reimb_outstanding"]
            by_payer[r.get("reimb_payer") or "(none)"][r["currency"]] += r["reimb_outstanding"]
    base = db.get_setting("base_currency", "USD"); rates = fx_rates(db)
    payers = [p["p"] for p in db.all("""SELECT DISTINCT reimb_payer p FROM transactions WHERE (reimbursable=1 OR kind='reimbursement')
                                        AND COALESCE(reimb_payer,'')!='' ORDER BY 1""")]
    unalloc = receipts(db, 500, unallocated_only=True)["items"]
    un_cur = defaultdict(float)
    for r in unalloc:
        un_cur[r["currency"]] += r["reimb_unallocated"]
    return {"items": rows, "count": len(rows), "counts": dict(counts), "state_counts": state_counts,
            "statuses": STATUSES, "states": STATES, "payers": payers,
            "unallocated": {"count": len(unalloc), "by_currency": [{"currency": c, "amount": _r2(v)} for c, v in sorted(un_cur.items())]},
            "outstanding": {"by_currency": [{"currency": c, "amount": round(v, 2)} for c, v in sorted(by_cur.items())],
                            "total_base": round(sum(convert(v, c, base, rates) for c, v in by_cur.items()), 2),
                            "base_currency": base,
                            "by_payer": [{"payer": p, "by_currency": [{"currency": c, "amount": round(v, 2)} for c, v in sorted(d.items())]}
                                         for p, d in sorted(by_payer.items())]}}


# ------------------------------------------------------------------ reimbursements (receipts)
def record_receipt(db: Database, body: dict) -> dict:
    """Record a reimbursement received 报销到账 into account_id that settles `items` = [{expense_id, amount?}]
    (amount in the expense's currency; default = its outstanding amount; partial allowed). The receipt amount
    (account currency) defaults to the sum of the item amounts converted with the current rates."""
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise ValidationError("Select at least one reimbursable expense")
    if not body.get("account_id"):
        raise ValidationError("account_id (where the money arrived) is required")
    rates = fx_rates(db)
    with db.tx() as con:
        acct = con.execute("SELECT * FROM accounts WHERE id=?", (int(body["account_id"]),)).fetchone()
        if not acct:
            raise ValidationError("Account not found")
        links, seen, total_acct, payers = [], set(), 0.0, set()
        for it in items:
            eid = int(it.get("expense_id") or it.get("id") or 0)
            if eid in seen:
                raise ValidationError(f"Expense {eid} listed twice")
            seen.add(eid)
            e = _expense_for_link(con, eid)
            outstanding = _outstanding(con, e)
            amt = it.get("amount")
            amt = outstanding if amt in (None, "") else round(float(amt), 2)
            if amt <= 0:
                raise ValidationError(f"Expense {eid}: nothing left to reimburse" if outstanding <= EPS else "Amounts must be > 0")
            if amt > outstanding + EPS:
                raise ValidationError(f"Expense {eid}: {amt:.2f} is more than the outstanding {outstanding:.2f} {e['currency']}")
            links.append((eid, amt, e["currency"]))
            total_acct += convert(amt, e["currency"], acct["currency"], rates)
            if e["reimb_payer"]:
                payers.add(e["reimb_payer"])
        amount = body.get("amount")
        amount = round(total_acct, 2) if amount in (None, "") else round(float(amount), 2)
        if amount <= 0:
            raise ValidationError("Receipt amount must be > 0")
        payer = body.get("payer") or (payers.pop() if len(payers) == 1 else None)
        data = {"kind": "reimbursement", "account_id": acct["id"], "amount": amount,
                "currency": body.get("currency") or acct["currency"], "date": body.get("date"),
                "note": body.get("note") or ("报销 Reimbursement" + (f" · {payer}" if payer else "")),
                "reimb_payer": payer, "source": body.get("source") or "web", "status": "posted",
                "ledger_id": body.get("ledger_id"), "tags": body.get("tags") or ""}
        tx = normalize_tx(con, data, rates)
        same = [a for _, a, c in links if c == tx["currency"]]
        if len(same) == len(links) and sum(same) > tx["amount"] + EPS:
            raise ValidationError(f"The amount received ({tx['amount']:.2f}) is less than the amounts settled ({sum(same):.2f}): "
                                  "lower the per-expense amounts")
        allocs = distribute_alloc(tx["amount"], tx["currency"], [(a, c) for _, a, c in links], rates)
        tx["updated_at"] = tx["created_at"] = now_iso()
        cols = [k for k in TX_FIELDS + ["updated_at", "created_at"] if k in tx]
        rid = con.execute(f"INSERT INTO transactions({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                          [tx[k] for k in cols]).lastrowid
        for (eid, amt, _), al in zip(links, allocs):
            con.execute("INSERT INTO reimb_links(receipt_id, expense_id, amount, alloc_amount) VALUES(?,?,?,?)", (rid, eid, amt, al))
    return {"receipt": receipt(db, rid), "settled": [expense_status(db, eid) for eid, _, _ in links]}


def receipt(db: Database, rid: int) -> dict:
    r = db.one(TX_SELECT + " WHERE t.id=? AND t.kind='reimbursement'", (rid,))
    if not r:
        raise KeyError(rid)
    a, n = _allocated(db, [rid]).get(rid, (0.0, 0))
    return _decorate_receipt(decorate(r), a, n, _receipt_links_for(db, [rid]).get(rid, []))


def receipts(db: Database, limit: int = 100, unallocated_only: bool = False) -> dict:
    """Reimbursements, newest first, with allocated / unallocated and their links."""
    where = "t.kind='reimbursement' AND t.status='posted'"
    if unallocated_only:
        where += (" AND COALESCE(t.reimb_done,0)=0 AND t.amount - COALESCE((SELECT SUM(COALESCE(l.alloc_amount, l.amount)) FROM reimb_links l "
                  "WHERE l.receipt_id=t.id),0) > 0.005")
    rows = [decorate(r) for r in db.all(f"{TX_SELECT} WHERE {where} ORDER BY t.date DESC, t.id DESC LIMIT ?",
                                        (max(1, min(int(limit), 500)),))]
    ids = [r["id"] for r in rows]
    al, lk = _allocated(db, ids), _receipt_links_for(db, ids)
    return {"items": [_decorate_receipt(r, *al.get(r["id"], (0.0, 0)), lk.get(r["id"], [])) for r in rows]}


# ------------------------------------------------------------------ linking (from either side)
def _expense_for_link(con, eid: int):
    e = con.execute("SELECT * FROM transactions WHERE id=?", (eid,)).fetchone()
    if not e or e["kind"] != "expense" or not e["reimbursable"]:
        raise ValidationError(f"Transaction {eid} is not a reimbursable expense")
    if e["status"] != "posted":
        raise ValidationError(f"Expense {eid} is still in review: approve it first")
    if e["reimb_writeoff"]:
        raise ValidationError(f"Expense {eid} was closed (written off): reopen it first")
    return e


def _receipt_for_link(con, rid: int):
    r = con.execute("SELECT * FROM transactions WHERE id=?", (rid,)).fetchone()
    if not r or r["kind"] != "reimbursement":
        raise ValidationError(f"Transaction {rid} is not a reimbursement")
    if r["status"] != "posted":
        raise ValidationError(f"Reimbursement {rid} is still in review: approve it first")
    if r["reimb_done"]:
        raise ValidationError(f"Reimbursement {rid} is marked complete: reopen it first")
    return r


def _outstanding(con, e, exclude_link: int | None = None) -> float:
    s = con.execute("SELECT COALESCE(SUM(amount),0) FROM reimb_links WHERE expense_id=? AND id!=?",
                    (e["id"], exclude_link or 0)).fetchone()[0]
    exp = e["reimb_expected"] if e["reimb_expected"] is not None else e["amount"]
    return round(max(exp - s, 0.0), 2)


def _unallocated(con, r, exclude_link: int | None = None) -> float:
    s = con.execute("SELECT COALESCE(SUM(COALESCE(alloc_amount, amount)),0) FROM reimb_links WHERE receipt_id=? AND id!=?",
                    (r["id"], exclude_link or 0)).fetchone()[0]
    return round(max(r["amount"] - s, 0.0), 2)


def _num(v, what: str) -> float | None:
    if v in (None, ""):
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        raise ValidationError(f"{what} must be a number")


def _check_amounts(con, e, r, amount, alloc, rates, exclude_link=None) -> tuple[float, float]:
    """Resolve and validate one link's amounts: (amount in expense currency, alloc in reimbursement currency).
    Defaults: amount = the smaller of the expense's outstanding and the reimbursement's unallocated money."""
    out = _outstanding(con, e, exclude_link)
    un = _unallocated(con, r, exclude_link)
    same = e["currency"] == r["currency"]
    un_in_exp = un if same else round(convert(un, r["currency"], e["currency"], rates), 2)
    if amount is None:
        if alloc is not None and same:
            amount = alloc
        elif alloc is not None:
            amount = round(convert(alloc, r["currency"], e["currency"], rates), 2)
        else:
            amount = round(min(out, un_in_exp), 2)
    if alloc is None:
        alloc = amount if same else round(convert(amount, e["currency"], r["currency"], rates), 2)
        if not same and amount >= out - EPS and abs(alloc - un) <= max(0.02, un * 0.02):
            alloc = un  # rounding / rate noise: settling an expense with "the rest" of a reimbursement uses all of it
        if not same and un < alloc <= un + max(0.02, un * 0.02):
            alloc = un  # conversion noise only; a real shortfall is still rejected below
    if same and abs(alloc - amount) > EPS:
        raise ValidationError("Same currency: the expense part and the reimbursement part of a link must be equal")
    if amount <= 0 or alloc <= 0:
        if out <= EPS:
            raise ValidationError(f"Expense {e['id']} has nothing outstanding")
        if un <= EPS:
            raise ValidationError(f"Reimbursement {r['id']} has no unallocated money left")
        raise ValidationError("Link amounts must be greater than zero")
    if amount > out + EPS:
        raise ValidationError(f"Expense {e['id']}: {amount:.2f} is more than its outstanding {out:.2f} {e['currency']}")
    if alloc > un + EPS:
        raise ValidationError(f"Reimbursement {r['id']}: {alloc:.2f} is more than its unallocated {un:.2f} {r['currency']}")
    return amount, alloc


def link(db: Database, body: dict) -> dict:
    """Create links, all or nothing. Accepts one link {expense_id, reimbursement_id, amount?, alloc_amount?} or a batch
    {reimbursement_id, items: [{expense_id, amount?}]} / {expense_id, items: [{reimbursement_id, amount?}]} /
    {items: [{expense_id, reimbursement_id, amount?}]}. receipt_id is accepted for reimbursement_id."""
    rid0 = body.get("reimbursement_id") or body.get("receipt_id")
    eid0 = body.get("expense_id")
    items = body.get("items")
    if items is None:
        items = [{"expense_id": eid0, "reimbursement_id": rid0, "amount": body.get("amount"), "alloc_amount": body.get("alloc_amount")}]
    if not isinstance(items, list) or not items:
        raise ValidationError("Nothing to link")
    rates = fx_rates(db)
    made, eids, rids = [], set(), set()
    with db.tx() as con:
        for it in items:
            try:
                eid = int(it.get("expense_id") or eid0 or 0)
                rid = int(it.get("reimbursement_id") or it.get("receipt_id") or rid0 or 0)
            except (TypeError, ValueError):
                raise ValidationError("expense_id and reimbursement_id must be ids")
            if not eid or not rid:
                raise ValidationError("A link needs an expense_id and a reimbursement_id")
            e, r = _expense_for_link(con, eid), _receipt_for_link(con, rid)
            if con.execute("SELECT 1 FROM reimb_links WHERE expense_id=? AND receipt_id=?", (eid, rid)).fetchone():
                raise ValidationError(f"Expense {eid} and reimbursement {rid} are already linked: edit that link's amount instead")
            amount, alloc = _check_amounts(con, e, r, _num(it.get("amount"), "amount"), _num(it.get("alloc_amount"), "alloc_amount"), rates)
            lid = con.execute("INSERT INTO reimb_links(receipt_id, expense_id, amount, alloc_amount, created_at) VALUES(?,?,?,?,?)",
                              (rid, eid, amount, alloc, now_iso())).lastrowid
            made.append(lid); eids.add(eid); rids.add(rid)
    return _result(db, made, eids, rids)


def _result(db: Database, link_ids, eids, rids) -> dict:
    lks = _link_rows(db, f"l.id IN ({','.join('?' * len(link_ids))})", list(link_ids)) if link_ids else []
    return {"ok": True, "links": lks, "expenses": [expense_status(db, i) for i in sorted(eids)],
            "reimbursements": [receipt(db, i) for i in sorted(rids)]}


def update_link(db: Database, lid: int, body: dict) -> dict:
    """Change a link's amount {amount?, alloc_amount?} (same limits as a new link, not counting the link itself)."""
    rates = fx_rates(db)
    with db.tx() as con:
        l = con.execute("SELECT * FROM reimb_links WHERE id=?", (lid,)).fetchone()
        if not l:
            raise KeyError(lid)
        e, r = _expense_for_link(con, l["expense_id"]), _receipt_for_link(con, l["receipt_id"])
        amount, alloc = _num(body.get("amount"), "amount"), _num(body.get("alloc_amount"), "alloc_amount")
        if amount is None and alloc is None:
            raise ValidationError("Give the new amount")
        amount, alloc = _check_amounts(con, e, r, amount, alloc, rates, exclude_link=lid)
        con.execute("UPDATE reimb_links SET amount=?, alloc_amount=? WHERE id=?", (amount, alloc, lid))
    return _result(db, [lid], {l["expense_id"]}, {l["receipt_id"]})


def unlink(db: Database, lid: int) -> dict:
    l = db.one("SELECT * FROM reimb_links WHERE id=?", (lid,))
    if not l:
        raise KeyError(lid)
    with db.tx() as con:
        con.execute("DELETE FROM reimb_links WHERE id=?", (lid,))
    out = _result(db, [], set(), {l["receipt_id"]})
    e = db.one("SELECT kind, reimbursable FROM transactions WHERE id=?", (l["expense_id"],))
    out["expenses"] = [expense_status(db, l["expense_id"])] if e and e["kind"] == "expense" and e["reimbursable"] else []
    return out


# ------------------------------------------------------------------ suggestions
def _days(a: str, b: str) -> int:
    try:
        return abs((datetime.strptime(a[:10], "%Y-%m-%d") - datetime.strptime(b[:10], "%Y-%m-%d")).days)
    except (TypeError, ValueError):
        return 999


def _score(e: dict, r: dict, e_out: float, r_un: float, rates) -> tuple[float, list[str]]:
    """How likely expense e is (part of) what reimbursement r pays back: amount, date and payer."""
    sc, why = 0.0, []
    un = convert(r_un, r["currency"], e["currency"], rates)
    if un > 0 and abs(e_out - un) <= max(0.01, un * 0.005):
        sc += 50; why.append("amount")
    elif e_out <= un + EPS:
        sc += 15; why.append("fits")
    d = _days(e["date"], r["date"])
    if e["date"][:10] <= r["date"][:10]:
        sc += max(0, 30 - d * 0.5)
        if d <= 45:
            why.append("date")
    else:
        sc += max(0, 10 - d)  # an expense after the reimbursement is unusual (advance payment)
    payer = (r.get("reimb_payer") or "").strip().lower()
    names = [(e.get("reimb_payer") or "").lower(), (e.get("merchant_name") or "").lower()]
    note = (r.get("note") or "").lower()
    if payer and payer in names or any(n and n in note for n in names):
        sc += 25; why.append("payer")
    return round(sc, 1), why


def suggest(db: Database, reimbursement_id: int | None = None, expense_id: int | None = None, limit: int = 50) -> dict:
    """From a reimbursement: open reimbursable expenses, best match first (amount / date / payer).
    From an expense: reimbursements with unallocated money, best match first. Each item has default_amount
    (= the smaller of outstanding and unallocated) and, across currencies, default_alloc."""
    rates = fx_rates(db)
    limit = max(1, min(int(limit or 50), 200))
    if reimbursement_id:
        r = receipt(db, int(reimbursement_id))
        linked = {l["expense_id"] for l in r["links"]}
        cands = [x for x in list_items(db, {"status": "open,partial"})["items"] if x["id"] not in linked]
        out = []
        for e in cands:
            sc, why = _score(e, r, e["reimb_outstanding"], r["reimb_unallocated"], rates)
            un_e = r["reimb_unallocated"] if e["currency"] == r["currency"] else convert(r["reimb_unallocated"], r["currency"], e["currency"], rates)
            amt = _r2(min(e["reimb_outstanding"], un_e))
            e.update(score=sc, match=why, default_amount=amt,
                     default_alloc=amt if e["currency"] == r["currency"] else _r2(min(convert(amt, e["currency"], r["currency"], rates), r["reimb_unallocated"])))
            out.append(e)
        out.sort(key=lambda x: (-x["score"], x["date"]))
        return {"reimbursement": r, "items": out[:limit]}
    if expense_id:
        e = expense_status(db, int(expense_id))
        linked = {l["receipt_id"] for l in e["reimb_links"]}
        out = []
        for r in receipts(db, 500, unallocated_only=True)["items"]:
            if r["id"] in linked:
                continue
            sc, why = _score(e, r, e["reimb_outstanding"], r["reimb_unallocated"], rates)
            un_e = r["reimb_unallocated"] if e["currency"] == r["currency"] else convert(r["reimb_unallocated"], r["currency"], e["currency"], rates)
            amt = _r2(min(e["reimb_outstanding"], un_e))
            r.update(score=sc, match=why, default_amount=amt,
                     default_alloc=amt if e["currency"] == r["currency"] else _r2(min(convert(amt, e["currency"], r["currency"], rates), r["reimb_unallocated"])))
            out.append(r)
        out.sort(key=lambda x: (-x["score"], x["date"]))
        return {"expense": e, "items": out[:limit]}
    raise ValidationError("Give reimbursement_id or expense_id")


# ------------------------------------------------------------------ close / reopen, flag
def write_off(db: Database, tid: int, undo: bool = False) -> dict:
    """Close by hand (write off 核销: the rest won't be reimbursed and counts as a personal expense) or reopen (undo)."""
    e = db.one("SELECT kind, reimbursable FROM transactions WHERE id=?", (tid,))
    if not e or e["kind"] != "expense" or not e["reimbursable"]:
        raise ValidationError("Not a reimbursable expense")
    with db.tx() as con:
        con.execute("UPDATE transactions SET reimb_writeoff=?, updated_at=? WHERE id=?", (0 if undo else 1, now_iso(), tid))
    return expense_status(db, tid)


def complete(db: Database, rid: int, undo: bool = False) -> dict:
    """Mark a reimbursement complete (the unallocated rest is left as is and it is no longer offered for linking) or
    reopen it (undo)."""
    r = db.one("SELECT kind FROM transactions WHERE id=?", (rid,))
    if not r or r["kind"] != "reimbursement":
        raise ValidationError("Not a reimbursement")
    with db.tx() as con:
        con.execute("UPDATE transactions SET reimb_done=?, updated_at=? WHERE id=?", (0 if undo else 1, now_iso(), rid))
    return receipt(db, rid)


def mark(db: Database, ids: list[int], flag: bool, payer: str | None = None) -> dict:
    """Flag / unflag expenses as reimbursable 可报销 (unflagging is refused while reimbursements are linked)."""
    ids = [int(i) for i in ids or []]
    if not ids:
        raise ValidationError("Nothing selected")
    done = 0
    with db.tx() as con:
        for i in ids:
            e = con.execute("SELECT kind FROM transactions WHERE id=?", (i,)).fetchone()
            if not e or e["kind"] != "expense":
                raise ValidationError(f"Transaction {i} is not an expense")
            if not flag and con.execute("SELECT 1 FROM reimb_links WHERE expense_id=?", (i,)).fetchone():
                raise ValidationError(f"Expense {i} is linked to reimbursements: unlink them or close it instead")
            if flag:
                sets, vals = "reimbursable=1", []
                if payer is not None:
                    sets += ", reimb_payer=?"; vals.append(payer.strip() or None)
            else:
                sets, vals = "reimbursable=0, reimb_writeoff=0", []
            done += con.execute(f"UPDATE transactions SET {sets}, updated_at=? WHERE id=?", vals + [now_iso(), i]).rowcount
    return {"ok": True, "updated": done}
