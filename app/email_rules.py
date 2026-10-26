"""Email alert parsing: per-sender regex rules + generic fallback.

Preset rules are *best effort*: bank alert wording changes over time and differs per product,
so every preset is editable in Settings > Email Monitoring > Parsing rules, and anything a rule
can't parse falls back to the generic extractor and lands in the To-review inbox.
"""
from __future__ import annotations

import html as htmlmod
import re
from typing import Any

AMT_USD = r"\$\s?([\d,]+(?:\.\d{1,2})?)"
LAST4_US = r"(?:ending in|ending|card ending|account ending|last 4 digits?:?|last four digits?:?|acct\.? ending|x{2,}|\*{2,}|\.\.\.)\s*\(?\.{0,3}\s*(\d{4,5})"
AMT_CN = r"(?:人民币|RMB|CNY|￥|¥)\s*([\d,]+(?:\.\d{1,2})?)|([\d,]+(?:\.\d{1,2})?)\s*元"

PRESET_VERSION = 2  # bump when presets change; preset rows are refreshed on startup

PRESET_RULES: list[dict[str, Any]] = [
    {"name": "Chase", "sender_pattern": r"chase\.com", "amount_regex": AMT_USD,
     "merchant_regex": r"(?:transaction with|charge .{0,40}? at|purchase at|\bat)\s+(.+?)(?:\s+(?:on|has|was|for|using)\b|[.\n]|$)",
     "last4_regex": LAST4_US, "kind": "auto", "note": "Chase transaction / charge alerts (best effort)"},
    {"name": "American Express", "sender_pattern": r"americanexpress\.com|aexp\.com", "amount_regex": r"(?:Amount:?\s*)?\$\s?([\d,]+(?:\.\d{1,2})?)",
     "merchant_regex": r"(?:Merchant:?\s*|purchase at\s+|\bat\s+)(.+?)(?:\s+(?:on|for|Amount)\b|[\n.]|$)",
     "last4_regex": r"(?:Card Ending:?|ending in|Account Ending:?)\s*(\d{4,5})", "kind": "auto",
     "note": "Amex large-purchase / transaction alerts; Amex uses 5-digit card endings"},
    {"name": "Bank of America", "sender_pattern": r"bankofamerica\.com|bofa", "amount_regex": r"(?:Amount:?\s*)?\$\s?([\d,]+(?:\.\d{1,2})?)",
     "merchant_regex": r"(?:Where:?\s*|Merchant:?\s*|\bat\s+)(.+?)(?:\s+(?:on|was|has)\b|[\n.]|$)",
     "last4_regex": LAST4_US, "kind": "auto", "note": "BofA alerts usually have 'Amount:' / 'Where:' lines"},
    {"name": "Citi", "sender_pattern": r"citi\.com|citibank|citicards", "amount_regex": AMT_USD,
     "merchant_regex": r"(?:made at|\bat|Merchant:?)\s+(.+?)(?:\s+(?:on|was|has)\b|[\n.]|$)",
     "last4_regex": LAST4_US, "kind": "auto", "note": "Citi purchase alerts"},
    {"name": "Capital One", "sender_pattern": r"capitalone\.com|capital one", "amount_regex": AMT_USD,
     "merchant_regex": r"(?:\bat|Merchant:?)\s+(.+?)(?:,|\s+(?:on|a|was|has|for)\b|[\n.]|$)",
     "last4_regex": LAST4_US, "kind": "auto", "note": "Capital One transaction notifications"},
    {"name": "Discover", "sender_pattern": r"discover\.com|discovercard", "amount_regex": r"(?:Amount:?\s*)?\$\s?([\d,]+(?:\.\d{1,2})?)",
     "merchant_regex": r"(?:Merchant:?\s*|\bat\s+)(.+?)(?:\s+(?:on|was|has)\b|[\n.]|$)",
     "last4_regex": r"(?:Last 4 #:?|ending in|ending)\s*(\d{4})", "kind": "auto", "note": "Discover transaction alerts"},
    {"name": "Alipay 支付宝", "sender_pattern": r"alipay|支付宝", "amount_regex": r"(?:付款金额|支付金额|交易金额|金额)[:：]?\s*(?:￥|¥)?\s*([\d,]+(?:\.\d{1,2})?)",
     "merchant_regex": r"(?:商户名称|商户|收款方|交易对方)[:：]\s*([^\s，,。\n]+)",
     "last4_regex": "", "kind": "auto", "currency": "CNY", "note": "支付宝付款/交易提醒 (best effort)"},
    {"name": "China bank card 银行卡动账 (招行/工行/建行等)", "sender_pattern": r"cmbchina|icbc|ccb\.com|boc\.cn|bankcomm|abchina|95555|95588|95533|银行",
     "amount_regex": AMT_CN,
     "merchant_regex": r"(?:商户[:：]?|在【|在)\s*([^\s，,。】\n]+?)(?:】|消费|支付|快捷|[，,。\s]|$)",
     "last4_regex": r"尾号\s*(\d{4})", "kind": "auto", "currency": "CNY",
     "note": "中国银行卡/信用卡动账短信或邮件, e.g. 您尾号1234的信用卡…消费人民币35.00元 (best effort)"},
]

