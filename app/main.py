"""FastAPI application: auth, REST API, static SPA, background jobs."""
from __future__ import annotations

import hashlib
import hmac
import json
from urllib.parse import quote
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import backup as bk
from . import agent_api, config, demo, email_reports, mailer, offsite, importers, ingest, ledger, loans, plan_lines, taxtips, principal, projects, quotes, receipts, reimburse, reminders, reconcile, reserves, returns, schedules
from .db import ACCOUNT_TYPES, BASE_DIR, Database, now_iso
from .email_rules import parse_email

log = logging.getLogger("finance")
STATIC = BASE_DIR / "static"
PUBLIC_API = {"/api/auth/status", "/api/auth/login", "/api/auth/setup", "/healthz"}


# ---------------------------------------------------------------- password hashing
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 390000)
    return f"pbkdf2_sha256$390000${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), h)
    except (ValueError, AttributeError):
        return False


def create_app(db_path: str | os.PathLike | None = None, start_background: bool | None = None) -> FastAPI:
    db_path = db_path or str(config.db_path())
    db = Database(db_path)
    db.init()
    data_dir = Path(db.path).parent
    secret = ingest.SecretBox(data_dir / "secret.key")
    (data_dir / "attachments").mkdir(parents=True, exist_ok=True)
    (data_dir / "imports").mkdir(parents=True, exist_ok=True)
    if start_background is None:
        start_background = os.environ.get("FINANCE_DISABLE_SCHEDULER") != "1"

    app = FastAPI(title="IACF Finance", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.db = db
    app.state.secret = secret
    fails: dict[str, list[float]] = defaultdict(list)

    # ------------------------------------------------------------ reply-message language
    @app.middleware("http")
    async def lang_mw(request: Request, call_next):
        # API 'message' texts: ?lang= param > Accept-Language > English (user data is never translated)
        ingest.MSG_LANG.set(ingest.resolve_lang(request.query_params.get("lang"), request.headers.get("accept-language")))
        return await call_next(request)

    # ------------------------------------------------------------ auth middleware
    @app.middleware("http")
    async def auth_mw(request: Request, call_next):
        path = request.url.path
        if path.startswith("/static/") or path in PUBLIC_API or path in ("/login", "/setup", "/favicon.ico", "/manifest.json"):
            return await call_next(request)
        key = request.headers.get("x-api-key")
        api_ok = bool(key) and hmac.compare_digest(key, db.get_setting("api_key", "") or "")
        sess_ok = bool(request.session.get("user")) and request.session.get("pwv") == (db.get_setting("password_hash") or "")[-12:]
        if api_ok or sess_ok:
            request.state.via = "api_key" if api_ok else "session"
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated (login or X-API-Key header required)",
                                 "message": "Not signed in: add the X-API-Key header"}, status_code=401)
        return RedirectResponse("/setup" if not db.get_setting("password_hash") else "/login", status_code=303)

    app.add_middleware(SessionMiddleware, secret_key=db.get_setting("session_secret"), session_cookie="iacf_session",
                       max_age=60 * 60 * 24 * 30, same_site="lax", https_only=False)

    @app.exception_handler(ledger.ValidationError)
    async def _val(_req, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # ------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/login", include_in_schema=False)
    def login_page():
        if not db.get_setting("password_hash"):
            return RedirectResponse("/setup", status_code=303)
        return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})

    @app.get("/setup", include_in_schema=False)
    def setup_page():
        if db.get_setting("password_hash"):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "setup_required": not db.get_setting("password_hash")}

    # ------------------------------------------------------------ auth api
    @app.get("/api/auth/status")
    def auth_status(request: Request):
        return {"setup_required": not db.get_setting("password_hash"), "logged_in": bool(request.session.get("user"))}

    @app.post("/api/auth/setup")
    def auth_setup(request: Request, body: dict = Body(...)):
        with db.lock:
            if db.get_setting("password_hash"):
                raise HTTPException(409, "Password already set")
            pw = str(body.get("password") or "")
            if len(pw) < 8:
                raise HTTPException(400, "Password must be at least 8 characters")
            db.set_setting("password_hash", hash_password(pw))
        request.session["user"] = "owner"
        request.session["pwv"] = db.get_setting("password_hash")[-12:]
        return {"ok": True}

    @app.post("/api/auth/login")
    def auth_login(request: Request, body: dict = Body(...)):
        ip = request.client.host if request.client else "?"
        now = time.time()
        fails[ip] = [t for t in fails[ip] if now - t < 300]
        if len(fails[ip]) >= 8:
            raise HTTPException(429, "Too many attempts, wait 5 minutes")
        stored = db.get_setting("password_hash")
        if not stored:
            raise HTTPException(409, "Setup required")
        if not verify_password(str(body.get("password") or ""), stored):
            fails[ip].append(now)
            time.sleep(0.5)
            raise HTTPException(401, "Wrong password")
        request.session["user"] = "owner"
        request.session["pwv"] = stored[-12:]
        return {"ok": True}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request):
        request.session.clear()
        return {"ok": True}

    @app.post("/api/auth/change-password")
    def change_password(request: Request, body: dict = Body(...)):
        if not verify_password(str(body.get("old_password") or ""), db.get_setting("password_hash") or ""):
            raise HTTPException(400, "Current password is wrong")
        pw = str(body.get("new_password") or "")
        if len(pw) < 8:
            raise HTTPException(400, "Password must be at least 8 characters")
        db.set_setting("password_hash", hash_password(pw))
        request.session["pwv"] = db.get_setting("password_hash")[-12:]
        return {"ok": True}

    # ------------------------------------------------------------ meta / settings
    SETTING_KEYS = ["base_currency", "review_threshold", "default_account_id", "default_ledger_id",
                    "backup_interval_hours", "backup_keep", "schedule_run_time", "schedule_catchup_days", "timezone",
                    "lock_reconciled", "receipts_dir", "receipts_pattern",
                    "reminder_lead_days", "reminder_notify", "backup_dir", "backup_receipts", "card_expiry_lead_days",
                    "reserve_release_days", "reserve_hold_days"]
    SETTING_DEFAULTS = {"schedule_run_time": "00:15", "schedule_catchup_days": "90", "timezone": schedules.DEFAULT_TZ, "lock_reconciled": "0",
                        "receipts_dir": "", "receipts_pattern": "{yyyy}/{mm}/{dd}",
                        "reminder_lead_days": "10", "reminder_notify": "off", "backup_dir": "", "backup_receipts": "1",
                        "card_expiry_lead_days": str(reminders.EXPIRY_LEAD_DEFAULT),
                        "reserve_release_days": str(reserves.RELEASE_DAYS_DEFAULT), "reserve_hold_days": str(reserves.HOLD_DAYS_DEFAULT)}

    @app.get("/api/meta")
    def meta():
        return {
            "account_types": {k: {"label": v[0], "group": v[1], "liability": v[2], "investment": v[3]} for k, v in ACCOUNT_TYPES.items()},
            "kinds": ledger.KINDS,
            "groups": db.all("SELECT * FROM account_groups ORDER BY sort, id"),
            "ledgers": db.all("SELECT * FROM ledgers ORDER BY id"),
            "categories": ledger.categories_tree(db),
            "members": db.all("SELECT * FROM members ORDER BY id"),
            "merchants": db.all("SELECT * FROM merchants ORDER BY name"),
            "projects": db.all("SELECT * FROM projects ORDER BY archived, name"),
            "plan_lines": db.all("SELECT id, project_id, name, sort FROM project_plan_lines ORDER BY project_id, sort, id"),
            "accounts": db.all("""SELECT id, name, type, currency, archived, aliases, last4, system_role,
                (SELECT MAX(as_of) FROM reconciliations r WHERE r.account_id=accounts.id) reconciled_at
                FROM accounts ORDER BY archived, sort, name"""),
            "currencies": db.all("SELECT * FROM fx_rates ORDER BY currency"),
            "settings": {k: db.get_setting(k, SETTING_DEFAULTS.get(k)) for k in SETTING_KEYS},
            "review_count": db.one("SELECT COUNT(*) n FROM transactions WHERE status='review'")["n"],
            "reminder_count": reminders.badge(db),
            "reserve_review_count": reserves.review_count(db),
            "has_demo": bool(db.one("SELECT 1 x FROM accounts WHERE is_demo=1 LIMIT 1")),
            "prefs": ledger.get_prefs(db),
            "adj_reasons": [{"code": c, "name": en, "name_zh": zh} for c, en, zh in ledger.ADJ_REASONS],
            "investment_types": returns.INVEST_TYPES,
        }

    @app.get("/api/prefs")
    def get_prefs():
        return ledger.get_prefs(db)

    @app.put("/api/prefs")
    def put_prefs(body: dict = Body(...)):
        try:
            return ledger.update_prefs(db, body)
        except ledger.ValidationError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/prefs/tx-last")
    def remember_tx(body: dict = Body(...)):
        try:
            return ledger.remember_tx(db, body)
        except ledger.ValidationError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/settings")
    def get_settings(request: Request):
        out = {k: db.get_setting(k, SETTING_DEFAULTS.get(k)) for k in SETTING_KEYS}
        out["api_key"] = db.get_setting("api_key")
        out["last_backup_at"] = db.get_setting("last_backup_at")
        out["last_gdrive_sync"] = db.get_setting("last_gdrive_sync")
        out["telegram_enabled"] = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
        out["db_path"] = db.path
        return out

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        for k, v in body.items():
            if k in SETTING_KEYS:
                if k == "base_currency":
                    v = str(v).upper()
                    if not db.one("SELECT 1 x FROM fx_rates WHERE currency=?", (v,)):
                        raise HTTPException(400, f"Add an exchange rate for {v} first")
                if k == "schedule_run_time":
                    try:
                        v = datetime.strptime(str(v).strip()[:5], "%H:%M").strftime("%H:%M")
                    except ValueError:
                        raise HTTPException(400, "Run time must be HH:MM")
                if k == "schedule_catchup_days":
                    if not str(v).isdigit() or not 0 <= int(v) <= 3650:
                        raise HTTPException(400, "Catch-up days must be 0–3650")
                if k == "backup_dir":
                    try:
                        v = bk.check_dir(str(v or ""))
                    except ValueError as e:
                        raise HTTPException(400, str(e))
                if k == "card_expiry_lead_days":
                    if not str(v).strip().isdigit() or not 0 <= int(str(v).strip()) <= 365:
                        raise HTTPException(400, "Card expiry reminder days must be 0–365")
                    v = str(int(str(v).strip()))
                if k in ("reserve_release_days", "reserve_hold_days"):
                    if not str(v).strip().isdigit() or not 0 <= int(str(v).strip()) <= 365:
                        raise HTTPException(400, ("Reserve reminder days after check-out" if k == "reserve_release_days"
                                                  else "Reserve reminder days after check-in") + " must be 0–365")
                    v = str(int(str(v).strip()))
                if k == "backup_receipts":
                    v = "0" if str(v).lower() in ("0", "false", "off", "no", "") else "1"
                if k == "receipts_pattern":
                    try:
                        v = receipts.validate_pattern(str(v or ""))
                    except receipts.ReceiptError as e:
                        raise HTTPException(400, str(e))
                if k == "receipts_dir" and v:
                    pth = Path(os.path.expanduser(str(v).strip()))
                    if not pth.is_absolute():
                        raise HTTPException(400, "Receipts folder must be an absolute path (blank = data/receipts)")
                    try:
                        pth.mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        raise HTTPException(400, f"Can't use that folder: {e}")
                    v = str(pth)
                if k == "timezone":
                    try:
                        from zoneinfo import ZoneInfo
                        ZoneInfo(str(v))
                    except Exception:  # noqa: BLE001
                        raise HTTPException(400, f"Unknown time zone {v}")
                db.set_setting(k, "" if v is None else v)
        return {k: db.get_setting(k, SETTING_DEFAULTS.get(k)) for k in SETTING_KEYS}

    @app.post("/api/settings/regenerate-api-key")
    def regen_key():
        k = "fin_" + secrets.token_urlsafe(32)
        db.set_setting("api_key", k)
        _write_key_file(db)
        return {"api_key": k}

    # ------------------------------------------------------------ fx
    @app.get("/api/fx")
    def get_fx():
        return {"base": db.get_setting("base_currency", "USD"), "rates": db.all("SELECT * FROM fx_rates ORDER BY currency")}

    @app.put("/api/fx")
    def put_fx(body: dict = Body(...)):
        cur = str(body.get("currency") or "").upper().strip()
        if not re.fullmatch(r"[A-Z]{3,5}", cur):
            raise HTTPException(400, "Currency code like USD / CNY")
        rate = float(body.get("rate") or 0)
        if rate <= 0:
            raise HTTPException(400, "Rate must be > 0")
        with db.tx() as con:
            con.execute("INSERT INTO fx_rates(currency,rate,updated_at) VALUES(?,?,?) ON CONFLICT(currency) DO UPDATE SET rate=excluded.rate, updated_at=excluded.updated_at",
                        (cur, rate, now_iso()))
        return get_fx()

    @app.delete("/api/fx/{cur}")
    def del_fx(cur: str):
        if cur.upper() == db.get_setting("base_currency"):
            raise HTTPException(400, "Cannot delete the base currency")
        if db.one("SELECT 1 x FROM accounts WHERE currency=?", (cur.upper(),)):
            raise HTTPException(400, "Currency in use by an account")
        with db.tx() as con:
            con.execute("DELETE FROM fx_rates WHERE currency=?", (cur.upper(),))
        return get_fx()

    # ------------------------------------------------------------ accounts
    ACCT_FIELDS = ["group_id", "name", "type", "currency", "opening_balance", "credit_limit", "institution", "last4",
                   "aliases", "note", "archived", "sort", "opening_returns",
                   "stmt_day", "due_day", "due_after_days", "remind_days", "autopay", "min_pay_pct", "card_expiry"]

    def _acct_data(body: dict, partial=False) -> dict:
        d = {k: body[k] for k in ACCT_FIELDS if k in body}
        if not partial:
            if not str(d.get("name") or "").strip():
                raise HTTPException(400, "Name required")
            if d.get("type") not in ACCOUNT_TYPES:
                raise HTTPException(400, f"type must be one of {list(ACCOUNT_TYPES)}")
        if "type" in d and d["type"] not in ACCOUNT_TYPES:
            raise HTTPException(400, "Bad account type")
        if "currency" in d:
            d["currency"] = str(d["currency"] or "USD").upper()
            if not db.one("SELECT 1 x FROM fx_rates WHERE currency=?", (d["currency"],)):
                raise HTTPException(400, f"Unknown currency {d['currency']}: add it under Settings > Currencies")
        if not d.get("group_id") and d.get("type"):
            g = db.one("SELECT id FROM account_groups WHERE name=?", (ACCOUNT_TYPES[d["type"]][1],))
            d["group_id"] = g["id"] if g else None
        for k in ("opening_balance", "credit_limit", "opening_returns", "min_pay_pct"):
            if k in d:
                d[k] = None if d[k] in ("", None) else float(d[k])
        for k, lo, hi in (("stmt_day", 1, 31), ("due_day", 1, 31), ("due_after_days", 1, 60), ("remind_days", 0, 60)):
            if k in d:
                try:
                    d[k] = None if d[k] in ("", None) else int(d[k])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{k} must be a whole number")
                if d[k] is not None and not lo <= d[k] <= hi:
                    raise HTTPException(400, f"{k} must be between {lo} and {hi}")
        if "card_expiry" in d:
            try:
                d["card_expiry"] = reminders.parse_expiry(d["card_expiry"])
            except ValueError as e:
                raise HTTPException(400, str(e))
        if "autopay" in d:
            d["autopay"] = 1 if d["autopay"] in (True, 1, "1", "true", "on") else 0
        if "opening_balance" in d and d["opening_balance"] is None:
            d["opening_balance"] = 0
        return d

    @app.get("/api/accounts")
    def list_accounts():
        accts = ledger.accounts_with_balances(db)
        principal.annotate_accounts(db, accts)
        return {"accounts": accts, "net_worth": ledger.net_worth(db, [a for a in accts]),
                "tree": ledger.account_tree(db, accts),
                "groups": db.all("SELECT * FROM account_groups ORDER BY sort, id")}

    @app.post("/api/accounts")
    def create_account(body: dict = Body(...)):
        d = _acct_data(body)
        d.setdefault("currency", db.get_setting("base_currency", "USD"))
        # A new account's opening balance is recorded as an "Opening balance 期初余额" adjustment transaction dated at
        # the account's start date (start_date, default today), so history stays consistent. The legacy
        # accounts.opening_balance field stays 0 for new accounts (no double counting).
        opening = d.pop("opening_balance", 0) or 0
        with db.tx() as con:
            cols = list(d)
            aid = con.execute(f"INSERT INTO accounts({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [d[c] for c in cols]).lastrowid
        if d.get("card_expiry"):
            reminders.sync_expiry(db)
        if opening:
            try:
                ledger.record_opening_balance(db, aid, opening, body.get("start_date"))
            except ledger.ValidationError as e:
                raise HTTPException(400, str(e))
        return next(a for a in ledger.accounts_with_balances(db) if a["id"] == aid)

    @app.put("/api/accounts/{aid}")
    def update_account(aid: int, body: dict = Body(...)):
        d = _acct_data(body, partial=True)
        if not db.one("SELECT 1 x FROM accounts WHERE id=?", (aid,)):
            raise HTTPException(404)
        if d:
            with db.tx() as con:
                con.execute(f"UPDATE accounts SET {','.join(k + '=?' for k in d)} WHERE id=?", list(d.values()) + [aid])
            if {"card_expiry", "archived", "type"} & set(d):
                reminders.sync_expiry(db)  # a renewed card confirms its expiry reminder; a new expiry starts a new cycle
        result = None
        # Editing the balance itself records an adjustment (like 随手记's 余额变更) instead of rewriting the opening balance.
        if body.get("balance") not in (None, ""):
            try:
                result = ledger.set_balance(db, aid, body["balance"], body.get("balance_date"), body.get("balance_note"),
                                            reason=body.get("balance_reason"))
            except ledger.ValidationError as e:
                raise HTTPException(400, str(e))
        out = next(a for a in ledger.accounts_with_balances(db) if a["id"] == aid)
        if result is not None:
            out["balance_set"] = {k: v for k, v in result.items() if k != "transaction"} | {
                "transaction_id": (result.get("transaction") or {}).get("id")}
        return out

    @app.post("/api/accounts/{aid}/set-balance")
    def set_balance(aid: int, body: dict = Body(...)):
        if body.get("balance") in (None, ""):
            raise HTTPException(400, "balance is required (for credit cards/loans/payables: the amount owed)")
        if not db.one("SELECT 1 x FROM accounts WHERE id=?", (aid,)):
            raise HTTPException(404, "Account not found")
        try:
            r = ledger.set_balance(db, aid, body["balance"], body.get("date"), body.get("note"),
                                   as_owed=body.get("as_owed"), reason=body.get("reason"))
            if body.get("reconcile"):
                rc = reconcile.create(db, aid, body["balance"], r["date"], body.get("note") or "", as_owed=body.get("as_owed"))
                r["reconciliation"] = rc["reconciliation"]
                r["message"] += f" Marked as reconciled 已对账 on {r['date'][:10]}."
            return r
        except ledger.ValidationError as e:
            raise HTTPException(400, str(e))
        except reconcile.ReconcileError as e:
            raise HTTPException(400, str(e))

    def _rec_err(fn):
        try:
            return fn()
        except reconcile.ReconcileError as e:
            raise HTTPException(400, str(e))
        except KeyError:
            raise HTTPException(404, "Account not found")

    @app.get("/api/accounts/{aid}/reconcile")
    def reconcile_preview(aid: int, as_of: str | None = None, balance: str | None = None, as_owed: bool | None = None):
        """Preview for the Reconcile dialog (read-only)."""
        return _rec_err(lambda: reconcile.preview(db, aid, as_of, balance, as_owed))

    @app.post("/api/accounts/{aid}/reconcile")
    def reconcile_create(aid: int, body: dict = Body(...)):
        if body.get("balance") in (None, ""):
            raise HTTPException(400, "balance is required (statement balance; for credit cards/loans the amount owed)")
        return _rec_err(lambda: reconcile.create(db, aid, body["balance"], body.get("as_of"), body.get("note") or "",
                                                 body.get("cleared_ids"), body.get("adjust_reason"), body.get("as_owed")))

    @app.get("/api/accounts/{aid}/reconciliations")
    def reconcile_history(aid: int):
        return _rec_err(lambda: {"items": reconcile.history(db, aid)})

    @app.delete("/api/accounts/{aid}/reconciliations/latest")
    def reconcile_undo(aid: int, delete_adjustment: bool = False):
        return _rec_err(lambda: reconcile.undo_latest(db, aid, delete_adjustment))

    @app.delete("/api/accounts/{aid}")
    def delete_account(aid: int, force: bool = False):
        ln = db.one("SELECT name, counterparty FROM loans WHERE account_id=? LIMIT 1", (aid,))
        if ln:
            raise HTTPException(400, f"This account belongs to the loan '{ln['name'] or ln['counterparty']}': delete the loan in the Loan Center first.")
        n = db.one("SELECT COUNT(*) n FROM transactions WHERE account_id=? OR to_account_id=?", (aid, aid))["n"]
        if n and not force:
            raise HTTPException(400, f"Account has {n} transactions. Archive it instead, or delete with force=true (deletes them too).")
        with db.tx() as con:
            con.execute("DELETE FROM transactions WHERE account_id=? OR to_account_id=?", (aid, aid))
            con.execute("DELETE FROM accounts WHERE id=?", (aid,))
        return {"ok": True, "deleted_transactions": n}

    @app.post("/api/accounts/{aid}/adjust")  # older name, kept for compatibility
    def adjust_balance(aid: int, body: dict = Body(...)):
        if not db.one("SELECT 1 x FROM accounts WHERE id=?", (aid,)):
            raise HTTPException(404)
        try:
            r = ledger.set_balance(db, aid, body["target_balance"], body.get("date"), body.get("note"),
                                   as_owed=bool(body.get("as_owed")), reason=body.get("reason"))
        except ledger.ValidationError as e:
            raise HTTPException(400, str(e))
        return r["transaction"] if r["created"] else {"ok": True, "message": r["message"]}

    # groups
    @app.post("/api/account-groups")
    def add_group(body: dict = Body(...)):
        with db.tx() as con:
            gid = con.execute("INSERT INTO account_groups(name,sort) VALUES(?,?)", (body["name"].strip(), int(body.get("sort") or 50))).lastrowid
        return db.one("SELECT * FROM account_groups WHERE id=?", (gid,))

    @app.put("/api/account-groups/{gid}")
    def upd_group(gid: int, body: dict = Body(...)):
        with db.tx() as con:
            if "name" in body:
                con.execute("UPDATE account_groups SET name=? WHERE id=?", (body["name"].strip(), gid))
            if "sort" in body:
                con.execute("UPDATE account_groups SET sort=? WHERE id=?", (int(body["sort"]), gid))
        return db.one("SELECT * FROM account_groups WHERE id=?", (gid,))

    @app.delete("/api/account-groups/{gid}")
    def del_group(gid: int):
        if db.one("SELECT 1 x FROM accounts WHERE group_id=?", (gid,)):
            raise HTTPException(400, "Group still has accounts")
        with db.tx() as con:
            con.execute("DELETE FROM account_groups WHERE id=?", (gid,))
        return {"ok": True}

    # ------------------------------------------------------------ transactions
    @app.get("/api/transactions")
    def list_tx(request: Request):
        """Newest first. Cursor pagination: ?limit=50 then ?before=<next_cursor>. The first page also has total + summary."""
        return ledger.page_tx(db, dict(request.query_params))

    @app.get("/api/summary/month")
    def summary_month(month: str | None = None, ledger_id: int | None = None):
        try:
            return ledger.month_summary(db, month, ledger_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/accounts/{aid}/history")
    def account_history(aid: int, request: Request):
        """Account detail: ?period=1M|3M|6M|1Y|YTD|ALL (default 1M) or start/end, q, category_id, before, limit."""
        try:
            return ledger.account_history(db, aid, dict(request.query_params))
        except KeyError:
            raise HTTPException(404, "Account not found")

    @app.get("/api/accounts/{aid}/investment-series")
    def investment_series(aid: int, request: Request):
        """Principal vs returns vs value over time for an investment account.
        ?period=1M|3M|6M|YTD|1Y|3Y|ALL or from=YYYY-MM-DD&to=YYYY-MM-DD, interval=auto|day|week|month."""
        q = request.query_params
        a = next((x for x in ledger.accounts_with_balances(db) if x["id"] == aid), None)
        if not a:
            raise HTTPException(404, "Account not found")
        if not a["is_investment"]:
            raise HTTPException(400, "Not an investment account")
        start, end = q.get("from") or q.get("start"), q.get("to") or q.get("end")
        if not (start or end):
            per = (q.get("period") or "1Y").upper()
            if per not in ("1M", "3M", "6M", "1Y", "3Y", "YTD", "ALL"):
                raise HTTPException(400, "period must be 1M, 3M, 6M, YTD, 1Y, 3Y or ALL")
            start = ledger.period_start(per)
        try:
            return principal.investment_series(db, a, start, end, q.get("interval") or "auto")
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------ reimbursements 报销
    @app.get("/api/reimbursements")
    def reimb_list(request: Request):
        """Reimbursable expenses: ?status=pending,partial (default) | settled | written_off | all, start, end, payer, account_id."""
        try:
            return reimburse.list_items(db, dict(request.query_params))
        except (ValueError, ledger.ValidationError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/reimbursements/receipts")
    def reimb_receipts(limit: int = 100, unallocated: bool = False):
        """Reimbursements (newest first) with allocated / unallocated and links. ?unallocated=1: only those with money left."""
        return reimburse.receipts(db, limit, unallocated_only=unallocated)

    @app.get("/api/reimbursements/receipts/{rid}")
    def reimb_receipt(rid: int):
        try:
            return reimburse.receipt(db, rid)
        except KeyError:
            raise HTTPException(404, "Reimbursement not found")

    @app.get("/api/reimbursements/links")
    def reimb_links(expense_id: int | None = None, reimbursement_id: int | None = None):
        """Links between reimbursable expenses and reimbursements (filter by either side)."""
        return reimburse.list_links(db, expense_id, reimbursement_id)

    @app.post("/api/reimbursements/links")
    def reimb_link(body: dict = Body(...)):
        """Link (all or nothing): {expense_id, reimbursement_id, amount?, alloc_amount?} or
        {reimbursement_id, items: [{expense_id, amount?}]} or {expense_id, items: [{reimbursement_id, amount?}]}.
        amount = part of the expense (its currency), default = min(outstanding, unallocated); alloc_amount = part of the
        reimbursement (its currency; only needed across currencies)."""
        try:
            return reimburse.link(db, body or {})
        except (ValueError, ledger.ValidationError) as e:
            raise HTTPException(400, str(e))

    @app.put("/api/reimbursements/links/{lid}")
    def reimb_link_update(lid: int, body: dict = Body(...)):
        """{amount?, alloc_amount?}"""
        try:
            return reimburse.update_link(db, lid, body or {})
        except KeyError:
            raise HTTPException(404, "Link not found")
        except (ValueError, ledger.ValidationError) as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/reimbursements/links/{lid}")
    def reimb_unlink(lid: int):
        try:
            return reimburse.unlink(db, lid)
        except KeyError:
            raise HTTPException(404, "Link not found")

    @app.get("/api/reimbursements/suggest")
    def reimb_suggest(reimbursement_id: int | None = None, expense_id: int | None = None, limit: int = 50):
        """Link candidates, best match first: open expenses for a reimbursement, or reimbursements with unallocated
        money for an expense. Each has score, match (amount/date/payer), default_amount (+ default_alloc)."""
        try:
            return reimburse.suggest(db, reimbursement_id, expense_id, limit)
        except KeyError:
            raise HTTPException(404, "Reimbursement not found")
        except (ValueError, ledger.ValidationError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/reimbursements/receipts")
    def reimb_record(body: dict = Body(...)):
        """{account_id, date?, amount?, currency?, payer?, note?, items: [{expense_id, amount?}]} -> a 'reimbursement'
        transaction (money in) linked to the expenses it settles (partial allowed)."""
        try:
            return reimburse.record_receipt(db, body)
        except (ValueError, ledger.ValidationError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/reimbursements/mark")
    def reimb_mark(body: dict = Body(...)):
        """{ids: [...], reimbursable: true|false, payer?}"""
        try:
            return reimburse.mark(db, body.get("ids") or [], bool(body.get("reimbursable", True)), body.get("payer"))
        except (ValueError, ledger.ValidationError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/reimbursements/{tid}/write-off")
    def reimb_writeoff(tid: int, body: dict = Body(default={})):
        """Won't be reimbursed: the unreimbursed part counts as a normal expense again. {"undo": true} reverses it."""
        try:
            return reimburse.write_off(db, tid, bool((body or {}).get("undo")))
        except ledger.ValidationError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/reimbursements/{tid}/close")
    def reimb_close(tid: int):
        """Close an expense by hand: the outstanding rest is written off and counts as a personal expense."""
        try:
            return reimburse.write_off(db, tid)
        except ledger.ValidationError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/reimbursements/{tid}/reopen")
    def reimb_reopen(tid: int):
        """Undo a manual close (write-off)."""
        try:
            return reimburse.write_off(db, tid, undo=True)
        except ledger.ValidationError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------ reserves 押金/预授权
    @app.exception_handler(reserves.NotFound)
    async def _res_nf(_req, exc):
        return JSONResponse({"detail": "Reserve not found"}, status_code=404)

    @app.get("/api/reserves")
    def reserve_list(request: Request):
        """Reserves (card holds / refundable deposits): ?state=open,partial (default) | settled | released | cancelled |
        all, method=hold|deposit, account_id, project_id, review=1. Also totals on_hold / deposits (by currency)."""
        return reserves.list_reserves(db, dict(request.query_params))

    @app.post("/api/reserves")
    def reserve_create(body: dict = Body(...)):
        """{method: hold|deposit, account_id, amount, currency?, merchant?, title?, start_date (check-in), end_date
        (check-out)?, category_id?, project_id?, member_id?, ref?, note?, remind?, paid_amount?, deposit_tx_id?}.
        A hold is memo only; a deposit also posts the transfer account → Deposits 押金."""
        body = dict(body or {}); body.pop("is_demo", None)
        return reserves.create(db, body)

    @app.get("/api/reserves/export.csv")
    def reserve_export():
        import csv, io
        rows = reserves.export_rows(db)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["id", "method", "title", "merchant", "account", "amount", "currency", "check_in",
                                            "check_out", "state", "outstanding", "links"])
        w.writeheader(); w.writerows(rows)
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="reserves-{date.today()}.csv"'})

    @app.get("/api/reserves/suggest")
    def reserve_suggest(reserve_id: int | None = None, transaction_id: int | None = None, limit: int = 50):
        """Candidates to link: ?reserve_id= (transactions) or ?transaction_id= (open reserves)."""
        return reserves.suggest(db, reserve_id, transaction_id, limit)

    @app.get("/api/reserves/{rid}")
    def reserve_get(rid: int):
        return reserves.get(db, rid)

    @app.put("/api/reserves/{rid}")
    def reserve_update(rid: int, body: dict = Body(...)):
        return reserves.update(db, rid, body or {})

    @app.delete("/api/reserves/{rid}")
    def reserve_delete(rid: int, unlink: bool = False):
        """Refused while transactions are linked unless ?unlink=1 (links removed, transactions kept)."""
        return reserves.delete(db, rid, unlink)

    @app.post("/api/reserves/{rid}/links")
    def reserve_link(rid: int, body: dict = Body(...)):
        """{transaction_id, role?, amount?, alloc_amount?, final?} or {items: [...]} (all or nothing)."""
        return reserves.link(db, rid, body or {})

    @app.delete("/api/reserves/links/{lid}")
    def reserve_unlink(lid: int):
        return reserves.unlink(db, lid)

    @app.post("/api/reserves/{rid}/settle")
    def reserve_settle(rid: int, body: dict = Body(...)):
        """Check-out in one step (dry_run: true previews). Hold: {final: {amount | transaction_id}} or {release: true};
        deposit: {refund: {account_id, amount?, received?}, apply: {amount}, forfeit: {amount}, charge: {amount}}."""
        return reserves.settle(db, rid, body or {})

    for _how in ("release", "close", "cancel", "reopen"):
        def _mk(how):
            def _act(rid: int):
                return reserves.close(db, rid, how)
            _act.__name__ = f"reserve_{how}"
            return _act
        app.post(f"/api/reserves/{{rid}}/{_how}", name=f"reserve_{_how}")(_mk(_how))

    @app.post("/api/reserves/{rid}/confirm")
    def reserve_confirm(rid: int):
        """A hold detected from an email is right: it leaves the review queue."""
        return reserves.confirm(db, rid)

    @app.post("/api/reserves/{rid}/to-transaction")
    def reserve_to_tx(rid: int):
        """'It's a real charge': the detected hold becomes an expense in the review queue."""
        return reserves.to_transaction(db, rid)

    @app.post("/api/review/{tid}/to-reserve")
    def review_to_reserve(tid: int, body: dict = Body(default={})):
        """Convert a To-review item into a reserve ({method: hold|deposit, end_date?, …})."""
        return reserves.from_review(db, tid, body or {})

    # ------------------------------------------------------------ Loan Center 借贷中心
    @app.exception_handler(loans.NotFound)
    async def _loan_nf(_req, exc):
        return JSONResponse({"detail": "Loan not found"}, status_code=404)

    @app.get("/api/loans")
    def loan_list(request: Request):
        """Loans lent / borrowed with totals. ?state=active (default) | paid_off | closed | all, ?direction=lent|borrowed."""
        return loans.list_loans(db, dict(request.query_params))

    @app.post("/api/loans")
    def loan_create(body: dict = Body(...)):
        """{direction: lent|borrowed, counterparty, principal, pay_account_id, account_id? (existing account: tracked
        from its balance), disburse?, rate?, freq?, first_due?, term?, payment?, escrow?, maturity_date?, …}."""
        return loans.create(db, body or {})

    @app.get("/api/loans/export.csv")
    def loan_export():
        import csv, io
        rows = loans.export_rows(db)
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="loans-{date.today()}.csv"'})

    @app.get("/api/loans/suggest")
    def loan_suggest(transaction_id: int | None = None, loan_id: int | None = None, limit: int = 30):
        """?transaction_id= → loans it could be a repayment of; ?loan_id= → transactions that look like its repayments."""
        return loans.suggest(db, transaction_id, loan_id, max(1, min(limit, 200)))

    @app.get("/api/loans/{lid}")
    def loan_get(lid: int):
        return loans.get(db, lid)

    @app.put("/api/loans/{lid}")
    def loan_update(lid: int, body: dict = Body(...)):
        return loans.update(db, lid, body or {})

    @app.delete("/api/loans/{lid}")
    def loan_delete(lid: int, unlink: bool = False):
        return loans.delete(db, lid, unlink)

    @app.get("/api/loans/{lid}/split")
    def loan_split(lid: int, amount: float | None = None, date_: str | None = Query(None, alias="date"), extra: bool = False):
        """Default principal / interest / escrow split of a repayment of `amount` (blank = the next expected payment)."""
        return loans.preview_split(db, lid, amount, date_, extra)

    @app.post("/api/loans/{lid}/payments")
    def loan_pay(lid: int, body: dict = Body(...)):
        """Record a repayment split into linked transactions ({amount?, principal?, interest?, escrow?, fee?, date?,
        account_id?, due_date?, extra?, transaction_id? (convert that transaction in place), dry_run?})."""
        return loans.record_payment(db, lid, body or {})

    @app.post("/api/loans/{lid}/disburse")
    def loan_disburse(lid: int, body: dict = Body(...)):
        return loans.disburse(db, lid, body or {})

    @app.post("/api/loans/{lid}/links")
    def loan_link(lid: int, body: dict = Body(...)):
        """Link existing transactions as one repayment: {items: [{transaction_id, role?}], due_date?, extra?}."""
        return loans.link(db, lid, body or {})

    @app.delete("/api/loans/links/{link_id}")
    def loan_unlink(link_id: int):
        return loans.unlink(db, link_id)

    @app.delete("/api/loans/events/{eid}")
    def loan_event_delete(eid: int, delete_transactions: bool = False):
        """Undo a repayment: unlink its transactions (default) or delete them too."""
        return loans.delete_event(db, eid, delete_transactions)

    @app.post("/api/loans/{lid}/close")
    def loan_close(lid: int, body: dict = Body(default={})):
        """{action: close|reopen, write_off?, category_id?, date?, note?}."""
        return loans.close(db, lid, body or {})

    @app.post("/api/loans/{lid}/schedule")
    def loan_schedule(lid: int, body: dict = Body(default={})):
        """Recurring schedule for the repayments: {post_mode: post|remind, start_date?, time_of_day?, lead_days?,
        adopt_schedule_id? (an existing schedule for this payment, paused and replaced)}."""
        return loans.setup_schedule(db, lid, body or {})

    @app.delete("/api/loans/{lid}/schedule")
    def loan_schedule_delete(lid: int):
        return loans.remove_schedule(db, lid)

    @app.post("/api/transactions/{tid}/to-loan-payment")
    def tx_to_loan(tid: int, body: dict = Body(...)):
        """Turn a transaction into a loan repayment in place ({loan_id, principal?, interest?, …}); the other parts are added."""
        if not (body or {}).get("loan_id"):
            raise HTTPException(400, "loan_id is required")
        return loans.record_payment(db, int(body["loan_id"]), {**body, "transaction_id": tid})

    @app.post("/api/transactions/{tid}/to-reserve")
    def tx_to_reserve(tid: int, body: dict = Body(default={})):
        """Turn a posted expense into a reserve in place ({method: hold|deposit, end_date?, …}): a hold removes the
        expense; a deposit turns it into the transfer to Deposits 押金 and links it."""
        return reserves.from_transaction(db, tid, body or {})

    @app.get("/api/transactions/export.csv")
    def export_tx(request: Request):
        f = dict(request.query_params); f["limit"] = 100000; f["with_links"] = "1"
        rows, _ = ledger.query_tx(db, f)
        return Response(importers.export_csv(rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="transactions-{date.today()}.csv"'})

    @app.post("/api/transactions")
    def add_tx(request: Request, body: dict = Body(...)):
        body.setdefault("source", "web" if getattr(request.state, "via", "") == "session" else "api")
        remember = body.pop("remember", False)
        receipt_ids = [int(i) for i in (body.pop("receipt_ids", None) or [])]
        probe = {"date": str(body.get("date") or "9999").replace("T", " "), "account_id": body.get("account_id"),
                 "to_account_id": body.get("to_account_id"), "kind": body.get("kind")}
        warning = reconcile.edit_warning(db, _recon_points(probe)) if body.get("date") else None
        _recon_guard(warning, body.pop("confirm_reconciled", None))
        tx = ledger.create_tx(db, body)
        if receipt_ids:
            tx["receipts"] = receipts.link(db, receipt_ids, tx["id"])
        if warning:
            tx["reconcile_warning"] = warning
        if remember:  # the form asks the server to remember last-used account/category/currency/member per type
            try:
                ledger.remember_tx(db, {"kind": tx["kind"], "account_id": tx.get("account_id"),
                                        "to_account_id": tx.get("to_account_id") if tx["kind"] == "transfer" else None,
                                        "category_id": tx.get("category_id"), "member_id": tx.get("member_id"),
                                        "currency": tx.get("orig_currency") or tx.get("currency")})
            except ledger.ValidationError:
                pass
        return tx

    @app.get("/api/transactions/{tid}")
    def one_tx(tid: int):
        t = ledger.get_tx(db, tid)
        if not t:
            raise HTTPException(404)
        return t

    def _moves_balance(old: dict, body: dict) -> bool:
        """Does this edit change anything that affects balances? (note/tag/category/merchant edits don't)."""
        def num(v):
            try:
                return round(float(v), 6) if v not in (None, "") else None
            except (TypeError, ValueError):
                return v
        foreign = body.get("currency") and old.get("orig_currency") and body["currency"] == old["orig_currency"]
        for k in ("date", "account_id", "to_account_id", "kind", "amount", "currency", "to_amount", "status", "shares", "price", "fee"):
            if k not in body:
                continue
            nv, ov = body[k], old.get(k)
            if k == "date":
                if str(nv or "").replace("T", " ")[:16] != str(ov or "")[:16]:
                    return True
            elif k == "currency":
                if nv and nv not in (old.get("currency"), old.get("orig_currency")):
                    return True
            elif k == "amount" and foreign:
                if num(nv) != num(old.get("orig_amount")):
                    return True
            elif k == "to_account_id" and (body.get("kind") or old.get("kind")) != "transfer":
                continue
            elif k == "kind" or k == "status":
                if nv != ov:
                    return True
            elif num(nv) != num(ov):
                return True
        return False

    def _recon_points(*txs) -> list:
        pts = []
        for t in txs:
            if t:
                pts += [(t.get("date"), t.get("account_id")), (t.get("date"), t.get("to_account_id") if t.get("kind") == "transfer" else None)]
        return pts

    def _recon_guard(warning: str | None, confirmed) -> None:
        """Optional lock (setting lock_reconciled=1): changes before a checkpoint need confirm_reconciled=true."""
        if warning and db.get_setting("lock_reconciled", "0") == "1" and str(confirmed).lower() not in ("1", "true", "yes"):
            raise HTTPException(409, "Reconciled 已对账: " + warning + ". Confirm to change it anyway.")

    @app.put("/api/transactions/{tid}")
    def put_tx(tid: int, body: dict = Body(...)):
        old = db.one("SELECT * FROM transactions WHERE id=?", (tid,))
        if not old:
            raise HTTPException(404)
        new = {**old, **{k: v for k, v in body.items() if k in ("date", "account_id", "to_account_id", "kind")}}
        if "date" in body:
            new["date"] = str(body["date"]).replace("T", " ")
        touches = _moves_balance(old, body)
        warning = reconcile.edit_warning(db, _recon_points(old, new)) if touches else None  # note/tag/category edits don't move balances
        _recon_guard(warning, body.pop("confirm_reconciled", None))
        try:
            t = ledger.update_tx(db, tid, body)
        except KeyError:
            raise HTTPException(404)
        receipts.relocate_for_tx(db, tid)  # receipt files follow the date/merchant/amount
        if warning:
            t["reconcile_warning"] = warning
        return t

    @app.delete("/api/transactions/{tid}")
    def del_tx(tid: int, confirm_reconciled: bool = False, keep_receipts: bool = False):
        old = db.one("SELECT * FROM transactions WHERE id=?", (tid,))
        warning = reconcile.edit_warning(db, _recon_points(old))
        _recon_guard(warning, confirm_reconciled)
        t = db.one("SELECT attachment FROM transactions WHERE id=?", (tid,))
        with db.tx() as con:
            con.execute("DELETE FROM transactions WHERE id=?", (tid,))
        if t and t.get("attachment"):
            (data_dir / "attachments" / t["attachment"]).unlink(missing_ok=True)
        receipts.on_tx_delete(db, [tid], keep=keep_receipts)
        return {"ok": True, **({"reconcile_warning": warning} if warning else {})}

    @app.post("/api/transactions/bulk-delete")
    def bulk_delete(body: dict = Body(...)):
        ids = [int(i) for i in body.get("ids") or []]
        if not ids:
            return {"deleted": 0}
        with db.tx() as con:
            n = con.execute(f"DELETE FROM transactions WHERE id IN ({','.join('?' * len(ids))})", ids).rowcount
        receipts.on_tx_delete(db, ids, keep=bool(body.get("keep_receipts")))
        return {"deleted": n}

    # ------------------------------------------------------------ Reminder Center 提醒中心
    def _rem(fn):
        try:
            return fn()
        except KeyError:
            raise HTTPException(404, "Reminder not found")
        except (reminders.ReminderError, ledger.ValidationError, ValueError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/reminders")
    def list_reminders(view: str = "open"):
        return _rem(lambda: reminders.listing(db, view))

    @app.get("/api/reminders/notify")
    def reminders_notify(days: int = 7, include_overdue: bool = True, lang: str = "auto"):
        """For iPhone Shortcuts: {count, title, message, items}."""
        return reminders.digest(db, max(0, min(days, 366)), include_overdue, lang)

    @app.post("/api/reminders/run")
    def run_reminders():
        return reminders.run(db, notify=False)

    @app.get("/api/reminders/{rid}")
    def one_reminder(rid: int):
        return _rem(lambda: reminders.get(db, rid))

    @app.put("/api/reminders/{rid}")
    def edit_reminder(rid: int, body: dict = Body(...)):
        return _rem(lambda: reminders.edit(db, rid, body))

    @app.post("/api/reminders/{rid}/done")
    def done_reminder(rid: int):
        return _rem(lambda: reminders.mark_done(db, rid))

    @app.post("/api/reminders/{rid}/paid")
    def paid_reminder(rid: int, body: dict = Body(default={})):
        return _rem(lambda: reminders.mark_paid(db, rid, body or {}))

    @app.post("/api/reminders/{rid}/dismiss")
    def dismiss_reminder(rid: int):
        return _rem(lambda: reminders.dismiss(db, rid))

    @app.post("/api/reminders/{rid}/schedules/{sid}/confirm")
    def confirm_reminder_schedule(rid: int, sid: int, body: dict = Body(default={})):
        """Card expiry: this scheduled payment now uses the new card (or nothing to change)."""
        return _rem(lambda: reminders.confirm_schedule(db, rid, sid, bool((body or {}).get("undo"))))

    @app.post("/api/reminders/{rid}/schedules/{sid}/reassign")
    def reassign_reminder_schedule(rid: int, sid: int, body: dict = Body(...)):
        """Card expiry: move this scheduled payment to another account."""
        return _rem(lambda: reminders.reassign_schedule(db, rid, sid, (body or {}).get("account_id")))

    @app.post("/api/reminders/{rid}/card")
    def confirm_reminder_card(rid: int, body: dict = Body(default={})):
        """Card expiry: {action: renew, expiry: 'MM/YY'} | {action: acknowledge} | {action: undo}."""
        return _rem(lambda: reminders.confirm_card(db, rid, body or {}))

    @app.post("/api/reminders/{rid}/reopen")
    def reopen_reminder(rid: int):
        return _rem(lambda: reminders.reopen(db, rid))

    @app.post("/api/reminders/{rid}/snooze")
    def snooze_reminder(rid: int, body: dict = Body(default={})):
        return _rem(lambda: reminders.snooze(db, rid, (body or {}).get("days"), (body or {}).get("until")))

    # ------------------------------------------------------------ receipt photos 小票 (local OCR)
    @app.get("/api/receipts/status")
    def receipts_status():
        return {"ocr": receipts.ocr_status(), "folder": str(receipts.base_dir(db)),
                "pattern": db.get_setting("receipts_pattern") or receipts.DEFAULT_PATTERN,
                "max_mb": receipts.MAX_BYTES // 1024 // 1024, "max_files": receipts.MAX_FILES}

    def _rc_err(code: int, msg: str):
        log.warning("receipt upload rejected (%s): %s", code, msg)
        return JSONResponse({"ok": False, "detail": msg, "message": "Receipt not saved: " + msg}, status_code=code)

    @app.post("/api/agent/transactions")
    async def agent_transaction(request: Request):
        """For AI agents / scripts: JSON (optional base64 'receipt') or multipart (fields + a file). Names instead of
        ids; optional tax/tip ("2.50" or "18%"). Returns {ok, id, message, transaction, warnings}; errors {detail, message}."""
        ctype = (request.headers.get("content-type") or "").lower()
        files: list = []
        try:
            if "multipart/" in ctype or "form-urlencoded" in ctype:
                form = await request.form()
                body: dict = {}
                for k, v in form.multi_items():
                    if hasattr(v, "read"):
                        data = await v.read()
                        if data:
                            files.append((data, getattr(v, "filename", "") or ""))
                    elif k in ("receipt_ids",):
                        body.setdefault(k, []).extend(int(x) for x in str(v).split(",") if x.strip().isdigit())
                    else:
                        body[k] = v
            else:
                raw = await request.body()
                body = json.loads(raw.decode("utf-8-sig") or "{}") if raw.strip() else {}
                if not isinstance(body, dict):
                    raise agent_api.AgentError("Send a JSON object")
            out = agent_api.create(db, body, files)
        except (ValueError, agent_api.AgentError, ledger.ValidationError, receipts.ReceiptError) as e:
            msg = str(e) if not isinstance(e, json.JSONDecodeError) else "Body is not valid JSON"
            return JSONResponse({"ok": False, "detail": msg, "message": msg}, status_code=400)
        except KeyError as e:
            return JSONResponse({"ok": False, "detail": f"Not found: {e}", "message": f"Not found: {e}"}, status_code=400)
        return out

    @app.post("/api/receipts")
    async def upload_receipts(request: Request, auto_post: bool = False, account_id: int | None = None,
                              ledger_id: int | None = None):
        """Multipart (file / files fields) or JSON {image|images: base64, filename?, auto_post?, account_id?}.
        auto_post=false (default): returns a draft for the form; nothing is recorded. auto_post=true: records the
        transaction (posted if confident, else To review) with the image(s) attached."""
        ctype = request.headers.get("content-type", "")
        raw = await request.body()
        if len(raw) > receipts.MAX_BYTES * receipts.MAX_FILES + 1024 * 1024:
            return _rc_err(413, f"Upload too large (max {receipts.MAX_BYTES // 1024 // 1024} MB per image)")
        try:
            items, params = receipts.extract_upload(raw, ctype)
        except receipts.ReceiptError as e:
            return _rc_err(400, str(e))
        truthy = lambda v: str(v or "").lower() in ("1", "true", "yes", "on")
        auto_post = auto_post or truthy(params.get("auto_post"))
        account_id = account_id or (int(params["account_id"]) if str(params.get("account_id") or "").isdigit() else None)
        ledger_id = ledger_id or (int(params["ledger_id"]) if str(params.get("ledger_id") or "").isdigit() else None)
        log.info("receipt upload: %d bytes, content-type %r, %d image(s), auto_post=%s", len(raw), ctype[:60], len(items), auto_post)
        if not items:
            return _rc_err(400, "No image received. Send the photo as the request body or as a form file (any field name).")
        if len(items) > receipts.MAX_FILES:
            return _rc_err(400, f"Too many images (max {receipts.MAX_FILES})")
        receipts.cleanup_drafts(db)
        saved = []
        try:
            for data, name in items:
                saved.append(receipts.save_upload(db, data, name))
        except receipts.ReceiptError as e:
            for r in saved:
                receipts.delete(db, r["id"])
            return _rc_err(400, str(e))
        except Exception as e:  # noqa: BLE001 - always answer Shortcuts with a readable message
            log.exception("receipt upload failed")
            for r in saved:
                receipts.delete(db, r["id"])
            return _rc_err(500, f"Receipt processing failed: {e}")
        # several photos of one receipt: the first with a readable total wins, the rest fill gaps
        parsed = {}
        for r in saved:
            for k, v in (r["parsed"] or {}).items():
                if parsed.get(k) in (None, [], "") and v not in (None, [], ""):
                    parsed[k] = v
        parsed.setdefault("confidence", 0.0)
        out = {"receipts": [{k: r[k] for k in ("id", "url", "thumb_url", "rel_path", "size", "ocr_confidence")} for r in saved],
               "receipt_ids": [r["id"] for r in saved], "ocr": receipts.ocr_status(), "parsed": parsed,
               "ocr_text": "\n---\n".join(r["ocr_text"] or "" for r in saved)[:6000]}
        out.update(ingest.receipt_draft(db, parsed, account_id))
        if auto_post:
            th = float(db.get_setting("review_threshold", "0.75"))
            d = dict(out["draft"])
            ok = out["confidence"] >= th and d.get("amount") and d.get("account_id")
            d.update(status="posted" if ok else "review", source="receipt", confidence=out["confidence"],
                     raw_text=out["ocr_text"][:4000], ledger_id=int(ledger_id or db.get_setting("default_ledger_id", "1") or 1))
            if not d.get("date"):
                d.pop("date")
            try:
                tx = ledger.create_tx(db, d)
            except ledger.ValidationError:
                d["status"] = "review"
                tx = ledger.create_tx(db, {**d, "amount": d.get("amount") or 0})
            receipts.link(db, out["receipt_ids"], tx["id"])
            out["transaction"] = ledger.get_tx(db, tx["id"])
            out["status"] = out["transaction"]["status"]
        else:
            out["status"] = "draft"
        zh = ingest.msg_zh()
        out["message"] = ingest.receipt_message(out, zh)
        if not out["ocr"].get("available"):
            out["message"] += " · OCR off on the server: " + out["ocr"].get("message", "")[:120]
        return out

    @app.get("/api/receipts/{rid}")
    def receipt_info(rid: int):
        r = receipts.get(db, rid)
        if not r:
            raise HTTPException(404, "Receipt not found")
        return r

    @app.get("/api/receipts/{rid}/image")
    def receipt_image(rid: int, thumb: bool = False):
        r = db.one("SELECT rel_path, mime FROM receipts WHERE id=?", (rid,))
        if not r or not r["rel_path"]:
            raise HTTPException(404, "Receipt not found")
        p = receipts.abs_path(db, r["rel_path"])
        if not p.exists():
            raise HTTPException(404, "Receipt file missing (receipts folder moved?)")
        if thumb:
            return Response(receipts.thumbnail(p), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
        return FileResponse(p, media_type=r["mime"] or "image/jpeg", filename=p.name)

    @app.post("/api/receipts/{rid}/link")
    def receipt_link(rid: int, body: dict = Body(...)):
        try:
            return receipts.link(db, [rid], int(body.get("transaction_id") or 0))
        except receipts.ReceiptError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/receipts/{rid}")
    def receipt_delete(rid: int):
        receipts.delete(db, rid)
        return {"ok": True}

    @app.get("/api/transactions/{tid}/receipts")
    def tx_receipts(tid: int):
        return receipts.for_tx(db, tid)

    @app.post("/api/transactions/{tid}/approve")
    def approve(tid: int, body: dict = Body(default={})):
        body = dict(body or {}); body["status"] = "posted"
        try:
            t = ledger.update_tx(db, tid, body)
        except KeyError:
            raise HTTPException(404)
        receipts.relocate_for_tx(db, tid)
        return t

    @app.post("/api/transactions/{tid}/attachment")
    async def upload_attachment(tid: int, file: UploadFile = File(...)):
        if not db.one("SELECT 1 x FROM transactions WHERE id=?", (tid,)):
            raise HTTPException(404)
        safe = re.sub(r"[^\w.\-]+", "_", file.filename or "file")[-80:]
        name = f"{tid}_{secrets.token_hex(4)}_{safe}"
        content = await file.read()
        if len(content) > 15 * 1024 * 1024:
            raise HTTPException(400, "Max 15 MB")
        (data_dir / "attachments" / name).write_bytes(content)
        with db.tx() as con:
            con.execute("UPDATE transactions SET attachment=? WHERE id=?", (name, tid))
        return ledger.get_tx(db, tid)

    @app.get("/api/transactions/{tid}/attachment")
    def get_attachment(tid: int):
        t = db.one("SELECT attachment FROM transactions WHERE id=?", (tid,))
        if not t or not t["attachment"]:
            raise HTTPException(404)
        return FileResponse(data_dir / "attachments" / t["attachment"], filename=t["attachment"].split("_", 2)[-1])

    @app.delete("/api/transactions/{tid}/attachment")
    def del_attachment(tid: int):
        t = db.one("SELECT attachment FROM transactions WHERE id=?", (tid,))
        if t and t["attachment"]:
            (data_dir / "attachments" / t["attachment"]).unlink(missing_ok=True)
            with db.tx() as con:
                con.execute("UPDATE transactions SET attachment=NULL WHERE id=?", (tid,))
        return {"ok": True}

    # ------------------------------------------------------------ quick add + email ingest
    @app.post("/api/quick-add")
    async def quick_add(request: Request):
        """Body: JSON {"text": "...", "dry_run": false, "review": false} (Shortcuts "Get Contents of URL" with a JSON body),
        a form, or plain text. Every response has a short 'message' (Siri-friendly); errors are JSON {detail, message}."""
        raw = await request.body()
        ctype = (request.headers.get("content-type") or "").lower()
        body: Any = None
        if "json" in ctype or raw[:1] in (b"{", b'"'):
            try:
                body = json.loads(raw.decode("utf-8-sig") or "null")
            except (ValueError, UnicodeDecodeError):
                body = None  # not JSON after all: treat as plain text below
        if body is None and "form" in ctype:
            body = dict(await request.form())
        if isinstance(body, str):
            body = {"text": body}
        if not isinstance(body, dict):
            body = {"text": raw.decode("utf-8-sig", errors="replace")}
        lower = {str(k).lower(): v for k, v in body.items()}
        text = str(lower.get("text") or "").strip()
        if len(text) > 1 and text[0] == text[-1] == '"' and text.count('"') == 2:  # Shortcuts sometimes wrap the text in quotes
            text = text[1:-1].strip()

        def flag(name):
            v = lower.get(name)
            return v if isinstance(v, bool) else str(v or "").strip().lower() in ("1", "true", "yes", "on")

        if not text:
            return JSONResponse({"detail": "text is required", "ok": False,
                                 "message": "Nothing to add: send text like 'coffee 4.50 cash'"}, status_code=400)
        default_src = "web" if getattr(request.state, "via", "") == "session" else "api"
        src = lower.get("source") or default_src
        if src not in ("web", "api", "email", "bot", "message"):
            src = "api"
        opts = {"ledger_id": lower.get("ledger_id"), "dry_run": flag("dry_run"), "review": flag("review")}
        try:
            return _qa(text, src, opts)
        except (ingest.QuickAddError, ledger.ValidationError) as e:
            return JSONResponse({"detail": str(e), "ok": False, "message": ingest.error_message(str(e), text)}, status_code=400)

    def _qa(text, src, body):
        lid = body.get("ledger_id")
        try:
            lid = int(lid) if lid not in (None, "") else None
        except (TypeError, ValueError):
            lid = None
        return ingest.quick_add(db, text, source=src, ledger_id=lid,
                                dry_run=bool(body.get("dry_run")), force_review=bool(body.get("review")))

    @app.post("/api/ingest/email")
    def ingest_email(body: dict = Body(...)):
        for k in ("from", "subject", "body"):
            body.setdefault(k, "")
        if not (body["subject"] or body["body"]):
            raise HTTPException(400, "subject or body required")
        return ingest.ingest_email(db, body, auto_post=bool(body.get("auto_post", False)))

    @app.get("/api/review")
    def review_items():
        rows, total = ledger.query_tx(db, {"status": "review", "limit": 500})
        holds = reserves.list_reserves(db, {"review": "1", "state": "all"})["items"]
        return {"items": rows, "total": total, "holds": holds}

    # ------------------------------------------------------------ categories & simple lists
    @app.get("/api/categories")
    def cats():
        return ledger.categories_tree(db)

    @app.post("/api/categories")
    def add_cat(body: dict = Body(...)):
        kind = body.get("kind") or "expense"
        if kind not in ("expense", "income"):
            raise HTTPException(400, "kind must be expense or income")
        pid = body.get("parent_id") or None
        if pid:
            p = db.one("SELECT * FROM categories WHERE id=?", (pid,))
            if not p or p["parent_id"]:
                raise HTTPException(400, "Parent must be a top-level category (two levels only)")
            kind = p["kind"]
        with db.tx() as con:
            cid = con.execute("INSERT INTO categories(kind,parent_id,name,name_zh,keywords,sort) VALUES(?,?,?,?,?,?)",
                              (kind, pid, body["name"].strip(), body.get("name_zh") or "", body.get("keywords") or "", int(body.get("sort") or 99))).lastrowid
        return db.one("SELECT * FROM categories WHERE id=?", (cid,))

    @app.put("/api/categories/{cid}")
    def upd_cat(cid: int, body: dict = Body(...)):
        d = {k: body[k] for k in ("name", "name_zh", "keywords", "sort") if k in body}
        if d:
            with db.tx() as con:
                con.execute(f"UPDATE categories SET {','.join(k + '=?' for k in d)} WHERE id=?", list(d.values()) + [cid])
        return db.one("SELECT * FROM categories WHERE id=?", (cid,))

    @app.delete("/api/categories/{cid}")
    def del_cat(cid: int):
        if (db.one("SELECT code FROM categories WHERE id=?", (cid,)) or {}).get("code"):
            raise HTTPException(400, "This is a built-in balance-adjustment category and can't be deleted")
        if db.one("SELECT 1 x FROM categories WHERE parent_id=?", (cid,)):
            raise HTTPException(400, "Delete or move its sub-categories first")
        n = db.one("SELECT COUNT(*) n FROM transactions WHERE category_id=?", (cid,))["n"]
        if n:
            raise HTTPException(400, f"Category is used by {n} transactions")
        with db.tx() as con:
            con.execute("DELETE FROM budgets WHERE category_id=?", (cid,))
            con.execute("DELETE FROM categories WHERE id=?", (cid,))
        return {"ok": True}

    # ------------------------------------------------------------ projects 项目
    def _prj(fn):
        try:
            return fn()
        except KeyError:
            raise HTTPException(404, "Project not found")
        except (projects.ProjectError, plan_lines.PlanError) as e:
            raise HTTPException(400, str(e))

    @app.get("/api/projects")
    def list_projects(include_archived: bool = False, ledger_id: int | None = None, summary: bool = True):
        if not summary:
            return db.all("SELECT * FROM projects" + ("" if include_archived else " WHERE COALESCE(archived,0)=0") + " ORDER BY name")
        return projects.listing(db, include_archived, ledger_id)

    @app.post("/api/projects")
    def add_project(body: dict = Body(...)):
        return _prj(lambda: projects.create(db, body))

    @app.post("/api/projects/assign")
    def assign_project(body: dict = Body(...)):
        pid = body.get("project_id")
        return _prj(lambda: projects.assign(db, body.get("ids") or [], int(pid) if pid not in (None, "", 0) else None))

    @app.get("/api/projects/{pid}")
    def project_detail(pid: int, start: str | None = None, end: str | None = None, ledger_id: int | None = None,
                       parent_id: int | None = None):
        return _prj(lambda: projects.detail(db, pid, start, end, ledger_id, parent_id))

    @app.put("/api/projects/{pid}")
    def upd_project(pid: int, body: dict = Body(...)):
        return _prj(lambda: projects.update(db, pid, body))

    @app.get("/api/projects/{pid}/plan")
    def get_plan(pid: int):
        return _prj(lambda: (projects.get(db, pid), {"project_id": pid, "lines": projects.plan_rows(db, pid)})[1])

    @app.put("/api/projects/{pid}/plan")
    def put_plan(pid: int, body: dict = Body(...)):
        return _prj(lambda: projects.set_plan(db, pid, body.get("lines") or [], body.get("replace", True) is not False))

    @app.post("/api/projects/{pid}/plan/copy")
    def copy_plan(pid: int, body: dict = Body(...)):
        return _prj(lambda: projects.copy_plan(db, pid, int(body.get("from_project_id") or 0), body.get("scale") or 1))

    # plan lines 预算明细 (free-named lines mapped to categories and/or explicitly assigned transactions)
    @app.get("/api/plan-templates")
    def plan_templates():
        return {"templates": plan_lines.templates(db)}

    @app.get("/api/projects/{pid}/plan-lines")
    def get_plan_lines(pid: int):
        return _prj(lambda: (projects.get(db, pid), {"project_id": pid, "lines": plan_lines.lines(db, pid)})[1])

    @app.post("/api/projects/{pid}/plan-lines")
    def add_plan_line(pid: int, body: dict = Body(...)):
        return _prj(lambda: plan_lines.create(db, pid, body))

    @app.post("/api/projects/{pid}/plan-lines/bulk")
    def bulk_plan_lines(pid: int, body: dict = Body(...)):
        return _prj(lambda: plan_lines.bulk(db, pid, body.get("lines") or [], bool(body.get("replace"))))

    @app.post("/api/projects/{pid}/plan-lines/reorder")
    def reorder_plan_lines(pid: int, body: dict = Body(...)):
        return _prj(lambda: plan_lines.reorder(db, pid, body.get("ids") or []))

    @app.post("/api/projects/{pid}/plan-lines/template")
    def template_plan_lines(pid: int, body: dict = Body(...)):
        return _prj(lambda: plan_lines.apply_template(db, pid, str(body.get("template") or ""), bool(body.get("replace")),
                                                      body.get("amounts"), body.get("names")))

    @app.post("/api/projects/{pid}/plan-lines/copy")
    def copy_plan_lines(pid: int, body: dict = Body(...)):
        return _prj(lambda: plan_lines.copy_from(db, pid, int(body.get("from_project_id") or 0), body.get("scale") or 1,
                                                 body.get("replace", True) is not False))

    @app.get("/api/projects/{pid}/plan-lines/{line}/transactions")
    def plan_line_txs(pid: int, line: str, start: str | None = None, end: str | None = None, ledger_id: int | None = None):
        def run():
            p = projects.get(db, pid)
            s, e = projects._range(p, start, end)
            return plan_lines.line_transactions(db, pid, line, s, e, ledger_id)
        try:
            return _prj(run)
        except ValueError:
            raise HTTPException(400, "line must be a plan line id or 'unplanned'")

    @app.put("/api/projects/{pid}/plan-lines/{lid}")
    def upd_plan_line(pid: int, lid: int, body: dict = Body(...)):
        return _prj(lambda: plan_lines.update(db, pid, lid, body))

    @app.delete("/api/projects/{pid}/plan-lines/{lid}")
    def del_plan_line(pid: int, lid: int):
        return _prj(lambda: plan_lines.delete(db, pid, lid))

    @app.post("/api/projects/{pid}/archive")
    def archive_project(pid: int, body: dict = Body(default={})):
        return _prj(lambda: projects.update(db, pid, {"archived": 0 if (body or {}).get("undo") else 1}))

    @app.delete("/api/projects/{pid}")
    def del_project(pid: int):
        return _prj(lambda: projects.delete(db, pid))

    for entity, col in (("members", "member_id"), ("merchants", "merchant_id"), ("ledgers", "ledger_id")):
        def _make(entity=entity, col=col):
            @app.get(f"/api/{entity}", name=f"list_{entity}")
            def _list():
                return db.all(f"SELECT * FROM {entity} ORDER BY id")

            @app.post(f"/api/{entity}", name=f"add_{entity}")
            def _add(body: dict = Body(...)):
                name = str(body.get("name") or "").strip()
                if not name:
                    raise HTTPException(400, "name required")
                try:
                    with db.tx() as con:
                        i = con.execute(f"INSERT INTO {entity}(name) VALUES(?)", (name,)).lastrowid
                except Exception:
                    raise HTTPException(400, "Already exists")
                return db.one(f"SELECT * FROM {entity} WHERE id=?", (i,))

            @app.put(f"/api/{entity}/{{iid}}", name=f"upd_{entity}")
            def _upd(iid: int, body: dict = Body(...)):
                d = {k: body[k] for k in ("name", "archived", "note") if k in body}
                if entity != "projects":
                    d.pop("archived", None)
                if entity != "ledgers":
                    d.pop("note", None)
                if d:
                    with db.tx() as con:
                        con.execute(f"UPDATE {entity} SET {','.join(k + '=?' for k in d)} WHERE id=?", list(d.values()) + [iid])
                return db.one(f"SELECT * FROM {entity} WHERE id=?", (iid,))

            @app.delete(f"/api/{entity}/{{iid}}", name=f"del_{entity}")
            def _del(iid: int):
                if entity == "ledgers":
                    if iid == 1:
                        raise HTTPException(400, "The default ledger cannot be deleted")
                    if db.one("SELECT 1 x FROM transactions WHERE ledger_id=?", (iid,)):
                        raise HTTPException(400, "Ledger has transactions")
                with db.tx() as con:
                    if entity != "ledgers":
                        con.execute(f"UPDATE transactions SET {col}=NULL WHERE {col}=?", (iid,))
                    con.execute(f"DELETE FROM {entity} WHERE id=?", (iid,))
                return {"ok": True}
        _make()

    # ------------------------------------------------------------ budgets
    @app.get("/api/budgets")
    def budgets(month: str | None = None, ledger_id: int | None = None):
        month = month or date.today().strftime("%Y-%m")
        return {"month": month, "items": ledger.budget_progress(db, month, ledger_id)}

    @app.post("/api/budgets")
    def set_budget(body: dict = Body(...)):
        month = body.get("month") or date.today().strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise HTTPException(400, "month must be YYYY-MM")
        cid = body.get("category_id") or None
        amt = float(body.get("amount") or 0)
        lid = int(body.get("ledger_id") or 1)
        with db.tx() as con:
            con.execute("DELETE FROM budgets WHERE ledger_id=? AND month=? AND category_id IS ?", (lid, month, cid))
            if amt > 0:
                con.execute("INSERT INTO budgets(ledger_id,category_id,month,amount) VALUES(?,?,?,?)", (lid, cid, month, amt))
        return budgets(month, lid)

    @app.delete("/api/budgets/{bid}")
    def del_budget(bid: int):
        with db.tx() as con:
            con.execute("DELETE FROM budgets WHERE id=?", (bid,))
        return {"ok": True}

    @app.post("/api/budgets/copy")
    def copy_budgets(body: dict = Body(...)):
        src, dst = body["from"], body["to"]
        lid = int(body.get("ledger_id") or 1)
        with db.tx() as con:
            rows = con.execute("SELECT category_id, amount FROM budgets WHERE month=? AND ledger_id=?", (src, lid)).fetchall()
            for r in rows:
                con.execute("DELETE FROM budgets WHERE ledger_id=? AND month=? AND category_id IS ?", (lid, dst, r[0]))
                con.execute("INSERT INTO budgets(ledger_id,category_id,month,amount) VALUES(?,?,?,?)", (lid, r[0], dst, r[1]))
        return budgets(dst, lid)

    # ------------------------------------------------------------ investments
    @app.get("/api/investments")
    def investments():
        accts = [a for a in ledger.accounts_with_balances(db) if a["is_investment"]]
        hold = ledger.holdings(db)
        names = {a["id"]: a["name"] for a in accts}
        for h in hold:
            h["account_name"] = names.get(h["account_id"], "")
        trades, _ = ledger.query_tx(db, {"kind": "buy,sell,dividend,invest_fee", "limit": 200})
        return {"accounts": accts, "holdings": hold, "trades": trades, "prices": db.all("SELECT * FROM prices ORDER BY ticker")}

    # ------------------------------------------------------------ scheduled transactions 周期记账
    def _sched_err(fn):
        try:
            return fn()
        except schedules.ScheduleError as e:
            raise HTTPException(400, str(e))
        except KeyError:
            raise HTTPException(404, "Schedule not found")

    @app.get("/api/schedules")
    def list_schedules():
        items = [schedules.decorate(db, s) for s in db.all("SELECT * FROM schedules ORDER BY enabled DESC, name")]
        return {"items": items, "upcoming": schedules.upcoming(db, 30), "runs": schedules.runs(db, 10),
                "settings": {k: db.get_setting(k, SETTING_DEFAULTS[k]) for k in ("schedule_run_time", "schedule_catchup_days", "timezone")},
                "today": schedules.local_today(db).isoformat()}

    @app.post("/api/schedules")
    def add_schedule(body: dict = Body(...)):
        return _sched_err(lambda: schedules.save(db, body))

    @app.post("/api/schedules/preview")
    def preview_schedule(body: dict = Body(...)):
        """Next occurrence dates + description for an unsaved rule (editor live preview)."""
        def go():
            today = schedules.local_today(db)
            after = max(today, schedules._d(body.get("start_date")) or today)
            return {"description": schedules.describe(body),
                    "next_dates": [f"{d.isoformat()} {schedules.normalize_rule(body)['time_of_day']}"
                                   for d in schedules.next_dates(body, after, int(body.get("count") or 5))],
                    "past_due": sum(1 for d in schedules.iter_occurrences(body, today - timedelta(days=1))
                                    if d >= today - timedelta(days=int(db.get_setting("schedule_catchup_days") or 90)))}
        return _sched_err(go)

    @app.get("/api/schedules/upcoming")
    def upcoming_schedules(days: int = 30):
        return {"items": schedules.upcoming(db, max(1, min(days, 366)))}

    @app.get("/api/schedules/runs")
    def schedule_runs(limit: int = 30):
        return {"items": schedules.runs(db, limit)}

    @app.post("/api/schedules/run")
    def run_schedules(body: dict = Body(default={})):
        return _sched_err(lambda: schedules.run(db, (body or {}).get("date"), bool((body or {}).get("dry_run")), "manual"))

    @app.get("/api/schedules/from-transaction/{tid}")
    def schedule_from_tx(tid: int):
        return _sched_err(lambda: schedules.from_transaction(db, tid))

    @app.get("/api/schedules/{sid}")
    def one_schedule(sid: int):
        s = schedules.get(db, sid)
        if not s:
            raise HTTPException(404, "Schedule not found")
        s["transactions"], _ = ledger.query_tx(db, {"schedule_id": sid, "status": "all", "limit": 20})
        return s

    @app.put("/api/schedules/{sid}")
    def upd_schedule(sid: int, body: dict = Body(...)):
        return _sched_err(lambda: schedules.save(db, body, sid))

    @app.post("/api/schedules/{sid}/pause")
    def pause_schedule(sid: int):
        return _sched_err(lambda: schedules.set_enabled(db, sid, False))

    @app.post("/api/schedules/{sid}/resume")
    def resume_schedule(sid: int):
        return _sched_err(lambda: schedules.set_enabled(db, sid, True))

    @app.delete("/api/schedules/{sid}")
    def del_schedule(sid: int, keep_history: bool = True):
        if not db.one("SELECT 1 x FROM schedules WHERE id=?", (sid,)):
            raise HTTPException(404, "Schedule not found")
        return schedules.delete(db, sid, keep_history)

    @app.get("/api/investments/principal-returns")
    def investments_principal_returns(start: str | None = Query(None, alias="from"), end: str | None = Query(None, alias="to"),
                                      interval: str = "auto", include_archived: bool = False):
        try:
            return principal.portfolio(db, start, end, interval, include_archived=include_archived)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/investments/returns")
    def investment_returns(period: str = "ytd", start: str | None = None, end: str | None = None, scope: str = "all"):
        try:
            return returns.report(db, period, start, end, scope)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/adjustments/reclassify")
    def reclassify_preview(include_savings: bool = False, account_ids: str = "", only: bool = False):
        """Preview only: lists unclassified balance adjustments with a proposed reason. Writes nothing."""
        ids = [int(x) for x in account_ids.split(",") if x.strip().isdigit()]
        return returns.reclassify_preview(db, include_savings, ids, only=only)

    @app.post("/api/adjustments/reclassify")
    def reclassify_apply(body: dict = Body(...)):
        items = body.get("items") or []
        if not items:
            raise HTTPException(400, "Nothing selected")
        p = bk.make_backup(db, "before-reclassify")
        return {**returns.reclassify_apply(db, items), "backup": p.name}

    @app.post("/api/prices")
    def manual_price(body: dict = Body(...)):
        quotes.set_price(db, str(body["ticker"]).upper(), float(body["price"]), body.get("currency") or "USD", "manual")
        return {"ok": True}

    @app.post("/api/prices/refresh")
    def refresh_prices():
        tickers = [r["ticker"] for r in db.all("SELECT DISTINCT ticker FROM transactions WHERE ticker IS NOT NULL")]
        return quotes.refresh_prices(db, tickers)

    # ------------------------------------------------------------ dashboard & reports
    @app.get("/api/dashboard")
    def dashboard(month: str | None = None, ledger_id: int | None = None):
        return ledger.dashboard(db, month, ledger_id)

    @app.get("/api/reports")
    def reports(start: str | None = None, end: str | None = None, group_by: str = "category", kind: str = "expense",
                ledger_id: int | None = None, project_id: int | None = None):
        today = date.today()
        start = start or f"{today.year}-01-01"
        end_excl = (end + " 99") if end else f"{today.year + 1}-01-01"
        if group_by not in ("category", "subcategory", "member", "merchant", "project", "account", "tag", "month"):
            raise HTTPException(400, "bad group_by")
        return {"rows": ledger.breakdown(db, start, end_excl, group_by, kind, ledger_id, project_id),
                "totals": ledger.totals(db, start, end_excl, ledger_id, project_id), "start": start, "end": end,
                "project_id": project_id}

    @app.get("/api/reports/tax-tips")
    def tax_tips(start: str | None = None, end: str | None = None, group_by: str = "month", kind: str = "expense",
                 ledger_id: int | None = None, project_id: int | None = None):
        """Tax & tips 税/小费 paid (or, kind=income, received): only transactions where they were recorded."""
        today = date.today()
        start = start or f"{today.year}-01-01"
        end_excl = (end + " 99") if end else f"{today.year + 1}-01-01"
        if kind not in ("expense", "income", "all"):
            raise HTTPException(400, "kind must be expense, income or all")
        try:
            return taxtips.summary(db, start, end_excl, group_by, kind, ledger_id, project_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # ------------------------------------------------------------ off-site backup, SMTP, emailed reports (all OFF by default)
    def _cfg_err(fn):
        try:
            return fn()
        except (ValueError, mailer.MailError) as e:
            return JSONResponse({"ok": False, "detail": str(e), "message": str(e)}, status_code=400)

    @app.get("/api/offsite")
    def offsite_get():
        return offsite.status(db)

    @app.put("/api/offsite")
    def offsite_put(body: dict = Body(...)):
        return _cfg_err(lambda: offsite.update(db, body))

    @app.post("/api/offsite/run")
    def offsite_run():
        return offsite.run(db, "manual")

    @app.get("/api/email/settings")
    def email_get():
        return email_reports.status(db)

    @app.put("/api/email/settings")
    def email_put(body: dict = Body(...)):
        def go():
            mailer.update(db, secret, {k: v for k, v in body.items() if k.startswith("smtp_") or k == "clear_password"})
            return email_reports.update(db, {k: v for k, v in body.items() if k.startswith("email_")})
        return _cfg_err(go)

    @app.post("/api/email/test")
    def email_test(body: dict | None = Body(None)):
        b = body or {}
        return _cfg_err(lambda: email_reports.test_send(db, secret, b.get("to"), b.get("lang")))

    @app.get("/api/email/preview")
    def email_preview(kind: str = "weekly", lang: str | None = None):
        if kind not in email_reports.JOBS:
            raise HTTPException(400, f"kind must be one of {', '.join(email_reports.JOBS)}")
        m = email_reports.build(db, kind, lng=lang)
        return HTMLResponse(m["html"], headers={"X-Subject": quote(m["subject"])})

    @app.post("/api/email/send")
    def email_send_now(kind: str = "weekly"):
        if kind not in email_reports.JOBS:
            raise HTTPException(400, f"kind must be one of {', '.join(email_reports.JOBS)}")
        return _cfg_err(lambda: email_reports.send_job(db, secret, kind))

    @app.get("/api/networth")
    def networth(months: int = 12):
        return {"current": ledger.net_worth(db), "history": ledger.networth_history(db, months)}

    # ------------------------------------------------------------ backups
    @app.get("/api/backups")
    def backups():
        return {"items": bk.list_backups(db), "rclone": bk.rclone_status(), "last_backup_at": db.get_setting("last_backup_at"),
                "last_gdrive_sync": db.get_setting("last_gdrive_sync"), "dir": str(bk.backup_dir(db))}

    @app.post("/api/backups")
    def backup_now():
        p = bk.make_backup(db, "manual")
        return {"ok": True, "name": p.name, "gdrive": bk.upload_latest(db)}

    @app.get("/api/backups/download")
    def backup_download():
        p = bk.make_backup(db, "download")
        return FileResponse(p, filename=p.name, media_type="application/octet-stream")

    @app.get("/api/backups/file/{name}")
    def backup_file(name: str):
        if not re.fullmatch(r"finance-[\w\-]+\.db", name):
            raise HTTPException(400)
        p = bk.backup_dir(db) / name
        if not p.exists():
            raise HTTPException(404)
        return FileResponse(p, filename=name, media_type="application/octet-stream")

    @app.post("/api/backups/restore")
    async def backup_restore(file: UploadFile = File(...)):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db", dir=data_dir) as tmp:
            tmp.write(await file.read())
        try:
            safety = bk.restore(db, Path(tmp.name))
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(tmp.name + suffix).unlink(missing_ok=True)
                except OSError:  # Windows: a file still open elsewhere cannot be deleted; harmless leftover
                    pass
        _write_key_file(db)
        return {"ok": True, "safety_backup": safety.name,
                "note": "Restored. If the backup had a different password, log in with that one."}

    @app.post("/api/backups/gdrive-sync")
    def gdrive_sync():
        if not bk.list_backups(db):
            bk.make_backup(db, "manual")
        return bk.upload_latest(db)

    # ------------------------------------------------------------ imports
    def _save_upload(content: bytes, filename: str) -> str:
        token = secrets.token_hex(8)
        ext = Path(filename or "").suffix.lower()[:6] or ".bin"
        (data_dir / "imports" / f"{token}{ext}").write_bytes(content)
        for p in (data_dir / "imports").glob("*"):  # clean old uploads (>1 day)
            if time.time() - p.stat().st_mtime > 86400:
                p.unlink(missing_ok=True)
        return f"{token}{ext}"

    def _load_upload(token: str) -> tuple[str, bytes]:
        if not re.fullmatch(r"[0-9a-f]{16}\.\w{1,5}", token or ""):
            raise HTTPException(400, "bad token")
        p = data_dir / "imports" / token
        if not p.exists():
            raise HTTPException(404, "Upload expired, upload the file again")
        return token, p.read_bytes()

    @app.post("/api/import/csv/preview")
    async def csv_preview(file: UploadFile = File(...)):
        content = await file.read()
        token = _save_upload(content, file.filename or "x.csv")
        return {"token": token, **importers.csv_preview(content)}

    @app.post("/api/import/csv/commit")
    def csv_commit(body: dict = Body(...)):
        _, content = _load_upload(body.get("token"))
        return importers.csv_commit(db, content, int(body.get("account_id") or 0), body.get("mapping") or {},
                                    body.get("sign") or "negative_is_expense", body.get("date_format") or None,
                                    "review" if body.get("as_review") else "posted", bool(body.get("skip_duplicates", True)),
                                    int(body.get("ledger_id") or 1))

    @app.post("/api/import/sui/preview")
    async def sui_preview(file: UploadFile | None = File(None), token: str | None = Form(None), overrides: str | None = Form(None)):
        if file is not None:
            content = await file.read()
            token = _save_upload(content, file.filename or "x.xls")
        else:
            token, content = _load_upload(token)
        try:
            prev = importers.sui_preview(db, token, content, json.loads(overrides) if overrides else None)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"Could not read file: {type(e).__name__}: {e}")
        return {"token": token, **prev}

    @app.post("/api/import/sui/commit")
    def sui_commit(body: dict = Body(...)):
        token, content = _load_upload(body.get("token"))
        bk.make_backup(db, "before-import")
        return importers.sui_commit(db, token, content, body.get("options") or {})

    # ------------------------------------------------------------ email monitoring
    MB_FIELDS = ["name", "host", "port", "use_ssl", "username", "folder", "interval_minutes", "enabled",
                 "sender_allowlist", "subject_keywords", "after_action", "move_folder", "default_account_id",
                 "auto_post", "lookback_days", "schedule_mode", "schedule_times", "schedule_weekdays"]

    def _mb_public(m: dict) -> dict:
        from . import mail_schedule as ms
        m = dict(m)
        m["password_set"] = bool(m.pop("password_enc", None))
        m["schedule_mode"] = m.get("schedule_mode") or "interval"
        try:
            m["next_run"], m["schedule_text"] = ms.next_run(db, m), ms.describe(m)
        except ms.ScheduleError as e:
            m["next_run"], m["schedule_text"] = None, f"invalid schedule: {e}"
        m["timezone"] = str(ms.tz(db))
        return m

    def _mb_data(body: dict) -> dict:
        d = {k: body[k] for k in MB_FIELDS if k in body}
        for k in ("port", "interval_minutes", "lookback_days"):
            if k in d:
                d[k] = int(d[k] or 0) or None
        for k in ("use_ssl", "enabled", "auto_post"):
            if k in d:
                d[k] = 1 if d[k] in (True, 1, "1", "true", "on") else 0
        if "after_action" in d and d["after_action"] not in ("none", "mark_read", "move"):
            raise HTTPException(400, "after_action: none | mark_read | move")
        if "default_account_id" in d:
            d["default_account_id"] = int(d["default_account_id"]) if d["default_account_id"] else None
        from . import mail_schedule as ms
        try:
            if "schedule_mode" in d:
                d["schedule_mode"] = (d["schedule_mode"] or "interval").strip()
                if d["schedule_mode"] not in ms.MODES:
                    raise ms.ScheduleError("schedule_mode: interval | fixed")
            if "schedule_times" in d:
                d["schedule_times"] = ",".join(ms.parse_times(d["schedule_times"]))
            if "schedule_weekdays" in d:
                d["schedule_weekdays"] = ",".join(map(str, ms.parse_weekdays(d["schedule_weekdays"])))
        except ms.ScheduleError as e:
            raise HTTPException(400, str(e))
        if body.get("password"):
            d["password_enc"] = secret.encrypt(str(body["password"]))
        return d

    def _mb_schedule(before: dict | None, d: dict) -> None:
        """Validates the merged schedule before saving. Fixed mode needs at least one time; switching to fixed (or
        changing its times/days) starts counting from now, so earlier slots aren't caught up."""
        from . import mail_schedule as ms
        m = {**(before or {}), **d}
        if (m.get("schedule_mode") or "interval") != "fixed":
            return
        if not ms.parse_times(m.get("schedule_times") or ""):
            raise HTTPException(400, "Fixed times: add at least one time (HH:MM)")
        keys = ("schedule_mode", "schedule_times", "schedule_weekdays")
        if not before or any((before.get(k) or "") != (m.get(k) or "") for k in keys):
            d["schedule_since"] = ms.local_now(db).strftime(ms.FMT)
            d["last_slot"] = None

    @app.get("/api/mailboxes")
    def mailboxes():
        return [_mb_public(m) for m in db.all("SELECT * FROM mailboxes ORDER BY id")]

    @app.post("/api/mailboxes")
    def add_mailbox(body: dict = Body(...)):
        d = _mb_data(body)
        for k in ("name", "host", "username"):
            if not d.get(k):
                raise HTTPException(400, f"{k} required")
        d.setdefault("folder", "INBOX")
        _mb_schedule(None, d)
        with db.tx() as con:
            cols = list(d)
            mid = con.execute(f"INSERT INTO mailboxes({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [d[c] for c in cols]).lastrowid
        return _mb_public(db.one("SELECT * FROM mailboxes WHERE id=?", (mid,)))

    @app.put("/api/mailboxes/{mid}")
    def upd_mailbox(mid: int, body: dict = Body(...)):
        d = _mb_data(body)
        before = db.one("SELECT * FROM mailboxes WHERE id=?", (mid,))
        if not before:
            raise HTTPException(404, "Mailbox not found")
        _mb_schedule(before, d)
        if d:
            with db.tx() as con:
                con.execute(f"UPDATE mailboxes SET {','.join(k + '=?' for k in d)} WHERE id=?", list(d.values()) + [mid])
        return _mb_public(db.one("SELECT * FROM mailboxes WHERE id=?", (mid,)))

    @app.delete("/api/mailboxes/{mid}")
    def del_mailbox(mid: int):
        with db.tx() as con:
            con.execute("DELETE FROM mailboxes WHERE id=?", (mid,))
        return {"ok": True}

    @app.post("/api/mailboxes/test")
    def test_unsaved(body: dict = Body(...)):
        """Test a config before saving (password in body), or a saved one with {id}."""
        mb = dict(body)
        pw = body.get("password")
        if body.get("id") and not pw:
            saved = db.one("SELECT * FROM mailboxes WHERE id=?", (int(body["id"]),))
            if not saved:
                raise HTTPException(404)
            mb = {**saved, **{k: v for k, v in body.items() if v not in (None, "")}}
            pw = secret.decrypt(saved["password_enc"] or "")
        if not pw:
            raise HTTPException(400, "password required")
        return ingest.test_mailbox(mb, pw)

    @app.post("/api/mailboxes/{mid}/test")
    def test_saved(mid: int):
        m = db.one("SELECT * FROM mailboxes WHERE id=?", (mid,))
        if not m:
            raise HTTPException(404)
        try:
            pw = secret.decrypt(m["password_enc"] or "")
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return ingest.test_mailbox(m, pw)

    @app.post("/api/mailboxes/{mid}/run")
    def run_mailbox(mid: int):
        m = db.one("SELECT * FROM mailboxes WHERE id=?", (mid,))
        if not m:
            raise HTTPException(404)
        return ingest.poll_mailbox(db, m, secret)

    @app.get("/api/ingest-log")
    def ingest_log(limit: int = 200):
        return db.all("""SELECT l.*, m.name mailbox_name, t.status tx_status, t.amount tx_amount, t.kind tx_kind
            FROM ingest_log l LEFT JOIN mailboxes m ON m.id=l.mailbox_id LEFT JOIN transactions t ON t.id=l.transaction_id
            ORDER BY l.id DESC LIMIT ?""", (min(limit, 2000),))

    RULE_FIELDS = ["name", "sender_pattern", "subject_pattern", "amount_regex", "merchant_regex", "last4_regex", "kind",
                   "currency", "enabled", "priority", "note", "adjust_reason"]

    def _rule_data(body: dict) -> dict:
        d = {k: body[k] for k in RULE_FIELDS if k in body}
        for k in ("sender_pattern", "subject_pattern", "amount_regex", "merchant_regex", "last4_regex"):
            if d.get(k):
                try:
                    re.compile(d[k])
                except re.error as e:
                    raise HTTPException(400, f"{k}: invalid regex ({e})")
        from .email_rules import RULE_KINDS
        if "kind" in d and d["kind"] not in RULE_KINDS:
            raise HTTPException(400, "kind: " + " | ".join(RULE_KINDS))
        if "adjust_reason" in d:
            d["adjust_reason"] = (d["adjust_reason"] or "").strip()
            if d["adjust_reason"] and d["adjust_reason"] not in ledger.REASON_CODES:
                raise HTTPException(400, f"adjust_reason must be one of {ledger.REASON_CODES} (blank = account default)")
        if "enabled" in d:
            d["enabled"] = 1 if d["enabled"] in (True, 1, "1", "true", "on") else 0
        return d

    @app.get("/api/email-rules")
    def email_rules():
        return db.all("SELECT * FROM email_rules ORDER BY priority, id")

    @app.post("/api/email-rules")
    def add_rule(body: dict = Body(...)):
        d = _rule_data(body)
        if not d.get("name") or not (d.get("sender_pattern") or d.get("subject_pattern")):
            raise HTTPException(400, "name and a sender or subject pattern are required")
        d.setdefault("priority", 10)
        with db.tx() as con:
            cols = list(d)
            rid = con.execute(f"INSERT INTO email_rules({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [d[c] for c in cols]).lastrowid
        return db.one("SELECT * FROM email_rules WHERE id=?", (rid,))

    @app.put("/api/email-rules/{rid}")
    def upd_rule(rid: int, body: dict = Body(...)):
        d = _rule_data(body)
        if d:
            with db.tx() as con:
                con.execute(f"UPDATE email_rules SET {','.join(k + '=?' for k in d)} WHERE id=?", list(d.values()) + [rid])
        return db.one("SELECT * FROM email_rules WHERE id=?", (rid,))

    @app.delete("/api/email-rules/{rid}")
    def del_rule(rid: int):
        with db.tx() as con:
            con.execute("DELETE FROM email_rules WHERE id=?", (rid,))
        return {"ok": True}

    @app.get("/api/email-rules/templates")
    def email_rule_templates():
        from .email_rules import RULE_TEMPLATES
        return RULE_TEMPLATES

    @app.post("/api/email-rules/test")
    def test_rules(body: dict = Body(...)):
        rules = db.all("SELECT * FROM email_rules WHERE enabled=1")
        if body.get("rule"):
            rules = [dict(_rule_data(body["rule"]), id=0, enabled=1, name=body["rule"].get("name") or "test")]
        res = parse_email(body.get("from") or "", body.get("subject") or "", body.get("body") or "", rules)
        res.pop("text", None)
        if res.get("kind") == "balance":  # preview of the adjustment it would record (no writes)
            res["balance_preview"] = ingest.balance_plan(db, res, ingest._email_date({"received_at": body.get("received_at")}))
        return res

    # ------------------------------------------------------------ demo
    @app.post("/api/demo/load")
    def demo_load():
        return demo.load_demo(db)

    @app.post("/api/demo/clear")
    def demo_clear():
        return demo.clear_demo(db)

    # ------------------------------------------------------------ static & background
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    _write_key_file(db)

    if start_background:
        stop = threading.Event()
        app.state.stop = stop

        def loop():
            time.sleep(5)
            try:  # catch up on missed scheduled transactions after downtime
                r = schedules.run(db, trigger="startup")
                if r["generated"] or r["errors"]:
                    log.info("scheduled transactions (startup): %s", r["summary"])
            except Exception:  # noqa: BLE001
                log.exception("scheduled transactions startup run failed")
            rem_last = [-1e9]
            while not stop.is_set():
                try:
                    if schedules.auto_run_due(db):  # daily at schedule_run_time (default 00:15, user's time zone)
                        r = schedules.run(db, trigger="daily")
                        log.info("scheduled transactions (daily): %s", r["summary"])
                    if bk.backup_due(db):
                        bk.make_backup(db, "auto")
                        bk.upload_latest(db)
                    slot = offsite.due(db)  # off-site (Google Drive) backup: off unless enabled
                    if slot:
                        r = offsite.run(db, "schedule", slot)
                        log.info("off-site backup: %s", r.get("detail"))
                    for r in email_reports.run_due(db, secret):  # emailed reports/reminders: off unless enabled
                        log.info("email %s: %s", r["job"], "ok" if r.get("ok") else r.get("error") or r.get("reason"))
                    for mb in ingest.due_mailboxes(db):
                        ingest.poll_mailbox(db, mb, secret)
                    if time.monotonic() - rem_last[0] > 600:  # reminders: generate + auto-complete (idempotent)
                        rem_last[0] = time.monotonic()
                        reminders.run(db)
                except Exception:  # noqa: BLE001
                    log.exception("background job failed")
                stop.wait(30)

        threading.Thread(target=loop, daemon=True, name="scheduler").start()
        from .telegram_bot import start_if_configured
        app.state.telegram = start_if_configured(db)
    return app


def _write_key_file(db: Database):
    """Convenience copy of the API key for the box owner (chmod 600)."""
    p = Path(db.path).parent / "API_KEY.txt"
    try:
        p.write_text(db.get_setting("api_key") + "\n", encoding="utf-8")
        config.private_file(p)
    except OSError:
        pass
