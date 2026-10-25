"""SQLite storage: schema, connection helper and default seed data."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

ACCOUNT_TYPES = {
    # type: (label, default group, is_liability, is_investment)
    "cash": ("Cash 现金", "Cash 现金账户", False, False),
    "checking": ("Checking 活期储蓄卡", "Bank 金融账户", False, False),
    "savings": ("Savings 储蓄/定期", "Bank 金融账户", False, False),
    "credit_card": ("Credit card 信用卡", "Credit Cards 信用卡", True, False),
    "ewallet": ("E-wallet 虚拟账户 (Alipay/WeChat/PayPal)", "E-Wallets 虚拟账户", False, False),
    "brokerage": ("Brokerage / stocks 股票账户", "Investments 投资账户", False, True),
    "retirement": ("Retirement 401k/IRA 养老金", "Investments 投资账户", False, True),
    "crypto": ("Crypto 加密货币", "Investments 投资账户", False, True),
    "loan": ("Loan / mortgage 贷款", "Liabilities 负债", True, False),
    "payable": ("Other payable 应付款项", "Liabilities 负债", True, False),
    "receivable": ("Receivable 应收款项/借出", "Receivables 债权", False, False),
    "other_asset": ("Other asset 其他资产", "Other 其他", False, False),
}
# Reason for difference 差额原因 on a balance adjustment: (code, english, chinese)
ADJ_REASONS = [
    ("investment_gain", "Investment gain/loss", "投资收益/亏损"),
    ("interest", "Interest", "利息"),
    ("fees", "Fees", "费用"),
    ("unrecorded_spending", "Unrecorded spending", "未记支出"),
    ("unrecorded_income", "Unrecorded income", "未记收入"),
    ("correction", "Correction/other", "校正/其他"),
    ("opening", "Opening balance", "期初余额"),
]
DEFAULT_GROUPS = ["Cash 现金账户", "Bank 金融账户", "Credit Cards 信用卡", "E-Wallets 虚拟账户",
                  "Investments 投资账户", "Liabilities 负债", "Receivables 债权", "Other 其他"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ledgers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, note TEXT DEFAULT '', is_demo INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS account_groups (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY, group_id INTEGER REFERENCES account_groups(id), name TEXT NOT NULL,
  type TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'USD', opening_balance REAL NOT NULL DEFAULT 0,
  credit_limit REAL, institution TEXT DEFAULT '', last4 TEXT DEFAULT '', aliases TEXT DEFAULT '',
  note TEXT DEFAULT '', archived INTEGER DEFAULT 0, sort INTEGER DEFAULT 0, is_demo INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, parent_id INTEGER REFERENCES categories(id),
  name TEXT NOT NULL, name_zh TEXT DEFAULT '', keywords TEXT DEFAULT '', sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS members (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, is_demo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS merchants (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, is_demo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, archived INTEGER DEFAULT 0, is_demo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY, ledger_id INTEGER REFERENCES ledgers(id), kind TEXT NOT NULL,
  date TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'USD',
  account_id INTEGER REFERENCES accounts(id), to_account_id INTEGER REFERENCES accounts(id), to_amount REAL,
  orig_amount REAL, orig_currency TEXT,
  category_id INTEGER REFERENCES categories(id), merchant_id INTEGER REFERENCES merchants(id),
  member_id INTEGER REFERENCES members(id), project_id INTEGER REFERENCES projects(id),
  tags TEXT DEFAULT '', note TEXT DEFAULT '', source TEXT DEFAULT 'web', attachment TEXT,
  status TEXT NOT NULL DEFAULT 'posted', confidence REAL, raw_text TEXT, external_id TEXT,
  ticker TEXT, shares REAL, price REAL, fee REAL,
  is_demo INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS ix_tx_acct ON transactions(account_id);
CREATE INDEX IF NOT EXISTS ix_tx_status ON transactions(status);
CREATE INDEX IF NOT EXISTS ix_tx_ext ON transactions(external_id);
CREATE TABLE IF NOT EXISTS budgets (
  id INTEGER PRIMARY KEY, ledger_id INTEGER, category_id INTEGER REFERENCES categories(id),
  month TEXT NOT NULL, amount REAL NOT NULL, is_demo INTEGER DEFAULT 0,
  UNIQUE(ledger_id, category_id, month));
CREATE TABLE IF NOT EXISTS prices (
  ticker TEXT PRIMARY KEY, price REAL NOT NULL, currency TEXT DEFAULT 'USD', source TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS fx_rates (currency TEXT PRIMARY KEY, rate REAL NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS mailboxes (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL, port INTEGER DEFAULT 993,
  use_ssl INTEGER DEFAULT 1, username TEXT NOT NULL, password_enc TEXT, folder TEXT DEFAULT 'INBOX',
  interval_minutes INTEGER DEFAULT 15, enabled INTEGER DEFAULT 1, sender_allowlist TEXT DEFAULT '',
  subject_keywords TEXT DEFAULT '', after_action TEXT DEFAULT 'none', move_folder TEXT DEFAULT '',
  default_account_id INTEGER, auto_post INTEGER DEFAULT 0, lookback_days INTEGER DEFAULT 7,
  last_checked_at TEXT, last_status TEXT, created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS email_seen (
  message_id TEXT PRIMARY KEY, mailbox_id INTEGER, seen_at TEXT DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS ingest_log (
  id INTEGER PRIMARY KEY, time TEXT DEFAULT (datetime('now','localtime')), mailbox_id INTEGER, source TEXT,
  sender TEXT, subject TEXT, message_id TEXT, result TEXT, detail TEXT, rule_name TEXT,
  confidence REAL, transaction_id INTEGER);
CREATE TABLE IF NOT EXISTS email_rules (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, sender_pattern TEXT DEFAULT '', subject_pattern TEXT DEFAULT '',
  amount_regex TEXT DEFAULT '', merchant_regex TEXT DEFAULT '', last4_regex TEXT DEFAULT '',
  kind TEXT DEFAULT 'expense', currency TEXT DEFAULT '', enabled INTEGER DEFAULT 1,
  priority INTEGER DEFAULT 100, is_preset INTEGER DEFAULT 0, note TEXT DEFAULT '');
-- scheduled transactions 周期记账 (AUTOINCREMENT: ids are never reused, so kept history can't collide)
CREATE TABLE IF NOT EXISTS schedules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
  template TEXT NOT NULL, freq TEXT NOT NULL DEFAULT 'monthly', interval INTEGER NOT NULL DEFAULT 1,
  weekdays TEXT DEFAULT '', month_mode TEXT DEFAULT 'day', month_day INTEGER, nth INTEGER, nth_weekday INTEGER,
  start_date TEXT NOT NULL, end_date TEXT, max_count INTEGER, time_of_day TEXT NOT NULL DEFAULT '09:00',
  post_mode TEXT NOT NULL DEFAULT 'post', variable_amount INTEGER NOT NULL DEFAULT 0, skip_before TEXT,
  is_demo INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT);
CREATE TABLE IF NOT EXISTS schedule_occurrences (
  schedule_id INTEGER NOT NULL, occurrence_date TEXT NOT NULL, transaction_id INTEGER,
  created_at TEXT DEFAULT (datetime('now','localtime')), PRIMARY KEY (schedule_id, occurrence_date));
CREATE TABLE IF NOT EXISTS schedule_runs (
  id INTEGER PRIMARY KEY, time TEXT DEFAULT (datetime('now','localtime')), as_of TEXT, trigger TEXT,
  dry_run INTEGER DEFAULT 0, generated INTEGER DEFAULT 0, to_review INTEGER DEFAULT 0, skipped_old INTEGER DEFAULT 0,
  errors TEXT DEFAULT '[]', summary TEXT DEFAULT '');
-- account reconciliation 对账: a checkpoint anchors the account's balance at as_of
CREATE TABLE IF NOT EXISTS reconciliations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL REFERENCES accounts(id), as_of TEXT NOT NULL,
  balance REAL NOT NULL, cash_balance REAL NOT NULL, computed_at_creation REAL NOT NULL, note TEXT DEFAULT '',
  adjustment_tx_id INTEGER, cleared_count INTEGER DEFAULT 0, tx_count INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now','localtime')));
CREATE INDEX IF NOT EXISTS ix_recon_acct ON reconciliations(account_id, as_of);
CREATE TABLE IF NOT EXISTS reconciled_tx (
  reconciliation_id INTEGER NOT NULL, transaction_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
  cleared INTEGER DEFAULT 0, PRIMARY KEY (reconciliation_id, transaction_id));
CREATE INDEX IF NOT EXISTS ix_recontx_tx ON reconciled_tx(transaction_id);
CREATE TRIGGER IF NOT EXISTS trg_tx_del_recon AFTER DELETE ON transactions
  BEGIN DELETE FROM reconciled_tx WHERE transaction_id = OLD.id; END;
CREATE TRIGGER IF NOT EXISTS trg_acct_del_recon AFTER DELETE ON accounts
  BEGIN DELETE FROM reconciled_tx WHERE account_id = OLD.id; DELETE FROM reconciliations WHERE account_id = OLD.id; END;
"""