RULE_KINDS = ("auto", "expense", "income", "refund", "balance", "hold", "bill", "bill_paid", "schedule_post", "schedule_new",
              "loan_payment", "credit", "reward_balance")
# kinds that make something other than a plain transaction (see app/email_actions.py); a rule of these kinds also uses
# date_regex (due / scheduled / payment / expiry date), payee (fixed payee / title), account_id and target_id
ACTION_KINDS = ("bill", "bill_paid", "schedule_post", "schedule_new", "loan_payment", "credit", "reward_balance")
# kinds whose target_id must name a row: schedule_post -> schedules, loan_payment -> loans, reward_balance -> rewards
TARGET_TABLES = {"schedule_post": "schedules", "loan_payment": "loans", "reward_balance": "rewards"}
POINTS_RX = r"([\d,]+)\s*(?:miles|points|pts|avios|里程|积分)"
# kind "bill": a bill / payment request -> a payment task (Bills) with a reminder; "bill_paid": a payment confirmation
# -> matched to the open bill (or kept for review). With "auto" these are detected by bills.classify().
# kind "hold": a card authorization / pre-authorization alert -> a card-hold Reserve (预授权) in the review queue
# Templates offered in Settings → Parsing rules → "From template" (NOT inserted automatically; saved disabled).
RULE_TEMPLATES: list[dict[str, Any]] = [
    {"name": "Statement balance (Total Account Value)", "sender_pattern": "", "subject_pattern": "",
     "amount_regex": r"Total Account Value\s*\$?([\d,]+\.\d{2})", "merchant_regex": "",
     "last4_regex": r"Account:\s*X+(\d{4})", "kind": "balance", "adjust_reason": "investment_gain",
     "currency": "", "enabled": 0, "priority": 20,
     "note": "Statement/summary email: the amount is the NEW account balance; the difference is recorded as a "
             "balance adjustment (set-balance). Fill in the sender regex, then enable."},
]

INCOME_HINTS = ["deposit", "credited", "direct deposit", "payment received", "you received", "refund", "credit posted",
                "存入", "入账", "收入", "退款", "转入", "到账"]
REFUND_HINTS = ["refund", "退款", "returned", "credit for return"]
HOLD_HINTS = ["authorization hold", "pre-authorization", "preauthorization", "pre-authorisation", "pre-auth", "temporary hold",
              "hold placed", "hold has been placed", "incidental hold", "预授权", "冻结"]
TRANSFER_HINTS = ["thank you for your payment", "payment posted", "payment was received", "还款"]
GENERIC_MERCHANT = [
    r"(?:Merchant|Where|Payee|Description)\s*:\s*(.+?)(?:\n|$)",
    r"(?:transaction with|purchase at|charge at|spent at|made at)\s+(.+?)(?:\s+(?:on|was|has|for|using)\b|[.\n]|$)",
    r"(?:商户名称|商户|收款方|交易对方)[:：]\s*([^\s，,。\n]+)",
    r"在【(.+?)】",
]


def strip_html(text: str) -> str:
    if not text:
        return ""
    if "<" in text and ">" in text:
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
    text = htmlmod.unescape(text)
    text = text.replace("\r", "")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _first_group(m: re.Match) -> str | None:
    for g in m.groups() or ():
        if g:
            return g
    return m.group(0) if m else None


