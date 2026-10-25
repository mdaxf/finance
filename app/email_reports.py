"""Emailed reminders 提醒 and daily / weekly / monthly reports 报表 (all OFF by default).

Jobs (each with its own schedule, fixed times like the email-check schedule):
  reminders  - open reminders inside their lead window (setting reminder_lead_days / per reminder) or overdue
  daily      - yesterday;  weekly - the 7 days up to yesterday;  monthly - the previous calendar month
A report covers income/expense, top categories, tax & tips, investments (principal vs returns), reminders due,
reimbursements outstanding and projects vs budget. Language: setting email_lang (en | zh-CN | zh-TW)."""
from __future__ import annotations

import html as H
from datetime import date, timedelta

from . import jobsched, ledger, mailer
from .db import Database

JOBS = ("reminders", "daily", "weekly", "monthly")
DEFAULTS = {"email_lang": "en",
            "email_reminders_enabled": "0", "email_reminders_times": "08:00", "email_reminders_weekdays": "",
            "email_daily_enabled": "0", "email_daily_times": "07:30", "email_daily_weekdays": "",
            "email_weekly_enabled": "0", "email_weekly_times": "07:30", "email_weekly_weekdays": "0",
            "email_monthly_enabled": "0", "email_monthly_times": "07:30", "email_monthly_monthday": "1"}

T = {
    "en": {"daily": "Daily report", "weekly": "Weekly report", "monthly": "Monthly report", "reminders": "Reminders",
           "income": "Income", "expense": "Expense", "net": "Net", "top": "Top spending categories", "tax": "Tax",
           "tips": "Tips", "taxtips": "Tax & tips", "tip_pct": "tip %", "none_tt": "No tax or tips recorded.",
           "inv": "Investments", "principal": "Principal added (net)", "returns": "Returns", "due": "Reminders due",
           "none_due": "Nothing due ✓", "reimb": "Reimbursements outstanding", "items": "items", "projects": "Projects vs budget",
           "budget": "Budget", "spent": "Spent", "none": "—", "footer": "Sent by IACF Finance. Turn these emails off in Settings → Email.",
           "overdue": "overdue", "today": "today", "in_days": "in {n} d", "count": "{n} transactions",
           "subject_rem": "{n} reminder(s) due", "test_subject": "IACF Finance test email",
           "test_body": "SMTP works. Reports and reminders will be sent to these recipients when you turn them on."},
    "zh-CN": {"daily": "每日报表", "weekly": "每周报表", "monthly": "月度报表", "reminders": "提醒",
              "income": "收入", "expense": "支出", "net": "结余", "top": "支出最多的分类", "tax": "税", "tips": "小费",
              "taxtips": "税和小费", "tip_pct": "小费率", "none_tt": "没有记录税或小费。", "inv": "投资",
              "principal": "本金净投入", "returns": "投资收益", "due": "到期提醒", "none_due": "没有到期事项 ✓",
              "reimb": "待报销", "items": "笔", "projects": "项目预算执行", "budget": "预算", "spent": "已花",
              "none": "—", "footer": "由 IACF Finance 发送。可在 设置 → 邮件 中关闭。", "overdue": "已逾期", "today": "今天",
              "in_days": "{n} 天后", "count": "{n} 笔交易", "subject_rem": "{n} 条提醒到期",
              "test_subject": "IACF Finance 测试邮件", "test_body": "SMTP 设置正常。开启后，报表和提醒会发送到这些收件人。"},
    "zh-TW": {"daily": "每日報表", "weekly": "每週報表", "monthly": "月度報表", "reminders": "提醒",
              "income": "收入", "expense": "支出", "net": "結餘", "top": "支出最多的分類", "tax": "稅", "tips": "小費",
              "taxtips": "稅和小費", "tip_pct": "小費率", "none_tt": "沒有記錄稅或小費。", "inv": "投資",
              "principal": "本金淨投入", "returns": "投資收益", "due": "到期提醒", "none_due": "沒有到期事項 ✓",
              "reimb": "待報銷", "items": "筆", "projects": "專案預算執行", "budget": "預算", "spent": "已花",
              "none": "—", "footer": "由 IACF Finance 發送。可在 設定 → 郵件 中關閉。", "overdue": "已逾期", "today": "今天",
              "in_days": "{n} 天後", "count": "{n} 筆交易", "subject_rem": "{n} 條提醒到期",
              "test_subject": "IACF Finance 測試郵件", "test_body": "SMTP 設定正常。開啟後，報表和提醒會寄到這些收件人。"},
}


def lang(db: Database, override: str | None = None) -> str:
    v = override or db.get_setting("email_lang", "en") or "en"
    v = {"zh": "zh-CN", "cn": "zh-CN", "tw": "zh-TW", "zh-hant": "zh-TW", "zh-hans": "zh-CN"}.get(v.lower(), v)
    return v if v in T else "en"


def config(db: Database) -> dict:
    out = {}
    for k, v in DEFAULTS.items():
        cur = db.get_setting(k, None)
        out[k] = v if cur is None else cur
    return out