# (english, chinese, [(sub_en, sub_zh, keywords)])
EXPENSE_CATEGORIES = [
    ("Food & Dining", "食品酒水", [
        ("Meals", "早午晚餐", "lunch,dinner,breakfast,brunch,meal,restaurant,eat out,午饭,午餐,晚饭,晚餐,早饭,早餐,早点,吃饭,夜宵,聚餐"),
        ("Groceries", "买菜超市", "grocery,groceries,supermarket,costco,whole foods,trader joe,safeway,kroger,h mart,99 ranch,买菜,超市,菜市场"),
        ("Coffee & Drinks", "烟酒茶饮", "coffee,starbucks,tea,boba,latte,beer,wine,drinks,咖啡,奶茶,喝茶,饮料,酒,烟"),
        ("Snacks & Fruit", "水果零食", "snack,snacks,fruit,dessert,ice cream,零食,水果,甜品"),
        ("Takeout & Delivery", "外卖", "doordash,uber eats,ubereats,grubhub,takeout,delivery,外卖,美团,饿了么"),
    ]),
    ("Shopping", "购物消费", [
        ("Clothing", "衣服鞋帽", "clothes,clothing,shoes,shirt,jacket,uniqlo,nike,衣服,裤子,鞋,帽子,外套"),
        ("Household Supplies", "日用品", "household,toiletries,target,walmart,detergent,日用品,日用,纸巾"),
        ("Online Shopping", "网购", "amazon,taobao,tmall,ebay,online order,淘宝,天猫,京东,拼多多,网购"),
        ("Electronics", "数码电器", "electronics,apple store,best buy,laptop,iphone,数码,电器,手机"),
        ("Beauty", "化妆护肤", "cosmetics,sephora,skincare,haircut,salon,化妆品,护肤,理发"),
    ]),
    ("Housing", "居家物业", [
        ("Rent", "房租", "rent,房租,租金"),
        ("Mortgage", "房贷", "mortgage,房贷,月供"),
        ("Utilities", "水电煤气", "utilities,utility,electric,electricity,water bill,gas bill,pg&e,水费,电费,燃气费,水电"),
        ("Internet & Phone", "话费网费", "internet,phone bill,comcast,xfinity,verizon,t-mobile,at&t,wifi,话费,网费,宽带,充话费"),
        ("Furniture & Repairs", "家具维修", "ikea,furniture,repair,plumber,家具,维修,装修"),
        ("Property Fees", "物业费", "hoa,property fee,物业,物业费"),
    ]),
    ("Transportation", "行车交通", [
        ("Public Transit", "公共交通", "bus,metro,subway,train,bart,caltrain,clipper,muni,地铁,公交,火车,高铁"),
        ("Taxi & Rideshare", "打车", "uber,lyft,taxi,cab,打车,滴滴,出租车"),
        ("Fuel", "加油", "gas,fuel,gasoline,chevron,shell,arco,加油,油费"),
        ("Parking & Tolls", "停车过路", "parking,toll,fastrak,停车,过路费"),
        ("Car Maintenance", "汽车保养", "car wash,oil change,car repair,tires,保养,洗车,修车"),
        ("Flights", "机票", "flight,airline,airfare,united airlines,delta,机票,飞机"),
    ]),
    ("Entertainment", "休闲娱乐", [
        ("Movies & Shows", "电影演出", "movie,movies,cinema,concert,theater,电影,演出,演唱会"),
        ("Subscriptions", "订阅会员", "netflix,spotify,subscription,youtube premium,icloud,disney+,hulu,chatgpt,会员,订阅"),
        ("Sports & Fitness", "运动健身", "gym,fitness,yoga,climbing,健身,运动,瑜伽"),
        ("Travel & Hotels", "旅游度假", "hotel,airbnb,travel,trip,vacation,酒店,旅游,住宿"),
        ("Games & Hobbies", "游戏爱好", "game,games,steam,nintendo,playstation,游戏,爱好"),
    ]),
    ("Health", "医疗保健", [
        ("Doctor & Hospital", "看病", "doctor,hospital,clinic,copay,urgent care,医院,看病,挂号"),
        ("Pharmacy", "药品", "pharmacy,cvs,walgreens,medicine,药,药店"),
        ("Dental & Vision", "牙科眼科", "dentist,dental,optometrist,glasses,牙医,眼镜"),
    ]),
    ("Education", "学习进修", [
        ("Tuition", "学费", "tuition,学费"),
        ("Books", "书报杂志", "book,books,kindle,书,杂志"),
        ("Courses", "培训课程", "course,class,udemy,coursera,课程,培训"),
    ]),
    ("Social & Gifts", "人情往来", [
        ("Gifts", "送礼", "gift,present,礼物,送礼"),
        ("Red Packets", "红包礼金", "红包,礼金,hongbao,red envelope"),
        ("Donations", "慈善捐助", "donation,charity,捐款,捐赠"),
        ("Treating Others", "请客", "请客,treat friends"),
    ]),
    ("Finance & Insurance", "金融保险", [
        ("Insurance", "保险费", "insurance,premium,geico,保险"),
        ("Bank Fees", "银行手续费", "bank fee,atm fee,service fee,annual fee,late fee,手续费,年费"),
        ("Interest Paid", "利息支出", "interest charge,finance charge,loan interest,利息支出"),
        ("Taxes", "税费", "tax,taxes,irs,税,个税"),
    ]),
    ("Family", "家庭", [
        ("Childcare & Kids", "育儿", "daycare,childcare,babysitter,diapers,toys,托儿,奶粉,尿布,玩具"),
        ("Pets", "宠物", "pet,vet,petco,dog food,cat food,宠物,猫粮,狗粮"),
        ("Parents", "孝敬长辈", "孝敬,父母,爸妈"),
    ]),
    ("Other", "其他杂项", [
        ("Miscellaneous", "其他支出", "misc,miscellaneous,其他"),
        ("Loss", "意外丢失", "lost,丢失,罚款,fine"),
    ]),
]
INCOME_CATEGORIES = [
    ("Work Income", "职业收入", [
        ("Salary", "工资收入", "salary,paycheck,payroll,wage,wages,direct deposit,工资,薪水,发工资"),
        ("Bonus", "奖金收入", "bonus,奖金,年终奖"),
        ("Side Income", "兼职收入", "freelance,side gig,consulting,兼职,外快"),
        ("Reimbursement", "报销", "reimbursement,reimburse,expense report,报销"),
    ]),
    ("Investment Income", "投资收入", [
        ("Interest", "利息收入", "interest,interest earned,利息"),
        ("Dividends", "股息红利", "dividend,dividends,分红,股息"),
        ("Capital Gains", "投资收益", "capital gain,investment gain,理财收益,收益"),
    ]),
    ("Other Income", "其他收入", [
        ("Gifts Received", "礼金收入", "gift received,收红包,收礼金"),
        ("Cashback & Rewards", "返现奖励", "cashback,cash back,rebate,rewards,返现"),
        ("Windfall", "意外来钱", "lottery,prize,中奖"),
        ("Other Income", "其他收入", "income,收入,进账"),
    ]),
]


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """Thin wrapper. One connection per call (sqlite is cheap); a lock serialises writers."""

    def __init__(self, path: str | os.PathLike):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        return con

    @contextmanager
    def tx(self):
        with self.lock:
            con = self.connect()
            try:
                yield con
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()

    # convenience helpers
    def all(self, sql, params=()):
        con = self.connect()
        try:
            return [dict(r) for r in con.execute(sql, params).fetchall()]
        finally:
            con.close()

    def one(self, sql, params=()):
        con = self.connect()
        try:
            r = con.execute(sql, params).fetchone()
            return dict(r) if r else None
        finally:
            con.close()

    def get_setting(self, key, default=None):
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return r["value"] if r else default

    def set_setting(self, key, value):
        with self.tx() as con:
            con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, None if value is None else str(value)))

    def pending_migrations(self) -> list[str]:
        """Schema changes init() is about to apply to an EXISTING database (empty for a new or up-to-date one)."""
        p = Path(self.path)
        if not p.exists() or p.stat().st_size == 0:
            return []
        con = sqlite3.connect(self.path, timeout=30)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "transactions" not in tables:
                return []
            out = []
            if "reimb_links" not in tables or "alloc_amount" not in {r[1] for r in con.execute("PRAGMA table_info(reimb_links)")}:
                out.append("reimb_links.alloc_amount")
            if "accounts" in tables and "card_expiry" not in {r[1] for r in con.execute("PRAGMA table_info(accounts)")}:
                out.append("accounts.card_expiry")
            if "reminders" in tables and "expiry" not in {r[1] for r in con.execute("PRAGMA table_info(reminders)")}:
                out.append("reminders.expiry")
            if "card_expiry_checks" not in tables:
                out.append("card_expiry_checks")
            if "reserves" not in tables or "reserve_links" not in tables:
                out.append("reserves")
            if "accounts" in tables and "system_role" not in {r[1] for r in con.execute("PRAGMA table_info(accounts)")}:
                out.append("accounts.system_role")
            if "reminders" in tables and "reserve_id" not in {r[1] for r in con.execute("PRAGMA table_info(reminders)")}:
                out.append("reminders.reserve_id")
            if "loans" not in tables or "loan_links" not in tables or "loan_events" not in tables:
                out.append("loans")
            if "schedules" in tables and "loan_id" not in {r[1] for r in con.execute("PRAGMA table_info(schedules)")}:
                out.append("schedules.loan_id")
            if "reminders" in tables and "loan_id" not in {r[1] for r in con.execute("PRAGMA table_info(reminders)")}:
                out.append("reminders.loan_id")
            if "credits" not in tables or "credit_uses" not in tables:
                out.append("credits")
            if "bills" not in tables or "bill_payments" not in tables:
                out.append("bills")
            if "reminders" in tables and not {"credit_id", "bill_id"} <= {r[1] for r in con.execute("PRAGMA table_info(reminders)")}:
                out.append("reminders.credit_id+bill_id")
            if "credits" in tables and any(r[1] == "account_id" and r[3] for r in con.execute("PRAGMA table_info(credits)")):
                out.append("credits.account_optional")
            if "rewards" not in tables or "reward_history" not in tables:
                out.append("rewards")
            if "reminders" in tables and "reward_id" not in {r[1] for r in con.execute("PRAGMA table_info(reminders)")}:
                out.append("reminders.reward_id")
            return out
        finally:
            con.close()

    def backup_before_migrate(self) -> Path | None:
        """Copy the database into the backups folder before init() changes the schema of an existing file."""
        todo = self.pending_migrations()
        if not todo:
            return None
        from .backup import make_backup  # local import: backup imports this module
        try:
            return make_backup(self, "before-migrate")
        except Exception:  # noqa: BLE001 - e.g. a custom backup folder that is offline: fall back to data/backups
            import logging
            import shutil
            d = Path(self.path).parent / "backups"
            d.mkdir(parents=True, exist_ok=True)
            dest = d / f"finance-{datetime.now().strftime('%Y%m%d-%H%M%S')}-before-migrate.db"
            src, dst = sqlite3.connect(self.path), sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close(); src.close()
            logging.getLogger("finance").warning("pre-migration backup written to %s", dest)
            return dest

    def init(self):
        self.last_migration_backup = self.backup_before_migrate()
        with self.tx() as con:
            con.executescript(SCHEMA)
            seeded = con.execute("SELECT value FROM settings WHERE key='seeded'").fetchone()
            if not seeded:
                seed_defaults(con)
                con.execute("INSERT INTO settings VALUES('seeded','1')")
            if not con.execute("SELECT 1 FROM settings WHERE key='api_key'").fetchone():
                con.execute("INSERT INTO settings VALUES('api_key',?)", ("fin_" + secrets.token_urlsafe(32),))
            if not con.execute("SELECT 1 FROM settings WHERE key='session_secret'").fetchone():
                con.execute("INSERT INTO settings VALUES('session_secret',?)", (secrets.token_hex(32),))
            # additive migration: system category for balance adjustments (kind 'adjustment', never in income/expense reports)
            if not con.execute("SELECT 1 FROM categories WHERE kind='adjustment'").fetchone():
                con.execute("INSERT INTO categories(kind,parent_id,name,name_zh,keywords,sort) "
                            "VALUES('adjustment',NULL,'Balance adjustment','余额调整','',0)")
            # additive migration: reasons for a balance difference (差额原因) as sub-categories of 'Balance adjustment',
            # identified by a stable `code` (new nullable column; existing rows are untouched apart from tagging the parent)
            cols = {r[1] for r in con.execute("PRAGMA table_info(categories)")}
            if "code" not in cols:
                con.execute("ALTER TABLE categories ADD COLUMN code TEXT")
            parent = con.execute("SELECT id, code FROM categories WHERE kind='adjustment' AND parent_id IS NULL ORDER BY id LIMIT 1").fetchone()
            if parent and parent[1] is None:
                con.execute("UPDATE categories SET code='adjustment' WHERE id=?", (parent[0],))
            if parent:
                for i, (code, en, zh) in enumerate(ADJ_REASONS):
                    if not con.execute("SELECT 1 FROM categories WHERE code=?", (code,)).fetchone():
                        con.execute("INSERT INTO categories(kind,parent_id,name,name_zh,keywords,sort,code) VALUES('adjustment',?,?,?,'',?,?)",
                                    (parent[0], en, zh, i + 1, code))
            # additive migration: generated transactions link back to their schedule
            tcols = {r[1] for r in con.execute("PRAGMA table_info(transactions)")}
            if "schedule_id" not in tcols:
                con.execute("ALTER TABLE transactions ADD COLUMN schedule_id INTEGER")
            con.execute("CREATE INDEX IF NOT EXISTS ix_tx_sched ON transactions(schedule_id)")
            # additive migration: principal/returns override per transaction (NULL = automatic) and the part of an
            # account's opening balance that is prior returns (NULL/0 = all principal)
            if "pr_class" not in tcols:
                con.execute("ALTER TABLE transactions ADD COLUMN pr_class TEXT")
            # additive migration: reimbursements 报销 (flag + optional fields on expenses, links from receipts)
            for col, typ in (("reimbursable", "INTEGER DEFAULT 0"), ("reimb_payer", "TEXT"), ("reimb_expected", "REAL"),
                             ("reimb_note", "TEXT"), ("reimb_writeoff", "INTEGER DEFAULT 0")):
                if col not in tcols:
                    con.execute(f"ALTER TABLE transactions ADD COLUMN {col} {typ}")
            con.execute("""CREATE TABLE IF NOT EXISTS reimb_links (id INTEGER PRIMARY KEY,
                receipt_id INTEGER NOT NULL REFERENCES transactions(id), expense_id INTEGER NOT NULL REFERENCES transactions(id),
                amount REAL NOT NULL, created_at TEXT DEFAULT (datetime('now','localtime')))""")
            con.execute("CREATE INDEX IF NOT EXISTS ix_rl_receipt ON reimb_links(receipt_id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_rl_expense ON reimb_links(expense_id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_tx_reimb ON transactions(reimbursable)")
            # reimbursement links (additive): alloc_amount = the part of the reimbursement (in ITS currency) a link uses, so a
            # reimbursement shows allocated vs unallocated money even when the expense is in another currency. `amount` stays
            # the part of the expense (expense currency) it reimburses. Existing links are back-filled once (see below).
            if "alloc_amount" not in {r[1] for r in con.execute("PRAGMA table_info(reimb_links)")}:
                con.execute("ALTER TABLE reimb_links ADD COLUMN alloc_amount REAL")
            _backfill_alloc(con)
            # deleting a receipt or an expense removes its links (every delete path, incl. bulk/account/demo)
            con.execute("""CREATE TRIGGER IF NOT EXISTS trg_tx_delete_reimb AFTER DELETE ON transactions BEGIN
                DELETE FROM reimb_links WHERE receipt_id=OLD.id OR expense_id=OLD.id; END""")
            acols = {r[1] for r in con.execute("PRAGMA table_info(accounts)")}
            if "opening_returns" not in acols:
                con.execute("ALTER TABLE accounts ADD COLUMN opening_returns REAL")
            # Projects 项目 (additive): extra project fields
            pcols = {r[1] for r in con.execute("PRAGMA table_info(projects)")}
            for col, ddl in (("description", "TEXT"), ("start_date", "TEXT"), ("end_date", "TEXT"), ("budget", "REAL"),
                             ("currency", "TEXT"), ("color", "TEXT"), ("icon", "TEXT"), ("note", "TEXT"),
                             ("sort", "INTEGER DEFAULT 0"), ("created_at", "TEXT"), ("updated_at", "TEXT")):
                if col not in pcols:
                    con.execute(f"ALTER TABLE projects ADD COLUMN {col} {ddl}")
            con.execute("CREATE INDEX IF NOT EXISTS ix_tx_project ON transactions(project_id)")
            # per-project budget plan by category (category_id NULL = "Other / uncategorized" line)
            con.execute("""CREATE TABLE IF NOT EXISTS project_budgets (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
                category_id INTEGER, amount REAL NOT NULL, note TEXT)""")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_pb ON project_budgets(project_id, COALESCE(category_id, 0))")
            # project plan lines (additive): free-named lines, each optionally mapped to categories; transactions can be
            # assigned to a line explicitly (transactions.plan_line_id). catch_all = absorbs project spending no other
            # line matches (the old "Other / uncategorized" category-plan line).
            con.execute("""CREATE TABLE IF NOT EXISTS project_plan_lines (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL,
                name TEXT NOT NULL, amount REAL NOT NULL DEFAULT 0, currency TEXT, note TEXT, sort INTEGER DEFAULT 0,
                catch_all INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)""")
            con.execute("CREATE INDEX IF NOT EXISTS ix_ppl_project ON project_plan_lines(project_id, sort)")
            con.execute("""CREATE TABLE IF NOT EXISTS project_plan_line_cats (line_id INTEGER NOT NULL, category_id INTEGER NOT NULL,
                PRIMARY KEY (line_id, category_id))""")
            if "plan_line_id" not in {r[1] for r in con.execute("PRAGMA table_info(transactions)")}:
                con.execute("ALTER TABLE transactions ADD COLUMN plan_line_id INTEGER")
            # tax / tip 税 / 小费 (additive, optional: NULL = not recorded, never 0)
            tcols = {r[1] for r in con.execute("PRAGMA table_info(transactions)")}
            for col in ("tax_amount", "tip_amount", "tax_rate"):
                if col not in tcols:
                    con.execute(f"ALTER TABLE transactions ADD COLUMN {col} REAL")
            con.execute("CREATE INDEX IF NOT EXISTS ix_tx_plan_line ON transactions(plan_line_id)")
            # one-time: copy each category plan (project_budgets, left untouched) into lines, one per category
            if not con.execute("SELECT 1 FROM settings WHERE key='migr_plan_lines'").fetchone():
                cats = {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, name, parent_id FROM categories")}
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for pid, in con.execute("SELECT DISTINCT project_id FROM project_budgets").fetchall():
                    if con.execute("SELECT 1 FROM project_plan_lines WHERE project_id=?", (pid,)).fetchone():
                        continue
                    rows = con.execute("SELECT category_id, amount, note FROM project_budgets WHERE project_id=? "
                                       "ORDER BY category_id IS NULL, amount DESC", (pid,)).fetchall()
                    for i, (cid, amt, note) in enumerate(rows):
                        if cid is not None and cid not in cats:
                            continue
                        if cid is None:
                            name = "Other"
                        else:
                            nm, par = cats[cid]
                            name = (cats[par][0] + " › " + nm) if par in cats else nm
                        cur = con.execute("INSERT INTO project_plan_lines(project_id, name, amount, note, sort, catch_all, "
                                          "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                                          (pid, name, amt, note, (i + 1) * 10, 1 if cid is None else 0, stamp, stamp))
                        if cid is not None:
                            con.execute("INSERT INTO project_plan_line_cats(line_id, category_id) VALUES(?,?)", (cur.lastrowid, cid))
                con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('migr_plan_lines', '1')")
            # Reminder Center 提醒中心 (additive): card cycle settings + reminders table
            acols2 = {r[1] for r in con.execute("PRAGMA table_info(accounts)")}
            for col, ddl in (("stmt_day", "INTEGER"), ("due_day", "INTEGER"), ("due_after_days", "INTEGER"),
                             ("remind_days", "INTEGER"), ("autopay", "INTEGER DEFAULT 0"), ("min_pay_pct", "REAL")):
                if col not in acols2:
                    con.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")
            if "lead_days" not in {r[1] for r in con.execute("PRAGMA table_info(schedules)")}:
                con.execute("ALTER TABLE schedules ADD COLUMN lead_days INTEGER")
            con.execute("""CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, account_id INTEGER, to_account_id INTEGER, schedule_id INTEGER,
                occurrence_date TEXT, transaction_id INTEGER, title TEXT, cycle_start TEXT, statement_date TEXT,
                due_date TEXT, amount REAL, amount_override REAL, minimum REAL, currency TEXT, reference TEXT, note TEXT,
                autopay INTEGER DEFAULT 0, status TEXT NOT NULL DEFAULT 'open', done_reason TEXT, done_at TEXT,
                snooze_until TEXT, notified_at TEXT, created_at TEXT, updated_at TEXT)""")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rem_card ON reminders(account_id, statement_date) WHERE kind='card'")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rem_sched ON reminders(schedule_id, occurrence_date) WHERE kind='schedule'")
            con.execute("CREATE INDEX IF NOT EXISTS ix_rem_status ON reminders(status, due_date)")
            # card expiry 信用卡到期 (additive): accounts.card_expiry 'YYYY-MM' (end of that month); one 'card_expiry'
            # reminder per card + expiry (reminders.expiry), its card confirmation (card_state renewed/acknowledged) and the
            # per-schedule confirmations of that cycle (card_expiry_checks: confirmed = card updated at the merchant,
            # reassigned = schedule moved to another account). A new expiry = a new reminder = fresh confirmations.
            if "card_expiry" not in {r[1] for r in con.execute("PRAGMA table_info(accounts)")}:
                con.execute("ALTER TABLE accounts ADD COLUMN card_expiry TEXT")
            rcols = {r[1] for r in con.execute("PRAGMA table_info(reminders)")}
            for col in ("expiry", "card_state", "card_state_at"):
                if col not in rcols:
                    con.execute(f"ALTER TABLE reminders ADD COLUMN {col} TEXT")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rem_cexp ON reminders(account_id, expiry) WHERE kind='card_expiry'")
            con.execute("""CREATE TABLE IF NOT EXISTS card_expiry_checks (id INTEGER PRIMARY KEY, reminder_id INTEGER NOT NULL,
                schedule_id INTEGER NOT NULL, action TEXT NOT NULL, from_account_id INTEGER, to_account_id INTEGER, created_at TEXT,
                UNIQUE(reminder_id, schedule_id))""")
            # receipt photos 小票 (additive): files under the receipts folder, path stored relative to it
            con.execute("""CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY, transaction_id INTEGER, rel_path TEXT NOT NULL DEFAULT '', orig_name TEXT,
                mime TEXT, size INTEGER, sha256 TEXT, ocr_text TEXT, ocr_confidence REAL, parsed TEXT,
                kept INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now','localtime')))""")
            con.execute("CREATE INDEX IF NOT EXISTS ix_receipts_tx ON receipts(transaction_id)")
            # mailbox schedule: interval (default, existing behaviour) or fixed local times (additive)
            mcols = {r[1] for r in con.execute("PRAGMA table_info(mailboxes)")}
            for col, ddl in (("schedule_mode", "TEXT DEFAULT 'interval'"), ("schedule_times", "TEXT DEFAULT ''"),
                             ("schedule_weekdays", "TEXT DEFAULT ''"), ("last_slot", "TEXT"), ("schedule_since", "TEXT")):
                if col not in mcols:
                    con.execute(f"ALTER TABLE mailboxes ADD COLUMN {col} {ddl}")
            # email rules: kind 'balance' (statement balance -> set-balance adjustment) + its reason (additive)
            if "adjust_reason" not in {r[1] for r in con.execute("PRAGMA table_info(email_rules)")}:
                con.execute("ALTER TABLE email_rules ADD COLUMN adjust_reason TEXT DEFAULT ''")
            # Reserves 押金/预授权 (additive): a reserve = money set aside with a merchant (hotel / rental) — a card hold
            # (memo only, never a transaction) or a refundable deposit (a transfer into the system "Deposits 押金"
            # receivable account, accounts.system_role='deposits', one per currency, created on first use). reserve_links
            # tie transactions to it with a role (deposit / charge / refund / apply / forfeit / fx). No rows are created here.
            con.execute("""CREATE TABLE IF NOT EXISTS reserves (
                id INTEGER PRIMARY KEY, ledger_id INTEGER, method TEXT NOT NULL, title TEXT DEFAULT '', merchant_id INTEGER,
                account_id INTEGER, deposit_account_id INTEGER, amount REAL NOT NULL DEFAULT 0, currency TEXT NOT NULL,
                start_date TEXT NOT NULL, end_date TEXT, category_id INTEGER, project_id INTEGER, member_id INTEGER, ref TEXT,
                status TEXT NOT NULL DEFAULT 'open', closed_state TEXT, closed_reason TEXT, closed_at TEXT,
                needs_review INTEGER DEFAULT 0, remind INTEGER DEFAULT 1, source TEXT DEFAULT 'web', external_id TEXT,
                raw_text TEXT, note TEXT DEFAULT '', is_demo INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS reserve_links (
                id INTEGER PRIMARY KEY, reserve_id INTEGER NOT NULL, transaction_id INTEGER NOT NULL, role TEXT NOT NULL,
                amount REAL NOT NULL, alloc_amount REAL, final INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')))""")
            con.execute("CREATE INDEX IF NOT EXISTS ix_rsl_res ON reserve_links(reserve_id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_rsl_tx ON reserve_links(transaction_id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_res_status ON reserves(status)")
            con.execute("""CREATE TRIGGER IF NOT EXISTS trg_tx_delete_reserve AFTER DELETE ON transactions BEGIN
                DELETE FROM reserve_links WHERE transaction_id=OLD.id; END""")
            if "system_role" not in {r[1] for r in con.execute("PRAGMA table_info(accounts)")}:
                con.execute("ALTER TABLE accounts ADD COLUMN system_role TEXT")
            if "reserve_id" not in {r[1] for r in con.execute("PRAGMA table_info(reminders)")}:
                con.execute("ALTER TABLE reminders ADD COLUMN reserve_id INTEGER")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rem_reserve ON reminders(reserve_id) WHERE kind='reserve'")
            # Loan Center 借贷中心 (additive): money lent / borrowed. A loan owns one account (loan / payable / receivable)
            # whose balance is what is still owed; loan_events group the linked transactions of one disbursement or
            # repayment (principal = transfer, interest / escrow / fee = expense or income). No rows are created here.
            con.execute("""CREATE TABLE IF NOT EXISTS loans (
                id INTEGER PRIMARY KEY, ledger_id INTEGER, direction TEXT NOT NULL, name TEXT DEFAULT '', counterparty TEXT DEFAULT '',
                kind TEXT DEFAULT 'person', account_id INTEGER NOT NULL, pay_account_id INTEGER, currency TEXT NOT NULL,
                principal REAL, start_date TEXT NOT NULL, rate REAL DEFAULT 0, freq TEXT NOT NULL DEFAULT 'none', first_due TEXT,
                term INTEGER, payment REAL, escrow REAL, interest_category_id INTEGER, escrow_category_id INTEGER, maturity_date TEXT,
                schedule_id INTEGER, adopted_schedule_id INTEGER, status TEXT NOT NULL DEFAULT 'active', closed_at TEXT,
                closed_reason TEXT, remind INTEGER DEFAULT 1, note TEXT DEFAULT '', is_demo INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS loan_events (
                id INTEGER PRIMARY KEY, loan_id INTEGER NOT NULL, kind TEXT NOT NULL, date TEXT NOT NULL, due_date TEXT,
                account_id INTEGER, note TEXT, created_at TEXT DEFAULT (datetime('now','localtime')))""")
            con.execute("""CREATE TABLE IF NOT EXISTS loan_links (
                id INTEGER PRIMARY KEY, loan_id INTEGER NOT NULL, event_id INTEGER NOT NULL, transaction_id INTEGER NOT NULL,
                role TEXT NOT NULL, amount REAL NOT NULL, created_at TEXT DEFAULT (datetime('now','localtime')))""")
            con.execute("CREATE INDEX IF NOT EXISTS ix_loan_acct ON loans(account_id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_lev_loan ON loan_events(loan_id, date)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_llk_loan ON loan_links(loan_id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_llk_tx ON loan_links(transaction_id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_llk_event ON loan_links(event_id)")
            con.execute("""CREATE TRIGGER IF NOT EXISTS trg_tx_delete_loan AFTER DELETE ON transactions BEGIN
                DELETE FROM loan_links WHERE transaction_id=OLD.id; END""")
            if "loan_id" not in {r[1] for r in con.execute("PRAGMA table_info(schedules)")}:
                con.execute("ALTER TABLE schedules ADD COLUMN loan_id INTEGER")
            if "loan_id" not in {r[1] for r in con.execute("PRAGMA table_info(reminders)")}:
                con.execute("ALTER TABLE reminders ADD COLUMN loan_id INTEGER")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rem_loan ON reminders(loan_id) WHERE kind='loan'")
            # Expiring credits 抵用金 (additive): refund / cancellation / bonus credits held on an account with an expiry
            # date. Memo only (never a transaction); credit_uses logs each (partial) use. Reminder kind 'credit'.
            # credits.account_id was NOT NULL: a credit may now belong to an issuer only (airline / hotel / store credit not
            # tied to a card). SQLite can't drop NOT NULL, so the table is rebuilt once (rows and ids kept).
            if any(r[1] == "account_id" and r[3] for r in con.execute("PRAGMA table_info(credits)")):
                cols = [r[1] for r in con.execute("PRAGMA table_info(credits)")]
                sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='credits'").fetchone()[0]
                con.execute("DROP TRIGGER IF EXISTS trg_acct_delete_credits")
                con.execute(sql.replace("account_id INTEGER NOT NULL", "account_id INTEGER", 1).replace("CREATE TABLE credits", "CREATE TABLE credits_new", 1))
                con.execute(f"INSERT INTO credits_new({','.join(cols)}) SELECT {','.join(cols)} FROM credits")
                con.execute("DROP TABLE credits")
                con.execute("ALTER TABLE credits_new RENAME TO credits")
            con.execute("""CREATE TABLE IF NOT EXISTS credits (
                id INTEGER PRIMARY KEY, account_id INTEGER, source TEXT NOT NULL, title TEXT DEFAULT '',
                amount REAL NOT NULL, remaining REAL NOT NULL, currency TEXT NOT NULL, issued_date TEXT NOT NULL,
                expiry_date TEXT, status TEXT NOT NULL DEFAULT 'open', reference TEXT DEFAULT '', note TEXT DEFAULT '',
                closed_at TEXT, is_demo INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS credit_uses (
                id INTEGER PRIMARY KEY, credit_id INTEGER NOT NULL, amount REAL NOT NULL, date TEXT NOT NULL,
                note TEXT DEFAULT '', transaction_id INTEGER, created_at TEXT DEFAULT (datetime('now','localtime')))""")
            con.execute("CREATE INDEX IF NOT EXISTS ix_credit_acct ON credits(account_id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_credit_use ON credit_uses(credit_id)")
            con.execute("""CREATE TRIGGER IF NOT EXISTS trg_acct_delete_credits AFTER DELETE ON accounts BEGIN
                DELETE FROM credit_uses WHERE credit_id IN (SELECT id FROM credits WHERE account_id=OLD.id);
                DELETE FROM credits WHERE account_id=OLD.id; END""")
            con.execute("""CREATE TRIGGER IF NOT EXISTS trg_tx_delete_credit_use AFTER DELETE ON transactions BEGIN
                UPDATE credit_uses SET transaction_id=NULL WHERE transaction_id=OLD.id; END""")
            # Email-driven bills 账单待付 (additive): bill / payment-request emails -> payment tasks (reminder kind 'bill');
            # bill_payments = payment confirmations, matched to a bill or kept for review (unmatched / ambiguous).
            con.execute("""CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY, payee TEXT NOT NULL, amount REAL, currency TEXT, due_date TEXT, issued_date TEXT,
                account_id INTEGER, reference TEXT DEFAULT '', note TEXT DEFAULT '', lead_days INTEGER,
                status TEXT NOT NULL DEFAULT 'open', source TEXT DEFAULT 'web', message_id TEXT, sender TEXT,
                sender_domain TEXT, subject TEXT, raw_text TEXT, paid_at TEXT, paid_amount REAL, paid_message_id TEXT,
                paid_subject TEXT, matched TEXT, transaction_id INTEGER, is_demo INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS bill_payments (
                id INTEGER PRIMARY KEY, message_id TEXT, subject TEXT, payee TEXT, amount REAL, currency TEXT,
                paid_date TEXT, sender_domain TEXT, reference TEXT, status TEXT NOT NULL DEFAULT 'unmatched',
                bill_id INTEGER, candidates TEXT DEFAULT '[]', reason TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT)""")
            con.execute("CREATE INDEX IF NOT EXISTS ix_bills_status ON bills(status)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_billpay_status ON bill_payments(status)")
            con.execute("""CREATE TRIGGER IF NOT EXISTS trg_acct_delete_bills AFTER DELETE ON accounts BEGIN
                UPDATE bills SET account_id=NULL WHERE account_id=OLD.id; END""")
            con.execute("""CREATE TRIGGER IF NOT EXISTS trg_tx_delete_bill AFTER DELETE ON transactions BEGIN
                UPDATE bills SET transaction_id=NULL WHERE transaction_id=OLD.id; END""")
            rem_cols = {r[1] for r in con.execute("PRAGMA table_info(reminders)")}
            if "credit_id" not in rem_cols:
                con.execute("ALTER TABLE reminders ADD COLUMN credit_id INTEGER")
            if "bill_id" not in rem_cols:
                con.execute("ALTER TABLE reminders ADD COLUMN bill_id INTEGER")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rem_credit ON reminders(credit_id) WHERE kind='credit'")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rem_bill ON reminders(bill_id) WHERE kind='bill'")
            # Miles & points 里程/积分 (additive): one row per loyalty program (airline miles, hotel points, card points),
            # its balance in its own unit, expiry date and optional value per point. Memo only: never a transaction, not
            # in net worth. reward_history keeps every balance update. Reminder kind 'reward' (expiry, like credits).
            con.execute("""CREATE TABLE IF NOT EXISTS rewards (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'airline', unit TEXT NOT NULL DEFAULT 'miles',
                member_no TEXT DEFAULT '', balance REAL NOT NULL DEFAULT 0, balance_date TEXT, expiry_date TEXT,
                value_per_point REAL, value_currency TEXT, account_id INTEGER, note TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active', is_demo INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now','localtime')), updated_at TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS reward_history (
                id INTEGER PRIMARY KEY, reward_id INTEGER NOT NULL, date TEXT NOT NULL, balance REAL NOT NULL, change REAL,
                expiry_date TEXT, note TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now','localtime')))""")
            con.execute("CREATE INDEX IF NOT EXISTS ix_rwh_reward ON reward_history(reward_id, date)")
            con.execute("""CREATE TRIGGER IF NOT EXISTS trg_acct_delete_rewards AFTER DELETE ON accounts BEGIN
                UPDATE rewards SET account_id=NULL WHERE account_id=OLD.id; END""")
            if "reward_id" not in {r[1] for r in con.execute("PRAGMA table_info(reminders)")}:
                con.execute("ALTER TABLE reminders ADD COLUMN reward_id INTEGER")
            con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_rem_reward ON reminders(reward_id) WHERE kind='reward'")
            # cursor pagination / account history (additive indexes)
            con.execute("CREATE INDEX IF NOT EXISTS ix_tx_date_id ON transactions(date, id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_tx_acct_date ON transactions(account_id, date, id)")
            con.execute("CREATE INDEX IF NOT EXISTS ix_tx_to_acct_date ON transactions(to_account_id, date, id)")
            from .email_rules import PRESET_RULES, PRESET_VERSION
            ver = con.execute("SELECT value FROM settings WHERE key='preset_rules_version'").fetchone()
            if not ver or int(ver[0]) < PRESET_VERSION:
                con.execute("DELETE FROM email_rules WHERE is_preset=1")
                con.execute("INSERT OR REPLACE INTO settings VALUES('preset_rules_version',?)", (str(PRESET_VERSION),))
                for r in PRESET_RULES:
                    con.execute("""INSERT INTO email_rules(name,sender_pattern,subject_pattern,amount_regex,merchant_regex,
                        last4_regex,kind,currency,enabled,priority,is_preset,note) VALUES(?,?,?,?,?,?,?,?,1,?,1,?)""",
                                (r["name"], r["sender_pattern"], r.get("subject_pattern", ""), r["amount_regex"],
                                 r.get("merchant_regex", ""), r.get("last4_regex", ""), r.get("kind", "expense"),
                                 r.get("currency", ""), r.get("priority", 50), r.get("note", "")))