def _search(rx: str, text: str):
    if not rx:
        return None
    try:
        m = re.search(rx, text, re.I | re.M)
    except re.error:
        return None
    return _first_group(m) if m else None


def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return round(float(s.replace(",", "")), 2)
    except ValueError:
        return None


def detect_kind(text: str) -> str:
    low = text.lower()
    if any(h in low for h in HOLD_HINTS):
        return "hold"
    if any(h in low for h in TRANSFER_HINTS):
        return "payment"
    if any(h in low for h in REFUND_HINTS):
        return "refund"
    if any(h in low for h in INCOME_HINTS):
        return "income"
    return "expense"


def rule_matches(rule: dict, sender: str, subject: str) -> bool:
    if not rule.get("enabled", 1):
        return False
    try:
        if rule.get("sender_pattern") and not re.search(rule["sender_pattern"], sender or "", re.I):
            return False
        if rule.get("subject_pattern") and not re.search(rule["subject_pattern"], subject or "", re.I):
            return False
    except re.error:
        return False
    return bool(rule.get("sender_pattern") or rule.get("subject_pattern"))


def apply_rule(rule: dict, text: str) -> dict:
    out = {"rule_name": rule["name"], "amount": _to_float(_search(rule.get("amount_regex", ""), text)),
           "merchant": _search(rule.get("merchant_regex", ""), text),
           "last4": _search(rule.get("last4_regex", ""), text), "currency": rule.get("currency") or None}
    kind = rule.get("kind") or "auto"
    out["kind"] = detect_kind(text) if kind == "auto" else kind
    if out["kind"] == "balance":
        out["adjust_reason"] = rule.get("adjust_reason") or None
    if out["kind"] in ACTION_KINDS:
        out.update(rule_id=rule.get("id"), date_text=_search(rule.get("date_regex", ""), text),
                   payee=(rule.get("payee") or "").strip() or None, rule_account_id=rule.get("account_id") or None,
                   target_id=rule.get("target_id") or None, rule_amount=out["amount"], rule_currency=out["currency"])
    if out["kind"] == "reward_balance" and rule.get("amount_regex"):  # a points balance: no cents, no currency
        v = _search(rule["amount_regex"], text)
        out["amount"] = _to_float(v) if v else None
    return out


def generic_extract(text: str) -> dict:
    amount = cur = None
    m = re.search(AMT_USD, text)
    if m:
        amount, cur = _to_float(m.group(1)), "USD"
    else:
        m = re.search(AMT_CN, text)
        if m:
            amount, cur = _to_float(_first_group(m)), "CNY"
    merchant = None
    for rx in GENERIC_MERCHANT:
        merchant = _search(rx, text)
        if merchant:
            break
    last4 = _search(LAST4_US, text) or _search(r"尾号\s*(\d{4})", text)
    return {"rule_name": None, "amount": amount, "currency": cur, "merchant": merchant, "last4": last4,
            "kind": detect_kind(text)}


def parse_email(sender: str, subject: str, body: str, rules: list[dict]) -> dict:
    """Return extracted fields: amount, currency, merchant, last4, kind, rule_name, text."""
    text = strip_html(f"{subject or ''}\n{body or ''}")
    result = None
    for rule in sorted(rules, key=lambda r: (r.get("priority") or 100, r.get("id") or 0)):
        if rule_matches(rule, sender, subject):
            result = apply_rule(rule, text)
            if result["amount"] is not None or result["kind"] in ACTION_KINDS:
                break
    gen = generic_extract(text)
    if result is None:
        result = gen
    elif result["kind"] == "balance":  # statement balance: only the rule's own amount/last4 count
        pass
    elif result["kind"] == "reward_balance":  # miles / points: never a money amount
        if result.get("amount") is None:
            result["amount"] = _to_float(_search(POINTS_RX, text))
        result["currency"] = None
    else:  # fill gaps from the generic extractor
        for k in ("amount", "merchant", "last4", "currency"):
            if not result.get(k) and gen.get(k):
                result[k] = gen[k]
    if result.get("merchant"):
        result["merchant"] = re.sub(r"\s+", " ", result["merchant"]).strip(" .,:;-")[:80]
    result["text"] = text
    return result