def _cfg(db: Database, job: str) -> dict:
    c = config(db)
    return {"enabled": c.get(f"email_{job}_enabled") == "1" and mailer.config(db)["configured"], "mode": "fixed",
            "times": c.get(f"email_{job}_times"), "weekdays": c.get(f"email_{job}_weekdays", ""),
            "monthday": c.get(f"email_{job}_monthday", "0") if job == "monthly" else "0",
            "last_slot": db.get_setting(f"email_{job}_last_slot"), "since": db.get_setting(f"email_{job}_since")}


def update(db: Database, body: dict) -> dict:
    out = {}
    if "email_lang" in body:
        out["email_lang"] = lang(db, str(body["email_lang"] or "en"))
    for job in JOBS:
        sub = {k[len(f"email_{job}_"):]: v for k, v in body.items() if k.startswith(f"email_{job}_")}
        sub.pop("enabled", None)
        out.update({f"email_{job}_{k}": v for k, v in jobsched.validate(sub, "").items()})
        k = f"email_{job}_enabled"
        if k in body:
            on = str(body[k]).lower() in ("1", "true", "on", "yes")
            if on and not mailer.config(db)["configured"]:
                raise mailer.MailError("Set up SMTP (host, From, To) before turning on emails")
            if on and config(db)[k] != "1":
                db.set_setting(f"email_{job}_since", ledger_now())
            out[k] = "1" if on else "0"
        if any(f"email_{job}_{x}" in out for x in ("times", "weekdays", "monthday")):
            db.set_setting(f"email_{job}_since", ledger_now())
    for k, v in out.items():
        db.set_setting(k, v)
    return status(db)


def _ui_zh() -> bool:
    from .ingest import msg_zh  # the request's language (?lang / Accept-Language)
    return msg_zh()


def ledger_now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def status(db: Database) -> dict:
    c = config(db)
    jobs = {}
    for job in JOBS:
        cfg = _cfg(db, job)
        jobs[job] = {"enabled": c[f"email_{job}_enabled"] == "1", "schedule": jobsched.describe(cfg, _ui_zh()),
                     "next_run": jobsched.next_run(db, cfg), "last_run": db.get_setting(f"email_{job}_last_run"),
                     "last_status": db.get_setting(f"email_{job}_last_status")}
    return {**c, "jobs": jobs, "smtp": mailer.config(db)}


# ---------------------------------------------------------------- content
def period_range(period: str, today: date) -> tuple[date, date]:
    """Inclusive (start, end) for the report sent on `today`."""
    y = today - timedelta(days=1)
    if period == "daily":
        return y, y
    if period == "weekly":
        return y - timedelta(days=6), y
    first = today.replace(day=1)
    pe = first - timedelta(days=1)
    return pe.replace(day=1), pe


def _money(v, cur) -> str:
    from .ingest import fmt_money
    return fmt_money(v or 0, cur)


def build(db: Database, kind: str, today: date | None = None, lng: str | None = None) -> dict:
    """{subject, html, text, sections} for kind in daily|weekly|monthly|reminders. Read-only."""
    from . import reimburse, reminders, taxtips
    from . import projects as prj
    today = today or reminders.today(db)
    L = T[lang(db, lng)]
    zh = lang(db, lng) != "en"
    base = db.get_setting("base_currency", "USD")
    secs: list[tuple[str, list[list[str]] | str]] = []
    due = reminders.digest(db, 3650, True, "zh" if zh else "en", today)["items"]
    due_rows = []
    for r in due:
        amt = r["remaining"] if r["remaining"] is not None else r["statement_amount"]
        when = L["overdue"] if r["state"] == "overdue" else L["today"] if r["days_left"] == 0 else L["in_days"].format(n=r["days_left"])
        title = r["title"]
        if r["kind"] == "card_expiry":  # card expires: which scheduled payments still need the new card
            title = "💳 " + reminders.expiry_text(r, zh, lang(db, lng) == "zh-TW")
        if r["kind"] == "reserve":  # hold not released / deposit not refunded
            title = "🔒 " + reminders.reserve_text(r, zh, lang(db, lng) == "zh-TW")
        if r["kind"] == "credit":  # credit (refund / cancellation / bonus) about to expire or expired
            title = "🎟 " + reminders.credit_text(r, zh, lang(db, lng) == "zh-TW")
        if r["kind"] == "reward":  # miles / points about to expire
            from .rewards import reminder_text
            title = (r.get("reward_icon") or "⭐") + " " + reminder_text(r, zh, lang(db, lng) == "zh-TW")
        if r["kind"] == "bill":  # bill / payment request from an email
            title = "🧾 " + title
        due_rows.append([r["due_date"] or "", title, when, _money(amt, r["currency"]) if amt else ""])
    if kind == "reminders":
        subject = f"⏰ {L['subject_rem'].format(n=len(due))}"
        secs.append((L["due"], due_rows or L["none_due"]))
        return _render(subject, L["reminders"] + f" · {today.isoformat()}", secs, L, count=len(due))
    s, e = period_range(kind, today)
    e_excl = (e + timedelta(days=1)).isoformat()
    t = ledger.totals(db, s.isoformat(), e_excl)
    label = s.isoformat() if s == e else f"{s.isoformat()} – {e.isoformat()}"
    secs.append(("", [[L["income"], _money(t["income"], base)], [L["expense"], _money(t["expense"], base)],
                      [L["net"], _money(t["net"], base)]]))
    top = ledger.breakdown(db, s.isoformat(), e_excl, "category", "expense")[:5]
    secs.append((L["top"], [[r["name"], _money(r["amount"], base), f"{r.get('pct', '')}%"] for r in top] or L["none"]))
    tt = taxtips.summary(db, s.isoformat(), e_excl)["totals"]
    secs.append((L["taxtips"], [[L["tax"], _money(tt["tax"], base) + f" ({tt['tax_count']})"],
                               [L["tips"], _money(tt["tip"], base) + f" ({tt['tip_count']})" + (f" · {L['tip_pct']} {tt['tip_pct']}%" if tt["tip_pct"] is not None else "")]]
                 if tt["count"] else L["none_tt"]))
    try:
        from . import returns
        inv = returns.report(db, "custom", s.isoformat(), e.isoformat(), today=today)
        tot = next((r for r in inv["rows"] if r["level"] == "total"), None)
        if tot and tot.get("account_ids"):
            secs.append((L["inv"], [[L["principal"], _money(tot.get("net_contributions"), base)],
                                    [L["returns"], _money(tot.get("returns"), base)]]))
    except Exception:  # noqa: BLE001  (no investment accounts / odd data: skip the section)
        pass
    secs.append((L["due"], due_rows[:10] or L["none_due"]))
    rb = reimburse.list_items(db, {})
    if rb["count"]:
        secs.append((L["reimb"], [[f"{rb['count']} {L['items']}", _money(rb["outstanding"]["total_base"], base)]]))
    pj = [p for p in prj.listing(db)["items"] if p["summary"].get("budget")]
    if pj:
        secs.append((L["projects"], [[p["name"], f"{L['spent']} {_money(p['summary']['expense'], base)}",
                                      f"{L['budget']} {_money(p['summary']['budget'], base)}", f"{p['summary']['budget_pct']}%"] for p in pj[:8]]))
    subject = f"📊 {L[kind]} · {label} · {L['net']} {_money(t['net'], base)}"
    return _render(subject, f"{L[kind]} · {label}", secs, L, count=t.get("count"))