def distribute_alloc(receipt_amount: float, receipt_cur: str, links: list[tuple[float, str]], rates: dict[str, float]) -> list[float]:
    """Split a reimbursement's amount over its links -> alloc amount per link (reimbursement currency).
    links = [(amount in the expense's currency, expense currency)]. A same-currency link uses exactly its amount; links in
    another currency share what is left, in proportion to their converted values (so a USD reimbursement of 98.50 for a
    700 CNY expense allocates all 98.50 to it)."""
    def conv(v, f, t):
        if f == t or not v:
            return v
        fr, tr = rates.get(f), rates.get(t)
        return v * fr / tr if fr and tr else v
    out: list[float | None] = [round(a, 2) if c == receipt_cur else None for a, c in links]
    cross = [i for i, x in enumerate(out) if x is None]
    if cross:
        left = round(receipt_amount - sum(x for x in out if x is not None), 2)
        conv_vals = {i: conv(links[i][0], links[i][1], receipt_cur) for i in cross}
        tot = sum(conv_vals.values())
        if left > 0 and tot > 0:
            acc = 0.0
            for n, i in enumerate(cross):
                v = round(left - acc, 2) if n == len(cross) - 1 else round(left * conv_vals[i] / tot, 2)
                out[i] = v; acc += v
        else:
            for i in cross:
                out[i] = round(conv_vals[i], 2)
    return [float(x) for x in out]


def _backfill_alloc(con: sqlite3.Connection):
    """One-time fill of reimb_links.alloc_amount for links made before the column existed (only NULLs are written)."""
    rids = [r[0] for r in con.execute("SELECT DISTINCT receipt_id FROM reimb_links WHERE alloc_amount IS NULL")]
    if not rids:
        return
    rates = {r[0]: r[1] for r in con.execute("SELECT currency, rate FROM fx_rates")}
    for rid in rids:
        rc = con.execute("SELECT amount, currency FROM transactions WHERE id=?", (rid,)).fetchone()
        links = con.execute("""SELECT l.id, l.amount, e.currency FROM reimb_links l JOIN transactions e ON e.id=l.expense_id
            WHERE l.receipt_id=? ORDER BY l.id""", (rid,)).fetchall()
        if not rc or not links:
            continue
        allocs = distribute_alloc(rc[0] or 0, rc[1], [(l[1], l[2]) for l in links], rates)
        for l, a in zip(links, allocs):
            con.execute("UPDATE reimb_links SET alloc_amount=? WHERE id=? AND alloc_amount IS NULL", (a, l[0]))


def seed_defaults(con: sqlite3.Connection):
    defaults = {
        "base_currency": "USD", "review_threshold": "0.75", "backup_interval_hours": "24",
        "backup_keep": "30", "default_account_id": "", "default_ledger_id": "1",
    }
    for k, v in defaults.items():
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    con.execute("INSERT INTO ledgers(id,name,note) VALUES(1,'Personal 个人账本','Default ledger')")
    for i, g in enumerate(DEFAULT_GROUPS):
        con.execute("INSERT OR IGNORE INTO account_groups(name,sort) VALUES(?,?)", (g, i))
    for kind, cats in (("expense", EXPENSE_CATEGORIES), ("income", INCOME_CATEGORIES)):
        for i, (en, zh, subs) in enumerate(cats):
            cur = con.execute("INSERT INTO categories(kind,parent_id,name,name_zh,sort) VALUES(?,?,?,?,?)",
                              (kind, None, en, zh, i))
            pid = cur.lastrowid
            for j, (sen, szh, kw) in enumerate(subs):
                con.execute("INSERT INTO categories(kind,parent_id,name,name_zh,keywords,sort) VALUES(?,?,?,?,?,?)",
                            (kind, pid, sen, szh, kw, j))
    for m in ("Me 本人", "Household 家庭公用"):
        con.execute("INSERT OR IGNORE INTO members(name) VALUES(?)", (m,))
    ts = now_iso()
    # Approximate rates; the user should edit them in Settings > Currencies.
    for cur_, rate in (("USD", 1.0), ("CNY", 0.14)):
        con.execute("INSERT OR IGNORE INTO fx_rates(currency,rate,updated_at) VALUES(?,?,?)", (cur_, rate, ts))


def row_list_json(rows):
    return json.loads(json.dumps(rows, default=str))