def _render(subject: str, title: str, secs, L, count=None) -> dict:
    e = H.escape
    parts = [f"<div style=\"font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;max-width:640px;color:#222\">"
             f"<h2 style=\"margin:0 0 12px\">{e(title)}</h2>"]
    text = [title, ""]
    for head, body in secs:
        if head:
            parts.append(f"<h3 style=\"margin:18px 0 6px;font-size:15px\">{e(head)}</h3>"); text.append(head)
        if isinstance(body, str):
            parts.append(f"<p style=\"color:#666;margin:4px 0\">{e(body)}</p>"); text.append("  " + body)
        else:
            parts.append("<table style=\"border-collapse:collapse;width:100%;font-size:14px\">" + "".join(
                "<tr>" + "".join(f"<td style=\"padding:4px 8px;border-bottom:1px solid #eee;{'text-align:right' if i else ''}\">{e(str(c))}</td>"
                                 for i, c in enumerate(row)) + "</tr>" for row in body) + "</table>")
            text += ["  " + " · ".join(str(c) for c in row if c != "") for row in body]
        text.append("")
    parts.append(f"<p style=\"color:#999;font-size:12px;margin-top:24px\">{e(L['footer'])}</p></div>")
    text.append(L["footer"])
    return {"subject": subject, "html": "".join(parts), "text": "\n".join(text), "count": count}


# ---------------------------------------------------------------- sending
def send_job(db: Database, secret, job: str, today: date | None = None, slot: str | None = None) -> dict:
    if slot and not jobsched.claim(db, f"email_{job}_last_slot", slot):
        return {"ok": False, "skipped": True, "reason": "slot already done"}
    msg = build(db, job, today)
    if job == "reminders" and not msg["count"]:
        res = {"ok": True, "skipped": True, "reason": "nothing due"}
    else:
        try:
            res = mailer.send(db, secret, msg["subject"], msg["html"], msg["text"])
        except mailer.MailError as e:
            res = {"ok": False, "error": str(e)}
    db.set_setting(f"email_{job}_last_run", ledger_now())
    db.set_setting(f"email_{job}_last_status", "ok" if res.get("ok") and not res.get("skipped") else
                   ("skipped: " + res["reason"]) if res.get("skipped") else "FAILED: " + res.get("error", "")[:200])
    return res


def run_due(db: Database, secret) -> list[dict]:
    out = []
    for job in JOBS:
        ok, key = jobsched.is_due(db, _cfg(db, job))
        if ok:
            out.append({"job": job, **send_job(db, secret, job, slot=key)})
    return out


def test_send(db: Database, secret, to: str | None = None, lng: str | None = None) -> dict:
    L = T[lang(db, lng)]
    rc = mailer.recipients(to) if to else None
    return mailer.send(db, secret, L["test_subject"], f"<p>{H.escape(L['test_body'])}</p>", L["test_body"], rc)
