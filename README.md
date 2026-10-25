# IACF Finance

A self-hosted personal finance app modeled on Suishouji (随手记). It runs as a single FastAPI process with a SQLite
database and a responsive single-page UI, so it works on a phone too. UI and default categories are bilingual
(English/中文).

* **URL:** http://127.0.0.1:8088 or http://xxx.xxx.xxx.xxx:8088 (Tailscale). It binds only those two addresses,
  never 0.0.0.0.
* **Data:** `data/finance.db` (SQLite, WAL mode), with backups in `data/backups/`.
* **First run:** open the URL. You'll be sent to `/setup` to choose the single-user password.

Screenshots (made with clearly labeled `[DEMO]` data, which has since been removed) are in `screenshots/`.

## Features
* **Accounts**
  * Types: cash, checking, savings, credit card, e-wallet (Alipay/WeChat/PayPal), brokerage, retirement, crypto,
    loan, payable and receivable.
  * Accounts are organized into groups (现金 / 金融 / 信用卡 / 虚拟 / 投资 / 负债 / 债权), and each account can
    have its own currency.
  * Balance = opening balance + transactions. Liabilities show the amount owed. Credit cards also show the limit
    and available credit.
  * Net worth = assets − liabilities, converted to the base currency with manual FX rates
    (Settings → Currencies).
  * Each account can have aliases (for quick-add) and card last-4 digits (for email alerts).
  * **Accounts page tree:** each group is a collapsible node (click or tap the row).
    * The group row shows the number of accounts and the group total in USD (or whatever your base currency is).
      Liability groups show the amount owed, and mixed-currency groups also show per-currency subtotals.
    * There are *Expand all* and *Collapse all* buttons. Which groups are collapsed is remembered in the browser.
    * Accounts at 0 are dimmed, and you can hide them with *hide zero balances*.
    * The group totals come from `GET /api/accounts`, which now includes a `tree`, and they add up to net worth.
* **Set balance 调整余额**
  * Works like Suishouji's 余额变更. On Accounts, press **⚖️ Set balance** (or change *Current balance* / *Amount owed
    now* in the account's Edit form) and enter the real balance. For credit cards, loans and payables, enter the
    amount owed.
  * Optionally pick a date/time (default now) and add a note.
  * The app records a **balance adjustment** transaction for the difference against the computed balance on that
    date. Its source is `balance_set`, its category is *Balance adjustment 余额调整*, and its note reads
    "Balance set to X (was Y)". If the difference is 0, nothing is recorded.
  * Adjustments change balances and net worth but never count as income or expense in reports, budgets or the
    dashboard. They can be edited or deleted like any transaction.
  * A new account's opening balance is recorded as an *Opening balance 期初余额* adjustment on its start date.
* **Transactions**
  * Kinds: expense, income, transfer, refund and balance adjustment, plus investment buy, sell, dividend and fee.
  * Fields: date/time, amount (in any currency, converted to the account currency), two-level category,
    member, merchant, project, tags, note, source, and an attachment.
  * Search and filter by text, dates, type, account, category, member, merchant, project, tag and source.
  * Edit, delete, bulk delete, CSV export, and CSV import with column mapping.
* **Reserves 押金/预授权**
  * Hotel / rental card holds (a note only: never in balances or stats, the card shows *On hold* and less
    available credit) and refundable deposits (a transfer into the system *Deposits 押金* account). One-step check-out
    with preview, FX adjustment, reminders and email-alert detection. See [Reserves](#reserves-押金预授权-hotel-and-rental-deposits-card-holds).
* **Credits 抵用金** (expiring refund / cancellation / bonus credits): track what is left, use all or part of it,
  get a reminder 6 months (configurable) before it expires, and see expired credits flagged.
  See [Credits](#credits-抵用金-expiring-refund--cancellation--bonus-credits).
* **Bills 账单待付** (email-driven bill schedules): bill and payment-request emails become payment tasks with a
  reminder 3 days (configurable) before the due date; payment-confirmation emails mark the matching task paid, and
  unclear ones wait for review. See [Bills](#bills-账单待付-email-driven-bill-schedules).
* **New transaction form 记一笔**
  * **Quick add → form:** next to *Add* in the header box (and on the Review page) there is **✎ Review / Open in
    form**. It parses your text without saving and opens the form pre-filled: type, amount, currency, account
    (From/To for transfers), category, merchant, member, tags, date and note. Fields the parser was unsure about are
    highlighted in orange ("⚠ check"). *Add* still saves straight away as before.
  * In the quick-add box, **Enter** = Add and **Shift+Enter** = open the form. You can swap them in Settings →
    General (*Quick-add box: pressing Enter…*).
  * *To review* items (✎ Review) open in the same form, with missing amount/account/category highlighted.
  * **Save & add another 保存并继续** (or **Ctrl/⌘+Enter**) saves, shows a toast and clears amount, merchant,
    note, tags and attachment. It keeps type, account, date, currency, member and ledger, and puts the cursor back
    in Amount. **Enter in Amount** = Save.
  * **Remembers last used** account (From/To for transfers), category, currency and member per transaction type.
    These are stored on the server (`GET/PUT /api/prefs`, `POST /api/prefs/tx-last`, or `"remember": true` on
    `POST /api/transactions`), so phone and PC share them, with localStorage as a fallback. Choosing an account
    switches the currency to the account's currency.
  * Transfers show **From 转出账户 → To 转入账户**. *Amount received* only appears when the two currencies differ.
    Currency pickers show symbols ($ USD, ¥ CNY, ¥ JPY …).
  * On phones the form is full screen with large inputs (no iOS zoom) and a sticky button bar.
  * Dry-run quick-add (`{"dry_run": true}`) now also returns `prefill` and `uncertain`.
* **Reason for difference 差额原因** (on every balance adjustment: Set balance, the account Edit form, the
  *Balance adj.* type in the transaction form, quick-add `balance …` and the API `reason` / `adj_reason` field)

  | Reason | Reports (income / expense) | Investment income 投资收益 | Investment returns |
  |---|---|---|---|
  | Investment gain/loss 投资收益/亏损 (labelled gain or loss by sign) | excluded | **yes** (+/−) | part of the gain |
  | Interest 利息 | excluded | **yes** | part of the gain |
  | Fees 费用 | **expense** on normal accounts; on investment accounts it reduces investment income instead | investment accounts only (−) | part of the gain (net of fees) |
  | Unrecorded spending 未记支出 | **expense**, labelled "Unrecorded spending 未记支出 (balance adj.)" | – | money out |
  | Unrecorded income 未记收入 | **income**, labelled "Unrecorded income 未记收入 (balance adj.)" | – | money in |
  | Correction/other 校正/其他 (default; also every older adjustment) | excluded, as before | – | money in/out |
  | Opening balance 期初余额 | excluded | – | money in |

  * The default is Investment gain/loss on brokerage, retirement and crypto accounts, and Correction/other
    everywhere else. You can change the reason later on the transaction.
  * Reasons are stored as sub-categories of *Balance adjustment 余额调整* (built in, can't be deleted).
  * Investment income = gain/loss + interest + dividends − investment fees. It is shown on the dashboard
    (Balance card and trend chart) and in Reports, separately from income and expense.
  * Quick-add takes a reason word: `balance brokerage 52000 gain`, `余额 apple save 10250 利息`,
    `balance checking 830 未记支出`.
* **Investment returns 投资收益** (Investments page)
  * Covers each investment-type account (brokerage, retirement, crypto), each group, and *All investments*.
  * Periods: this month, year to date, last 12 months, all time, or custom. Values are in the base currency at
    today's FX rates.
  * **Net contributions** = money in − money out during the period. Money in/out means transfers from/to other
    accounts, income/expenses recorded on the account, and adjustments classified Correction, Opening balance or
    Unrecorded. Transfers between accounts of the same group are not counted in the group/total rows. Buys and
    sells only move money between cash and holdings, so they are not contributions.
  * **Gain** = end value − start value − net contributions. This equals investment gain/loss adjustments + interest
    + dividends − fees + market gains on tracked holdings (shown as a breakdown).
  * **Simple return** = gain ÷ (start value + net contributions).
  * **Money-weighted return (Modified Dietz)** = gain ÷ (start value + Σ flowᵢ × wᵢ), where wᵢ is the share of the
    period left after flowᵢ.
  * **Annualized** = (1 + Modified Dietz)^(365/days) − 1, for periods longer than a year.
  * **XIRR** is the yearly rate r that solves −start − Σ flowᵢ/(1+r)^tᵢ + end/(1+r)^T = 0.
  * The page also shows a cumulative-gain chart and per-year returns for the selected row (click a row).
  * API: `GET /api/investments/returns?period=ytd|month|1y|all|custom&start=&end=&scope=all|group:<id>|account:<id>`.
  * Past values of tracked holdings use the last trade price before that date. Today's value uses the latest quote.
* **Reclassify adjustments** (Investments → 🏷 Reclassify adjustments…), a one-time tool for imported 随手记
  余额变更:
  * It lists the unclassified adjustments on investment accounts (optionally also savings accounts → Interest) and
    proposes a reason. An account's *first* record is proposed as Opening balance, because exports have no
    starting balances; calling it a gain would inflate returns.
  * You can tick or untick rows and change any reason. **Nothing changes until you press Apply**, and a backup
    is taken first.
  * API: `GET /api/adjustments/reclassify[?include_savings=1]` (read-only preview);
    `POST /api/adjustments/reclassify {"items":[{"id":..,"reason":..}]}`.
* **Scheduled transactions 周期记账** (sidebar → *Scheduled 定时*)
  * Schedules can create any transaction type (expense, income, transfer with from/to and received amount,
    refund, balance adjustment with reason, investment trades). The editor is the same form as *New transaction*,
    plus a **Repeat** section.
  * Repeat options: once, every N days, weekly on chosen weekdays / every N weeks, monthly on day X (or *last
    day*; short months use their last day), monthly on the Nth weekday (1st–5th or last), every N months,
    quarterly, yearly (Feb 29 → Feb 28 in other years).
  * Each schedule has a start date, an optional end date *or* number of times, and a time of day.
  * *Post automatically* or *Create in To review*. **Variable amount** always goes to To review, with the
    amount as a suggestion.
  * The editor shows the next 5 dates. The page and the dashboard list everything due in the next 30 days.
  * **🔁 Make recurring** on any existing transaction creates a schedule from it. It starts one period later, so
    the original isn't duplicated.
  * Pausing skips the dates missed while paused. Deleting a schedule can keep or delete the transactions it
    already created. Editing a generated transaction never changes the schedule.
  * **Runner.** The app's built-in background job runs once a day at `schedule_run_time` (default 00:15,
    `timezone` default America/Los_Angeles; DST-safe) and once at server start. It creates every occurrence due
    up to today, catching up after downtime for at most `schedule_catchup_days` (default 90). Older ones are
    skipped and counted in the summary.
  * Generated transactions have source `scheduled` and `schedule_id`.
  * It is idempotent: each (schedule, date) is recorded in `schedule_occurrences` in the same database
    transaction, so re-runs never duplicate, and a generated transaction you delete is not re-created.
  * **Run now** / **Preview run** buttons and a run log (time, trigger, created, errors) are on the page.
  * API: `GET/POST /api/schedules`, `GET/PUT/DELETE /api/schedules/{id}` (`?keep_history=false` also deletes
    generated transactions), `POST /api/schedules/{id}/pause|resume`, `POST /api/schedules/preview`,
    `GET /api/schedules/upcoming?days=30`, `POST /api/schedules/run {"date"?, "dry_run"?}`,
    `GET /api/schedules/runs`, `GET /api/schedules/from-transaction/{tx_id}`.
  * CLI, for cron on a machine where the server isn't always running:
    `python -m app.cli run-schedules [--date YYYY-MM-DD] [--dry-run]`, e.g. crontab
    `15 0 * * * cd /path/to/finance-app && FINANCE_DB=data/finance.db .venv/bin/python -m app.cli run-schedules`.
    On this machine the in-app job is the trigger (there's no cron).
* **Investments:** holdings from your trades, with average cost, unrealized/realized P/L and dividends.
  Quotes come from Yahoo (chart API, with yfinance as a fallback). If a quote can't be fetched, you can set a
  manual price.
* **Budgets:** monthly budgets, total or per category, with progress bars and "copy last month".
* **Dashboard & reports**
  * Dashboard: month income/expense/balance, spending pie, 12-month income-vs-expense chart, net-worth history,
    balances, budgets and recent activity.
  * Reports: group by category, member, merchant, project or account over any date range.
* **Capture:** quick-add (free text, EN/中文), the email ingest endpoint, IMAP Email Monitoring and a Telegram
  bot. Anything uncertain goes to the **To review 待确认** inbox, where you approve or edit it.
* **Imports**
  * Suishouji (随手记) `.xls`, `.xlsx` and `.csv` exports.
  * Bank/card CSVs (Chase, Amex, BofA, Capital One, Citi, Discover, Alipay, WeChat) or any CSV via column mapping.
* **Backups:** download, restore and scheduled snapshots (30 kept), with an optional rclone upload to Google Drive.
* **Demo data:** Settings → Demo data can load clearly labeled `[DEMO]` sample data and remove it again in one
  click. The app ships with no data.

## Running
```bash
./start.sh        # creates .venv on first run, then starts in the background with nohup
./stop.sh
tail -f data/logs/server.log
```
Settings come from environment variables or an optional `.env` file next to the app (see `.env.example`). The app reads `.env`
itself, so this works the same on Linux and Windows; `start.sh` also reads it.
* `HOST`: the address(es) to listen on, comma separated. The app default is `127.0.0.1`; `start.sh` defaults to
  `127.0.0.1,xxx.xxx.xxx.xxx` on this server. Any address that can't be bound is skipped with a warning. `0.0.0.0` is allowed,
  but it logs a warning: prefer explicit addresses.
* `PORT` (default `8088`)
* `DATA_DIR`: where `finance.db`, backups, logs, attachments and `secret.key` go. The default is `./data`; relative paths are relative to the app folder.
* `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`
* The older names `FINANCE_HOSTS`, `FINANCE_PORT` and `FINANCE_DB` (the full database path) still work and take priority.

**Windows:** see [RUN_ON_WINDOWS.md](RUN_ON_WINDOWS.md). It covers `setup.ps1`, `start.ps1` and `stop.ps1`, double-click
`start.bat` / `stop.bat`, and the optional `install-service.ps1` boot task. No Node.js or WSL is needed.

`watchdog.sh` restarts the server if it isn't running. To use it, put this in your crontab:
`*/5 * * * * /workspace/finance-app/watchdog.sh`

**Docker (optional):** run `docker compose up -d --build`. The compose file publishes the port only on
`127.0.0.1:8088` and `xxx.xxx.xxx.xxx:8088`, and `./data` is mounted into the container.

### Password & admin CLI
```bash
.venv/bin/python -m app.cli reset-password   # back to the first-run /setup page
.venv/bin/python -m app.cli show-api-key
.venv/bin/python -m app.cli clear-demo
.venv/bin/python -m app.cli backup           # snapshot now (+ upload if rclone is configured)
```
You can also change the password in Settings → Password.

## Security
* There is one user. The password is hashed with PBKDF2-SHA256 (390k iterations). Sessions are signed cookies,
  and changing the password invalidates old sessions. Login attempts are rate-limited.
* Every page and API call requires either a logged-in session or the header `X-API-Key: <key>`. The key is
  generated on first run, shown in **Settings → API & Bots** (where it can also be regenerated), and written to
  `data/API_KEY.txt` (mode 600).
* IMAP app passwords are encrypted with Fernet using `data/secret.key` (mode 600) and are never sent back to the
  browser; only a mask is shown. If you lose `secret.key`, you'll need to re-enter the mailbox passwords.
* Keep `data/` private. It holds the database, keys and backups.

## API (curl examples)
```bash
KEY=$(cat data/API_KEY.txt); H="X-API-Key: $KEY"; B=http://127.0.0.1:8088

curl -s -H "$H" $B/api/accounts                      # accounts, balances, net worth
curl -s -H "$H" "$B/api/transactions?q=coffee&start=2026-10-01&limit=50"
curl -s -H "$H" -X POST $B/api/transactions -H 'Content-Type: application/json' \
  -d '{"kind":"expense","date":"2026-10-11 12:30","amount":12.5,"account_id":1,"category_id":12,"note":"lunch"}'
curl -s -H "$H" -o tx.csv "$B/api/transactions/export.csv?start=2026-01-01"
curl -s -H "$H" $B/api/review                         # items waiting for review
curl -s -H "$H" -X POST $B/api/transactions/42/approve
curl -s -H "$H" "$B/api/dashboard?month=2026-10"
curl -s -H "$H" "$B/api/reports?group_by=category&start=2026-01-01&end=2026-12-31"
curl -s -H "$H" -X POST $B/api/backups               # snapshot now
curl -s -H "$H" -o finance.db $B/api/backups/download
```
Interactive API docs are at `/api/docs` (you need to be logged in).

### Set balance — `POST /api/accounts/{id}/set-balance`
```bash
curl -s -H "$H" -X POST $B/api/accounts/3/set-balance -H 'Content-Type: application/json' \
  -d '{"balance": 5230.00, "date": "2026-10-11 09:00", "note": "matched bank app"}'
```
* `balance` is the real balance. For liability accounts it is the amount owed, unless you send `"as_owed": false`.
* `date` is optional. The default is now; a date on its own means the end of that day.
* The response gives `created`, `previous`, `difference`, `message` and the `transaction` (or `null` when nothing
  changed).
* `PUT /api/accounts/{id}` with `{"balance": …}` does the same thing.
* Quick-add also works: `余额 支付宝 1234.5`, `balance checking 5230`, `balance amex 820` (owed),
  `bal cash 40 yesterday`. Email ingest never sets balances.

### Quick-add — `POST /api/quick-add`
```bash
curl -s -H "$H" -X POST $B/api/quick-add -H 'Content-Type: application/json' -d '{"text":"lunch 25.5 chase sapphire"}'
curl -s -H "$H" -X POST $B/api/quick-add --data-binary '午饭 35 支付宝'      # text/plain body also works
curl -s -H "$H" -X POST $B/api/quick-add -H 'Content-Type: application/json' -d '{"text":"dinner 40","dry_run":true}'
```
Optional body fields:
* `source`: one of web, api, email, bot, message
* `dry_run`: parse only, don't save
* `review`: always send to the review inbox
* `ledger_id`

The response contains `status` (`posted` or `review`), `confidence` (0–1) and the transaction.

Every response also has a short top-level **`message`** that Siri can read aloud. It is in Chinese when the input
is Chinese:

| Case | `message` |
|---|---|
| posted | `Saved: Coffee $4.50 · Cash` · `已记: 午饭 ¥35 · 支付宝` |
| review | `Needs review: lunch $25 (account not found)` |
| transfer | `Transferred $300 Checking → Amex` |
| `balance …` / `reconcile …` | `Balance set: Alipay ¥1,234.50` · `Balance unchanged: …` · `Reconciled: …` (the long explanation is in `detail`) |
| `dry_run` | `Preview: Coffee $4.50 · Cash` · `Preview: balance Amex → owed $250` |
| error (HTTP 400) | JSON `{"detail": "...", "message": "Couldn't save: …", "ok": false}` |

The body can be JSON (`{"text": ...}`, any `application/json; charset=…`; the `text` key is case-insensitive;
`dry_run`/`review` accept `true` or `"true"`), a form or plain text. If text is sent with a JSON content type, it
is treated as the text. `POST /api/accounts/{id}/set-balance` is separate and keeps its own `message`.

**iPhone / Apple Watch Shortcut:**
1. Use *Dictate Text* (or *Ask for Input*).
2. Add *Get Contents of URL* with:
   * URL `http://<tailscale-ip>:8088/api/quick-add`
   * Method POST
   * Header `X-API-Key: <key>`
   * Request Body JSON with `text` = the dictated text
3. Add *Get Dictionary Value* `message`, then *Show Result* / *Speak Text*.

**Syntax.** Write a free-form line containing an amount, plus optionally an account, a category word, a date,
a merchant and #tags. Order doesn't matter.

| Example | Result |
|---|---|
| `lunch 25.5 chase sapphire` | expense 25.50, Meals, Chase Sapphire |
| `午饭 35 支付宝` | expense ¥35 on Alipay (account currency) |
| `coffee 4.5 sapphire yesterday #work` | yesterday, tag `work` |
| `uber 18 amex 10/3` · `打车 23 微信 10月3日` | explicit dates (`YYYY-MM-DD`, `M/D`, `X月Y日`, `yesterday/昨天/前天`) |
| `groceries 86.20 at Whole Foods amex` | merchant from `at …` / `@ …` / `在…` |
| `salary 5000 to checking` · `工资 8k 招行` | income (from category keywords) |
| `paid 300 from checking to amex` · `转账 500 支付宝 到 招行` | transfer (`from … to …`, 转/到) |
| `refund 20 amazon sapphire` · `退款 15 支付宝` | refund |
| `rent 2.1k checking` · `房租 1.2万 招行` | multipliers k / w / 万 / 千 |
| `taxi 300 CNY wechat` · `¥88 晚饭 微信` | explicit currency (USD/$, CNY/RMB/¥/元, EUR/€ …) |

**How accounts are matched:** by account name, alias (Accounts → Edit → Aliases, e.g. `sapphire, csp`) or card
last-4 digits. If no account is given, the default account (Settings → General) is used.

**Confidence score:** amount 0.40, account 0.30, category 0.25, plus small bonuses. Anything below the review
threshold (Settings → General, default 0.75) goes to **To review**. A line with no amount or no account always
goes to review.

### Email ingest — `POST /api/ingest/email`
```bash
curl -s -H "$H" -X POST $B/api/ingest/email -H 'Content-Type: application/json' -d '{
  "from":"alerts@chase.com","subject":"Your $18.40 transaction",
  "body":"You made a $18.40 transaction with SAMPLE COFFEE on your card ending in 4242.",
  "message_id":"<optional-unique-id>","received_at":"2026-10-11T08:10:00","auto_post":false}'
```
The same parser is used for IMAP mail:
1. Parsing rules are tried by sender. If no rule matches, a generic extractor runs.
2. The account is matched by card last-4, then by name, then the mailbox's default account.
3. Messages are deduplicated by Message-ID.
4. The result goes to review, or is posted directly when `auto_post` is true and confidence is above the
   threshold.
5. Card payment notices always go to review as transfers.

This endpoint is handy for forwarding mail from Gmail filters, Zapier, iOS Shortcuts and similar tools.

### Email Monitoring (IMAP) — Settings → Email Monitoring 邮件监控
* **Add mailbox:** fill in host, port, SSL, username, **app password** (encrypted at rest), folder, polling
  interval, enabled, sender allowlist, subject keywords, after-processing action (mark read, or move to a
  folder), default account and auto-post.
  * Gmail: `imap.gmail.com:993`. Create the app password under Google Account → Security → 2-Step Verification
    → App passwords.
  * Outlook: `outlook.office365.com:993`.
  * QQ / 163: enable IMAP in their web settings and use the authorization code they give you (授权码).
* **Buttons:** *Test connection* (lists folders and the unread count) and *Run now*. A background poller checks
  each enabled mailbox at its interval and only fetches recent mail (`SINCE` lookback).
* **Parsing rules 解析规则:** each rule has a sender regex, an optional subject regex, and regexes for amount,
  merchant, last-4 and date, plus a kind and priority. Presets are included for Chase, American Express,
  Bank of America, Citi, Capital One, Discover, Alipay 支付宝 and Chinese bank-card alerts (招行/工行/建行… 动账提醒).
  The presets are best effort; check them with **Test with a sample email**, then edit or add your own.
* **Ingest log 处理日志:** shows every message processed and its result: posted, review, duplicate, skipped or
  error.

### Suishouji (随手记) import — Settings → Import 导入
1. **Export from Suishouji** using one of these:
   * Web: 新功能 → 导入导出 → 数据导出 → 随手记 web版 (`.xls`, one sheet per type)
   * App / 神象云账本: 设置 → 数据导出 (`.xlsx`)
   * App CSV: 设置 → 高级功能 → 备份与同步 → 备份与恢复 → 本地备份与恢复 → 导出Excel文件(CSV)
2. **Upload and preview.** Sheets and columns are detected by header name, with synonyms for older or alternative
   layouts. Any column can be remapped by hand.
3. **Map each Suishouji account** to an existing account, or create a new one. The type is guessed (信用卡 →
   credit card, 支付宝/微信 → e-wallet, 储蓄卡 → checking, 负债/借入 → payable, 债权/借出 → receivable…).
   Categories (一级 › 二级) are mapped or created the same way. Members, merchants and projects are created
   automatically.
   The preview also shows each account's **net from file**. It flags accounts that look wrong from the file alone,
   such as an asset that ends up negative or a liability that ends up in credit. Suishouji exports don't include
   accounts' initial balances, so you can type the balance Suishouji shows today (for cards and loans, the amount
   owed) in **Balance in 随手记 today**. The opening balance is then set so the imported balance matches.
   Currencies without an exchange rate are listed too.
4. **Import.** A backup is taken automatically first.
   * 转账 (transfer) rows are imported as transfers, and the app CSV's 转出/转入 pairs are joined by 关联Id.
   * 余额变更 becomes a balance adjustment.
   * 负债变更 and 债权变更 become adjustments on payable/receivable accounts.
   * 借入/借出/收债/还债/代付/报销 become transfers. Their direction is best effort, so check the preview.
   * Rows already imported (same date + amount + account + note) are skipped when **Skip duplicates** is on.
   * The import summary shows counts by type, totals, duplicates, skipped rows and created accounts.

Format research and sources are in `docs/sui_reference.md`.

### Bank CSV import
Use Settings → Import → Bank / card CSV.
* Known formats are recognized by their headers. For anything else, map date, amount (or separate debit/credit
  columns), description, category, and the sign convention.
* Card payments are imported as transfers into review.
* Re-importing the same file skips duplicates.

## Telegram bot (optional)
1. Message @BotFather, run `/newbot`, and copy the token.
2. Put `TELEGRAM_BOT_TOKEN=...` in `.env` and run `./stop.sh && ./start.sh`.
3. Message your bot. It replies with your chat id because you're not authorized yet. Add that id to `.env` as
   `TELEGRAM_ALLOWED_CHAT_IDS=<id>` and restart.
4. From then on, every message is treated as quick-add, e.g. `lunch 25.5 sapphire`. The bot replies with what it
   recorded, or tells you the item went to review.

The bot uses long polling, so it needs no public URL. Status is shown in Settings → API & Bots.

## Backups & Google Drive
* Settings → Backup 备份 has buttons to snapshot now, download the live DB, download or restore any snapshot,
  and upload a `.db` to restore. Every restore takes a safety backup first.
* Scheduled snapshots run every `backup_interval_hours` (default 24) into `data/backups/`, and the newest
  `backup_keep` (default 30) are kept.
* **Google Drive via rclone (optional):** the app never signs in to Google itself. Set it up once on this machine:
  ```bash
  rclone config            # n) new remote → name: gdrive → storage: drive → follow the prompts
                           # on a headless box answer "n" to auto config and run `rclone authorize "drive"`
                           # on a computer with a browser, then paste the token back
  rclone mkdir gdrive:FinanceAppBackups
  ```
  Once a remote named `gdrive` exists, each scheduled backup is also copied to `gdrive:FinanceAppBackups`.
  Settings → Backup shows the rclone status and has an *Upload latest now* button.
* **Backup folder:** `backup_dir` (Settings → Backup) is an absolute path, e.g. a NAS or external disk; blank means `data/backups`.
  **Receipt photos are included** (`backup_receipts`, on by default). They are copied into `<backup folder>/receipts`, new/changed files only, even when the receipts folder is outside the data folder.
* **Incremental off-site backup to Google Drive** (Settings → Backup → ☁️, **off by default**):
  * *Synced folder:* point it at a Google Drive for desktop folder (e.g. `G:\My Drive\FinanceBackups`). Each run adds one snapshot to `<folder>/db/` and copies only new/changed photos to `<folder>/receipts/`; Drive uploads them.
  * *rclone:* `rclone copy` to `<target>/db` and `<target>/receipts` (only changed files transfer), e.g. target `gdrive:FinanceAppBackups`.
  * Schedule: every N hours, or fixed times (HH:MM in the app time zone) on chosen weekdays, like the email-check schedule. A run missed while the app was down runs once on restart.
  * Retention: the newest N snapshots (default 14) stay in the destination; receipt photos there are never deleted.
  * Settings shows last run, status, details and next run. *Run now* runs it immediately. `GET/PUT /api/offsite`, `POST /api/offsite/run`.
  * **Restore:**
    1. Download the newest `db/finance-….db` from Drive (or `rclone copy gdrive:FinanceAppBackups/db/<file> .`).
    2. Upload it in Settings → Backup → Restore; the current DB is backed up first.
    3. Copy the Drive `receipts/` folder back into your receipts folder.

## Email: SMTP, reminders and reports 邮件报表 (all off by default)
* **Settings → Email reports & SMTP:** host, port, security (STARTTLS 587 / SSL 465 / none), user, **app password** (stored encrypted with `data/secret.key`, like IMAP passwords; never returned by the API), From, and a comma-separated To list.
  *Send test email* checks the settings. For Gmail: `smtp.gmail.com`, 587, STARTTLS, plus an app password from Google Account → Security → App passwords.
* **Reminder emails:** at your times/weekdays, a list of the reminders inside their lead window (`reminder_lead_days` or per reminder) plus overdue ones. If nothing is due, no email is sent.
* **Reports:**
  * Daily covers yesterday; weekly covers the 7 days up to yesterday, sent on chosen weekdays; monthly covers the previous month, sent on a chosen day of the month (31 = last day).
  * Each report has income/expense/net, top categories, tax & tips, investments (net principal added vs returns), reminders due, reimbursements outstanding and projects vs budget.
  * HTML with a plain-text part, in `email_lang` (English, 简体中文, 繁體中文). *Preview* opens the HTML in the browser.
* Each job shows its schedule, last sent/status and next run. Every slot is sent once, even with several app instances.
* API: `GET/PUT /api/email/settings`, `POST /api/email/test`, `GET /api/email/preview?kind=daily|weekly|monthly|reminders&lang=`, `POST /api/email/send?kind=`.
* Tests use an in-process fake SMTP server (`tests/fake_smtp.py`); nothing is sent to the internet.
* **Test email fails?** The error says why, with likely fixes. Common causes:
  * a timeout means the wrong host/port, or a firewall/VPN/ISP blocking outgoing SMTP; try the other port;
  * port 465 needs SSL/TLS and port 587 needs STARTTLS;
  * Gmail needs an app password, not your normal password.

  Gmail: `smtp.gmail.com`, 587 + STARTTLS (or 465 + SSL/TLS), user = your Gmail address, password = the 16-character app password. The timeout is configurable (`smtp_timeout`, 5–60 s, default 15).

## Tests
```bash
.venv/bin/python -m pytest -q
```
The tests cover:
* the quick-add parser (EN/中文)
* ledger math, FX conversion, investments and the API (auth, API key, CRUD, CSV import/export, backups)
* email rules, using fake sample alerts for every preset
* expiring credits (window and expiry logic, use and partial use, reminders) and email-driven bills (parsing, task
  creation, reminders, payment matching including no-match and ambiguous cases): `tests/test_credits.py`,
  `tests/test_bills.py`
* Suishouji import, using a synthetic workbook, a synthetic app CSV, and ezBookkeeping's MIT-licensed sample `.xls`
  files

## Layout
```
app/        main.py (routes), db.py (schema/seed), ledger.py (balances/reports), parser.py (quick-add),
            ingest.py (quick-add/email/IMAP), email_rules.py, importers.py (CSV + Suishouji), backup.py,
            quotes.py, telegram_bot.py, demo.py, serve.py, cli.py, config.py (env/.env), reconcile.py, returns.py, schedules.py,
            reminders.py, reserves.py, credits.py (expiring credits), bills.py (email-driven bills)
static/     SPA (index.html, login.html, style.css, js/, vendor/chart.umd.min.js)
tests/      pytest suite + fixtures
docs/       sui_reference.md
scripts/    screenshots.py (Playwright), win_common.ps1 (helpers for the Windows scripts)
setup.ps1 start.ps1 stop.ps1 install-service.ps1 *.bat   Windows scripts (RUN_ON_WINDOWS.md)
```

## Account reconciliation 对账

Compare an account with a bank/card statement and lock the balance at that date.

* **Accounts → ✔ Reconcile 对账** (or quick add `reconcile chase 1234.56` / `对账 支付宝 1000`, or `POST /api/accounts/{id}/reconcile`
  with `{balance, as_of, note, cleared_ids, adjust_reason}`). The dialog lists the transactions since the previous checkpoint with
  "cleared" checkboxes, the balance the app computes, the statement balance and the difference. A difference can be recorded as a
  balance adjustment with a *Reason for difference* (interest, fees, unrecorded spending…) or accepted ("Finalize anyway").
  Quick add only finalizes when there is no difference.
* Finalizing stores a **checkpoint** (account, statement date, balance, note) and flags every transaction of that account dated on or
  before it as reconciled (✔🔒 in lists; Transactions → filter "Reconciled 已对账").
* **Balance rule:** balance(t) = latest checkpoint at or before t + that account's transactions after it up to t. Charts, net-worth
  history and investment returns use the same rule (returns count the jump at a checkpoint as the "reconciliation" component).
  Transfers: each side follows its own account's checkpoints. Income/expense reports are unaffected.
* Adding, editing or deleting a transaction dated on/before a checkpoint does **not** change today's balance. The form warns, and with
  Settings → General → "Lock 🔒" the change needs a confirmation (API: `confirm_reconciled: true`). Edits that don't affect balances
  (note, tags, category, merchant) don't warn.
* **History** (click "✔ Reconciled …" under an account): per checkpoint the statement balance, **drift** (what the transactions now add up
  to from the previous checkpoint − statement balance) and **changed since** (effect of later edits), cleared/total count, and
  **Undo** for the latest checkpoint (un-flags its transactions; optionally deletes the adjustment it created).
* Set balance has an "Also mark as reconciled" checkbox (`reconcile: true`). Imported 随手记 余额变更 rows are normal adjustments, not checkpoints.

## Home 首页, account history 账户明细 and infinite scroll

* **Home 首页** (first in the sidebar, and the page you land on after login or at `/`):
  * Cards for the selected month's Income, Expense, Net (= income − expense) and Investment income. They follow the Reports rules.
    The ‹ Oct 2026 › switcher changes the month.
  * **Scheduled remaining 本月待发生定时** (Income, Expense and Net cards): for example "🔁 Scheduled remaining: $505.99
    (7 items)" with "Projected month-end: $3,296.46" (= actual so far + scheduled remaining). Hovering over the line
    (or clicking / Enter on touch and keyboard) lists the items: date, name and amount. The line only appears when
    something is left.
    **What counts as remaining:**
    * Every occurrence of an **enabled** schedule whose transaction kind is **expense** or **income**. Transfers,
      reimbursements and the investment kinds never count.
    * Its date runs from **today** (counted if it hasn't run yet) through the **last day of the month**. A future month
      counts from the 1st; a past month has nothing remaining.
    * It must be inside the schedule's start/end date and max count, and not before a resumed schedule's skip date.
    * It must **not be generated yet**. A generated occurrence is already in the actual numbers if it was posted, or in
      *To review*, so it's not counted twice. Paused schedules don't count.
    * **Remind-only** schedules count (they are bills you will pay) until that date's reminder is paid, done or
      dismissed.
    * Variable-amount schedules count their suggested amount. Without one they are listed but add 0.
    * Amounts are converted to the base currency with the same rates as the KPIs.
    * The stats rules match the KPIs: a reimbursable expense counts 0 and a reimbursement is never income.
    * The ledger filter applies (by the template's ledger, else the default ledger).
  * The quick-add box (Add / ✎ Review) and a **+ Detailed 详细记账** button for the full form.
  * Below that, the month's transactions, newest first. Scrolling keeps loading older months.
  * After you save anything, the cards and the list refresh.
  * The old Dashboard is unchanged and is now called **Dashboard 仪表盘**.
* **Account history:** click an account row (or **📜 History 明细**) on the Accounts page. It opens `#/account/<id>`:
  * The balance, plus Set balance / Reconcile / Edit, and **+ Add** pre-filled with the account.
  * A period filter (1M by default; 3M, 6M, 1Y, YTD, All, or a custom range), search, and a category filter.
  * Period opening and closing balances, inflow and outflow.
  * A **running balance** column. It's the account's real balance after each row and follows the reconciliation rule
    (latest checkpoint + later transactions). Filters hide rows but don't change the balances. For investment accounts it shows the cash part.
* **Infinite scroll** on Transactions, Home and account history. More rows load automatically as you scroll, with a
  "Load more" button as a fallback.
* **API:**
  * `GET /api/transactions?limit=50[&before=<next_cursor>]` pages by cursor, newest first (`date|id`); there's also `has_more`.
    The first page also returns `total` and `summary` (count, income, expense) for the whole filtered set. `offset=` still works.
    The default limit is now 50 (max 500); the CSV export is unlimited.
  * `GET /api/summary/month?month=YYYY-MM` gives income, expense, net, investment_income, prev/next and count.
    It also returns `scheduled` {from, to, currency, income, expense, net, income_count, expense_count, count,
    unknown_amount_count, items[{date, schedule_id, name, kind, amount, currency, amount_base, post_mode,
    variable_amount, account_name}]} and `projected` {income, expense, net}. `GET /api/dashboard` has the same
    `scheduled_remaining`.
  * `GET /api/accounts/{id}/history?period=1M|3M|6M|1Y|YTD|ALL` (or `start`/`end`) also takes `q`, `category_id`, `before` and `limit`.
    It returns items with `running_balance`/`running_shown`/`account_effect`, plus `balances` {opening, closing, inflow, outflow}.
  * New indexes: `transactions(date,id)`, `(account_id,date,id)` and `(to_account_id,date,id)`.

## Principal 本金投入 vs returns 投资收益 vs unexplained 未分类 (investment accounts)

Investment-type accounts (brokerage, retirement 401k/IRA, crypto) split their value into three parts:
**value 市值 = principal 本金 + returns 收益 + unexplained 未分类**.

* **Principal** = net contributions:
  * opening balance (minus the part marked as prior returns)
  * transfers in − transfers out (a transfer between two investment accounts moves principal from one to the
    other)
  * income deposited (payroll contributions; an employer match counts as a contribution and is labelled
    *Employer match 雇主匹配*)
  * refunds − withdrawals/expenses
  * adjustments with reason *Opening balance* or *Unrecorded income/spending*
* **Returns** = explicit returns only:
  * prior returns in the opening balance
  * adjustments with reason *Investment gain/loss*, *Interest* or *Fees* (negative)
  * dividends − investment fees
  * income in an interest/dividend category
  * trade gains on holdings (realized + unrealized = market value − buys + sells)
* **Unexplained 未分类** has two parts:
  * `unclassified`: balance adjustments with reason *Correction/other* or no reason, e.g. imported 随手记 余额变更.
  * `reconciliation_diff`: whatever else the ledger doesn't explain (reconciliation checkpoint jumps).

  `unexplained` = `unclassified` + `reconciliation_diff`. Unclassified adjustments are not counted as returns.
  Use **🏷 Reclassify** (the button on the account's Unexplained card opens it for that account) to give them a
  reason. This is computation only: nothing is changed in the data.
* **Return %** = returns / principal, and the period return (Modified Dietz) uses explicit returns only.
* **Overrides:** the transaction form has *Investment: counts as 计入* (Auto / Principal / Returns / Unexplained;
  the API also accepts `other` as an older name). The account form has *Prior returns in opening balance*
  (default 0, so the whole opening balance is principal).

**Account page:**
* Cards: Current value, Principal, Returns (amount and % of principal) and Unexplained (with a Reclassify button).
* Period figures for the selected filter: principal start → end, contributions/withdrawals, returns, and the
  Modified Dietz return.
* A chart of market value, cumulative principal, cumulative returns and unexplained. It follows the period filter
  (1M/3M/6M/YTD/1Y/3Y/All/custom), with automatic daily/weekly/monthly granularity or a manual choice.
* Each row has a 本金/收益/未分类/交易 badge, plus running Principal, Returns and Unexplained columns next to the
  running cash balance. On mobile these columns are hidden by default, with a toggle button.

**Accounts page:** investment accounts and groups show
`Principal $X · Returns +$Y (+Z%) · Unexplained $U` (Unexplained is omitted when 0; toggle: *principal &
returns*).

API:
```bash
curl -s -H "$H" "$B/api/accounts"                    # accounts[].invest {value, principal, returns, returns_pct,
                                                     #   unexplained, unclassified, reconciliation_diff, correction_count, ...}; tree[].invest for groups
curl -s -H "$H" "$B/api/accounts/7/history?period=1Y" # items[] add pr_class, pr_badge, running_principal,
                                                     #   running_returns, running_unexplained; first page adds invest {..., period, series}
curl -s -H "$H" "$B/api/accounts/7/investment-series?from=2026-01-01&to=2026-06-30&interval=week"
#   ?period=1M|3M|6M|YTD|1Y|3Y|ALL or from/to; interval=auto|day|week|month
#   -> {interval, points:[{date, principal, returns, value, unexplained}], summary:{start_principal, end_principal,
#       returns_change, start/end_unexplained, contributions, withdrawals, period_returns, return_pct (Modified Dietz)}}
curl -s -H "$H" -X PUT "$B/api/transactions/123" -H 'Content-Type: application/json' -d '{"pr_class":"returns"}'
curl -s -H "$H" -X PUT "$B/api/accounts/7" -H 'Content-Type: application/json' -d '{"opening_returns":2500}'
```


### Investments page: principal & returns 本金与收益
- The principal / returns / unexplained summary lives on **Investments**. The Accounts tree no longer shows it, though the API still returns the `invest` fields on accounts and groups.
- There is one row per investment account: value, principal 本金投入, returns 投资收益 (amount and % of principal), unexplained 未分类, and returns for the selected period (money-weighted %).
- The table has group subtotals in the base currency, totals by currency, and an overall total.
- A range filter (1M/3M/6M/YTD/1Y/3Y/All/Custom) drives a combined principal vs returns (vs value) chart. Each row opens the account detail inside Investments (`#/investments/account/<id>`): the Investments nav item stays highlighted, and the back button (or browser back) returns to the list with the same range and scroll position. Accounts page links still open `#/account/<id>`.
- The figures are as of the end of the range. When the range ends today they equal the account page header.
- `GET /api/investments/principal-returns?from=&to=&interval=auto|day|week|month&include_archived=` returns `accounts`, `groups`, `by_currency`, `total`, and `series`.
- The **Investment returns** report uses the same logic:
  - contributions/withdrawals are principal flows only;
  - gain = returns;
  - unclassified adjustments and reconciliation gaps appear in a separate `unexplained` column.
- The old ledger-flow figures are kept as `flow_contributions`, `flow_withdrawals`, `flow_net_contributions`, `flow_gain`, `flow_md_return`, and `flow_simple_return`.

## Reimbursements 报销

- Flag any **expense** as *可报销 reimbursable* (quick/detail/edit forms, or the 报销中心 page). Optional: payer 报销方, expected amount (defaults to the expense amount), note.
- Money coming back is a transaction of kind **`reimbursement` 报销**: an inflow to the chosen account (balances move normally). Enter it anywhere (form, quick add, import, API) or record it from the Reimbursement Center with the expenses already selected.
- **Linking 关联 (many-to-many, partial amounts).** A reimbursement can pay back several expenses and an expense can be paid back by several reimbursements. Each link has an amount:
  - it can't be more than the expense's **outstanding** amount nor the reimbursement's **unallocated** money (rejected with a message);
  - the default is the smaller of the two ("Fill outstanding" in the UI);
  - across currencies, the link stores both parts: the expense part (expense currency) and the reimbursement part (reimbursement currency).
- Link from either side: Reimbursement Center → 🔗 Link on an expense, or 🔗 Link expenses on a reimbursement; or in the transaction dialog (links box). After saving a new reimbursement in the form, the link dialog opens with suggestions (best match first: same amount, date close to/after the expense, same payer). Links show with ✎ edit and ✕ unlink.
- **Expense status:** reimbursed = sum of its links, outstanding = expected − reimbursed. **Open** (nothing reimbursed) → **Partial** → **Closed**. It closes by itself when outstanding reaches 0. **Close & write off** 核销 closes it by hand: the outstanding rest won't be reimbursed. **Reopen** undoes that. A reimbursement shows allocated / unallocated.
- **Stats rule** (home, dashboard KPIs, reports, budgets, projects, transaction list totals):
  - a reimbursement is never income (allocated or not);
  - a reimbursable expense counts **0** while it is open or partial;
  - once closed it counts **amount − reimbursed** (never below 0). So the reimbursed part never counts; only what was written off (or not expected back, when *expected* < amount) is a personal expense.
- The Reimbursement Center filters by status (Open / Partial / Closed chips; Closed hidden by default). The transaction list shows status badges, and `GET /api/transactions?reimb=open|partial|closed|unallocated` filters on it.
- Deleting a reimbursement or an expense removes its links (DB trigger, every delete path). Changing a reimbursement into another kind unlinks it; lowering its amount below what is linked, or moving a linked transaction to another currency, is refused.
- Quick add (including Apple Watch / Shortcuts): start with `报销 …` or add `#报销`, `#reimb`, or `#报销:Payer`, e.g. `报销 打车 32 支付宝` or `hotel 389 chase #报销:Acme`.
- The pre-existing *income* category "Reimbursement 报销" is unchanged. Old transactions in it stay income; use the new kind for new receipts.
- Upgrade: the new `reimb_links.alloc_amount` column is additive; existing links are back-filled once, and the database is copied to the backups folder (`finance-…-before-migrate.db`) before the schema changes.

API:
- `GET /api/reimbursements?status=open,partial (default)|closed|all&start=&end=&payer=&account_id=` returns items (with `reimb_state`, `reimb_settled`, `reimb_outstanding`, `reimb_counted`, links), counts per state, outstanding totals by currency / payer and unallocated totals. The older names `pending|settled|written_off` still work.
- `POST /api/reimbursements/receipts` records a reimbursement already linked: `{account_id, date, amount?, payer?, note?, items:[{expense_id, amount?}]}`. Money received beyond the items stays unallocated.
- `GET /api/reimbursements/receipts?unallocated=1` and `GET /api/reimbursements/receipts/{id}` include `reimb_allocated`, `reimb_unallocated` and `links`.
- `GET /api/reimbursements/links?expense_id=&reimbursement_id=` lists links.
- `POST /api/reimbursements/links` links (all or nothing). It takes `{expense_id, reimbursement_id, amount?, alloc_amount?}` or `{reimbursement_id, items:[{expense_id, amount?}]}` or `{expense_id, items:[{reimbursement_id, amount?}]}`.
- `PUT /api/reimbursements/links/{id}` takes `{amount?, alloc_amount?}`; `DELETE /api/reimbursements/links/{id}` unlinks.
- `GET /api/reimbursements/suggest?reimbursement_id=` (open expenses) or `?expense_id=` (reimbursements with unallocated money): best match first, with `default_amount`.
- `POST /api/reimbursements/{id}/close` / `.../reopen` (or `.../write-off` with `{undo?}`), and `POST /api/reimbursements/mark` with `{ids, reimbursable, payer?}`.
- CSV export adds `reimbursable, reimb_state, reimb_reimbursed, reimb_outstanding, reimb_counted, reimb_unallocated, reimb_links` (`#<other id>=<amount>; …`). The agent endpoint accepts `kind: "reimbursement"` with `payer` and `expense_ids` to link right away.

## Reserves 押金/预授权 (hotel and rental deposits, card holds)

IACF Personal Finance tracks money a hotel, rental company or landlord holds for you, from check-in to the refund. Open **🔒 Reserves** in the sidebar, or press **＋** and pick the **🔒 Reserve** tab.

- **Card hold 预授权 (pre-authorization)** is a **note only**. No transaction is recorded, so card balances, net worth, budgets, reports and every KPI ignore it. While it is open, the card shows **🔒 On hold $X** and its available credit is lower by that amount. At check-out you enter the **final bill**, which is recorded once as a normal expense:
  - lower than the hold: the rest is **released** (nothing is recorded);
  - higher: the difference shows as **Over hold**;
  - no charge at all: **Release** (or **Booking cancelled**).
- **Deposit 押金** is money that left your account and should come back. It is recorded as a **transfer** into the system account **Deposits 押金** (type receivable, one per currency, e.g. `Deposits 押金 (EUR)`). That account is created the first time you need it, never at startup. Net worth does not change and nothing counts as spending. At check-out, split what is left:
  - **Refund received**: a transfer back from Deposits to the account that got the money;
  - **Applied to the bill**: an expense paid from the Deposits account;
  - **Kept by the merchant** (damage, cleaning fee): an expense paid from the Deposits account;
  - **Extra charge on the card**: a normal expense.

  Only the applied and kept parts are spending. Interest paid on a deposit is ordinary income. A refund that came back as a *refund/income* (for example from an email) and is linked to a deposit is turned into a transfer from Deposits, so it is never income.
- **Foreign currency.** A deposit in another currency stores both amounts (EUR at Deposits, USD charged to the card). If it comes back at a different rate, enter the amount that arrived. A small difference (up to 3 % or 1 unit) is posted as an **FX adjustment** on the Deposits account (reason *Correction*, so it is excluded from income and expense), and the account returns to exactly 0. A larger difference is rejected.
- **Check out** 退房结算 is one step with a live **preview** (a dry run that saves nothing). The detail view shows the amounts and a **timeline**: check-in, deposit, charges, refund, kept part, FX adjustment, check-out, reminder and closed. Prepaid non-refundable bookings are not reserves: record them as ordinary expenses.
- **Linking** is many-to-many with partial amounts. One bill can cover two rooms, and a reserve can take several charges. Linking more than the remaining deposit, or more than the unlinked part of a transaction, is refused. Link from the reserve (🔗 Link transaction, with suggestions by merchant, card and dates) or from the transaction dialog (*Link to a reserve…*). Lists show a 🔒 badge. Deleting a transaction removes its links (DB trigger). A linked transaction keeps its kind and currency, and a whole-amount link follows an amount edit.
- **States:** Open → Partly settled → Settled up, or Released / Cancelled. Reopen undoes a close.
- **Reminder.** If a hold is not released or a deposit not refunded, a *reserve* reminder is due **7 days after check-out**, or **30 days after check-in** when there is no check-out date. Change the days at the bottom of the Reserves page (settings `reserve_release_days`, `reserve_hold_days`, 0–365). It shows in Reminders (🧳 Check out / Open reserve) and goes out with the email reminders/reports and the Telegram push like the other reminders. It completes by itself when the reserve closes. You can turn it off per reserve (*Remind me if it is still open after check-out*).
- **Email alerts.** Authorization wording ("authorization hold", "pre-authorization", "temporary hold", "预授权", "冻结", …), or a parsing rule of kind **Card hold → Reserve** (`hold`), creates a card hold that **needs review**. It appears in **Quick add & Review** and on the Reserves page, never as a transaction:
  - ✓ **Confirm hold** to track it;
  - **It's a real charge** turns it into an expense in the review queue.

  Any review item can also be converted with **🔒 Reserve**.
- **Upgrade:** the new tables `reserves` and `reserve_links`, the columns `accounts.system_role` and `reminders.reserve_id`, and a trigger are additive. The database is copied to the backups folder (`finance-…-before-migrate.db`) before the schema changes, and no rows are created.

API:
- `GET /api/reserves?state=open,partial (default)|settled|released|cancelled|all&method=hold|deposit&account_id=&project_id=&review=1` returns the items, with totals `on_hold` / `deposits` by currency and `state_counts`.
- `POST /api/reserves` takes `{method, account_id, amount, currency?, merchant?, title?, start_date, end_date?, category_id?, project_id?, ref?, note?, remind?, paid_amount?}`. `GET|PUT|DELETE /api/reserves/{id}`; `DELETE` needs `?unlink=1` when transactions are linked, and the transactions stay.
- `POST /api/reserves/{id}/settle` (check-out) takes `dry_run?` and either:
  - hold: `{final: {amount | transaction_id, account_id?, category_id?, date?}}` or `{release: true}`;
  - deposit: `{refund: {account_id, amount?, received?}, apply: {amount}, forfeit: {amount, note?}, charge: {amount, account_id?}}`.
- `POST /api/reserves/{id}/links` takes `{transaction_id, role?, amount?, alloc_amount?, final?}` or `{items:[…]}` (all or nothing); `DELETE /api/reserves/links/{id}` unlinks.
- `POST /api/reserves/{id}/release|close|cancel|reopen|confirm|to-transaction`, and `POST /api/review/{tid}/to-reserve` with `{method, …}`.
- `GET /api/reserves/suggest?reserve_id=` or `?transaction_id=`, and `GET /api/reserves/export.csv`.

Screenshots (FAKE `[DEMO]` data): `docs/screenshots/54_reserves_list.png`, `55_reserve_detail_timeline.png`, `56_reserve_checkout.png`, `57_reserve_reminder.png` (each with a `_zh` version).

## Credits 抵用金 (expiring refund / cancellation / bonus credits)

Open **🎟 Credits** in the sidebar. A credit is money a merchant gives you instead of cash, for example an airline credit
for a cancelled flight, store credit for a return, or a sign-up bonus. Like a card hold, it is **a note only**: it never
creates a transaction or changes a balance or report.

- **Fields:** account that holds it, source (`refund` | `cancel` | `bonus`), amount, remaining balance (defaults to the
  amount), currency (defaults to the account's), issued date, **expiry date**, title, reference and note.
- **Use:** **Use…** records a use. Leave the amount blank or enter all that is left to mark it **used**; enter less to mark it
  **partly used**. Each use is logged and can be undone (↶). **Close** gives up what is left (void); **Reopen** undoes that.
- **Status:** `active`, `partial`, `used`, `void`, or **`expired`** (money left and the expiry date has passed). Expired
  credits are flagged in red on the Credits page and in Reminders, and are not counted as *Available*. Recording a use on
  an expired credit needs a confirmation (`force: true`).
- **Reminders:** every open credit with money left has a *credit* reminder due on its expiry date. It shows from
  **6 months before** (*Credits → Remind me N months*, or Reminders → Settings; setting `credit_expiry_lead_months`, 0–60).
  It is in Reminders, on Home, in the iPhone notify text, the reminder emails and the Telegram push, listed for its whole
  window. After the expiry date it shows as *Expired* until the credit is used or closed, or you press ✓. It completes
  by itself when the credit is used up or closed, and comes back if the credit is reopened.
- **API:** `GET /api/credits?state=active,partial,expired (default)|used|void|all&account_id=&source=&expiring=1&months=`,
  `GET /api/credits/expiring?months=`, `POST /api/credits`, `GET|PUT|DELETE /api/credits/{id}`,
  `POST /api/credits/{id}/use` `{amount?, date?, note?, transaction_id?, force?}`, `DELETE /api/credits/{id}/uses/{use_id}`,
  `POST /api/credits/{id}/void|reopen`.

## Bills 账单待付 (email-driven bill schedules)

Open **📬 Bills** in the sidebar. Every email that goes through email ingest (IMAP monitoring or `POST /api/ingest/email`)
is checked:

- **Bill / payment request** (for example "Your X bill is ready", "amount due", "payment due", "invoice … due", "requested
  a payment", "please pay", 账单, 应还, 到期还款日, 缴费通知) → the parser reads the **payee**, **amount**, **currency**,
  **due date** and **reference** and creates a **payment task**. No transaction is recorded, because nothing has been
  paid yet. A second email for the same bill (same payee, due date and amount) is linked to the existing task instead of
  making a duplicate. A *bill* reminder is due on the due date and shows **3 days before** (setting
  `bill_reminder_lead_days`, 0–365, or *Remind days before* on one bill). A request without a due date is due on the day
  it arrived.
- **Payment confirmation** ("payment confirmation", "payment received", "thank you for your payment", "your payment of …
  was processed", 还款成功, 缴费成功, …) → matched against open tasks. A task matches only when all of these hold:
  - **payee:** the same biller (normalized names that are equal, contain each other, or share most words) or the same
    sender domain as the bill email;
  - **amount:** within **1.00 or 1 %** of the bill, whichever is larger (setting `bill_match_tolerance`). A task with no
    amount is never matched automatically;
  - **date:** paid between 3 days before the bill was issued and **10 days after the due date** (`bill_match_days`).
    Without a due date, the window is 45 days from the issue date.

  If **exactly one** task matches, it is marked **paid**, with the paid date, amount and the confirmation email's
  message-id and subject linked to it. **No match** or **several matches** are never guessed. The confirmation goes to
  **Bills → Payments to review**, where you pick the bill it pays or dismiss it. The email then continues through the
  normal flow, so a card payment still lands in *Quick add & Review* as a transfer. A confirmation from a payee with no
  open task at all, such as an ordinary purchase alert, is not queued.
- **By hand:** **＋ New bill**, **Paid** (optionally records the payment as an expense from an account), **Cancel**,
  **Reopen**, edit and delete. *Paid* on a bill reminder in Reminders does the same thing. **🔍 Test an email** shows what
  the parser reads without saving anything (`POST /api/bills/parse`).
- **Parsing rules:** a rule of kind **`bill`** or **`bill_paid`** forces an email from that sender to be treated as a bill
  or a payment confirmation. With `auto`, the wording decides.
- **Formats the parser understands:** plain-text and HTML bodies; amounts such as `$1,234.56`, `US$`, `€`, `£`, `¥`/`￥`,
  `123.45 USD`, `3456.78元` (a labelled amount like *Amount due / Total due / Payment amount / 应还金额* wins over the first
  amount found); due dates such as `2026-11-05`, `11/05/2026`, `11/05/26`, `11/05`, `November 5, 2026`, `Nov 5`,
  `5 November 2026`, `2026年11月5日`, `11月5日` after a label (*Due date, due on/by, pay by, 到期还款日, 还款日, 缴费截止*). A date
  without a year is the next one after the email date. US-style `MM/DD` order is assumed for numeric dates. The payee
  comes from a *Payee / Biller / Merchant / 收款方 / 商户* label, phrases like *Your X bill*, *bill from X*, *payment to X*,
  *X requested a payment*, or else the sender's display name or domain.
- **API:** `GET /api/bills?status=open (default)|paid|cancelled|all`, `POST /api/bills`
  `{payee, amount?, currency?, due_date?, issued_date?, account_id?, reference?, note?, lead_days?}`,
  `GET|PUT|DELETE /api/bills/{id}`, `POST /api/bills/{id}/paid` `{date?, create_payment?, from_account_id?, amount?}`,
  `POST /api/bills/{id}/cancel|reopen`, `GET /api/bills/payments?status=review|matched|dismissed|all`,
  `POST /api/bills/payments/{id}/match` `{bill_id}`, `POST /api/bills/payments/{id}/dismiss`, `POST /api/bills/parse`.

**Upgrading** (both features): the new tables `credits`, `credit_uses`, `bills` and `bill_payments`, the columns
`reminders.credit_id` and `reminders.bill_id`, and their indexes and triggers are additive. The database is copied to the
backups folder (`finance-…-before-migrate.db`) first, and no rows are created.

## Email rules: Balance adjust 调整余额 (statement balance)
- A parsing rule with **Kind = Balance adjust** (`kind: "balance"`) treats the extracted amount as the account's **new balance** (statement value), not a transaction amount.
- **Account:** matched by the last-4 regex (e.g. `Account: XXXXX1516` → 1516) against the account's last 4 digits.
- **What gets recorded:** the difference, as a balance adjustment using the same logic as Set balance, dated the email date.
  - The reason comes from the rule's `adjust_reason`: Gain/loss, Interest, Fees, Unrecorded spending/income, Correction/other, or Opening. Blank means the account default (gain/loss for investment accounts).
  - The reason flows into principal/returns as usual. For example, gain/loss counts as returns, and correction counts as unexplained.
- **Nothing recorded:**
  - When the difference is 0, nothing is recorded and the result is logged as `unchanged`.
  - An unknown account, or no amount found, is `skipped` and logged.
  - Duplicate emails (same Message-ID) are ignored.
- **Review mode:**
  - Without auto-post, or below the review threshold, the adjustment goes to the review inbox.
  - An email dated on or before the account's latest reconciliation always goes to review.
- **Tester:** the rule tester shows the adjustment it would record (`balance_preview`) without writing anything.
- **Template:** "From template" in Settings → Email Monitoring offers *Statement balance (Total Account Value)*. Templates are never inserted automatically, and they are saved disabled.
  - `amount_regex`: `Total Account Value\s*\$?([\d,]+\.\d{2})`
  - `last4_regex`: `Account:\s*X+(\d{4})`
  - `adjust_reason`: gain/loss
- **API:** `GET /api/email-rules/templates`. The `adjust_reason` field is on `/api/email-rules`.

## Email monitoring schedule 检查计划
- Each mailbox polls either **every N minutes** (`schedule_mode: "interval"`, the default; existing mailboxes keep it) or at **fixed times** (`schedule_mode: "fixed"`).
- **Fixed times:**
  - `schedule_times` takes one or more `HH:MM` in the app time zone (Settings → `timezone`), e.g. `08:00,21:30`.
  - `schedule_weekdays` is optional, using 0=Mon … 6=Sun; blank means every day.
- **Runner:**
  - It checks every 30 s and polls when a scheduled time arrives.
  - If the app was down at that time, it runs **once** when it comes back (the latest missed time only).
  - Each time is claimed atomically (`last_slot`), so it never runs twice.
  - Switching to fixed times, or changing them, doesn't catch up earlier times.
  - **Check now** still polls immediately and doesn't affect the schedule.
- **API:**
  - `GET /api/mailboxes` adds `schedule_text`, `next_run` (local `YYYY-MM-DD HH:MM`), `timezone`, `last_checked_at` and `last_slot`.
  - POST and PUT accept `schedule_mode`, `schedule_times` and `schedule_weekdays`.

## Receipt photos 小票 (local OCR)
- Use **📷** in the quick-add bar (or drop an image on it), or **Fill from photo 拍照识别** in the transaction form. On a phone it can open the camera; on a computer you can pick or drag in several images.
- The browser shrinks big photos before uploading. The server reads the text with **Tesseract** (eng + chi_sim), and the form opens pre-filled with merchant, date, total, currency and a guessed category/account (quick-add parser + merchant memory). **Nothing is saved until you press Save.**
- Saved photos are attached to the transaction and show as thumbnails in its detail view. When you delete a transaction, you choose whether its photos are deleted too or kept.
- **OCR runs only on your machine.** No cloud service is used. Without Tesseract, photos are still stored and you type the details yourself. `GET /api/receipts/status` reports what's available.
  - Linux: `apt install tesseract-ocr tesseract-ocr-chi-sim`. Windows: see RUN_ON_WINDOWS.md.
  - Optional, for better results on phone photos: `pip install opencv-python-headless`.
  - **Thermal paper** (most store receipts) fades and curls. Photograph it flat, in even bright light, without flash glare or shadows, filling the frame. Old faded receipts may still need manual fixes.
  - HEIC works when `pillow-heif` is installed. Otherwise use JPEG (iPhone: Settings → Camera → Formats → Most Compatible).
- **How the text is parsed:**
  - Total: an amount on a TOTAL / Amount Due / Grand Total / 合计 / 实付 / 应付 line, skipping subtotal/tax/tip/change/discount lines. Without such a line, the largest amount is used, at low confidence.
  - Subtotal / tax / tip / service charge (Subtotal/小计, Tax/Sales Tax/VAT/GST/税/税额, Tip/Gratuity/小费, Service charge/服务费) are read when printed. "Suggested tip" tables and "incl. tax" / "Tax ID" lines are ignored. When subtotal + tax + tip adds up to the total, confidence goes up. A tip written below the printed total is added to it. Values that aren't on the receipt stay empty (never 0). The form's Tax & tip fields are filled with what was found; a service charge counts as tip.
  - OCR runs several passes: autocontrast, denoise, and (with `opencv-python-headless` installed) deskew + CLAHE contrast + adaptive threshold + despeckle. Each uses Tesseract page modes 4/6/11 and a 2× upscale. The pass whose parsed result scores best wins (`parsed.ocr_pass` shows which); a clean read stops after one pass.
  - Dates: `2026-10-03`, `2026年10月3日`, `10/03/2026`, `Oct 3, 2026`, plus a time if one is printed.
  - Merchant: the first meaningful top line.
  - Currency: from ¥/￥/元/$/€/£.
  - The response includes a confidence score.
- **Storage:**
  - Default layout is `data/receipts/{yyyy}/{mm}/{dd}/YYYYMMDD_HHMMSS_<merchant>_<amount>_<id>.jpg`, using the receipt or transaction date (upload time when no date is found).
  - In Settings, `receipts_dir` sets the base folder (an absolute path, e.g. a NAS or synced Google Drive folder) and `receipts_pattern` sets the sub-folders, e.g. `{yyyy}/{mm}` or `{yyyy}-{mm}`.
  - The database stores paths relative to the base folder. When a transaction's date, merchant or amount changes, its files are renamed or moved to match.
  - Files under the data folder are copied into `backups/receipts` with each backup.
  - Limits: 15 MB per image, 6 images per receipt. Unconfirmed uploads are removed after 14 days.
- **API** (`X-API-Key` header):
  - `POST /api/receipts`: multipart `file` (repeatable), or JSON `{"image": "<base64>", "auto_post": false}`.
    - `auto_post=false` (default) returns `draft`, `receipt_ids`, `confidence`, `message`.
    - `auto_post=true` records the transaction, posted when confident or To review otherwise, with the photo attached.
  - `GET /api/receipts/{id}/image[?thumb=1]`, `POST /api/receipts/{id}/link {"transaction_id"}`, `GET /api/transactions/{id}/receipts`.
  - `POST /api/transactions` accepts `receipt_ids`. `DELETE /api/transactions/{id}?keep_receipts=true` keeps the photos.

### iPhone Shortcut: photo → transaction
1. **Take Photo** (or **Select Photos**). Optionally add **Convert Image** to JPEG, plus **Resize Image** to a width of 2000.
2. **Get Contents of URL**:
   - URL: `http://<your-server>:8088/api/receipts?auto_post=true`
   - Method: POST
   - Headers: `X-API-Key: <your key>`
   - Request Body: **File** set to the photo (simplest), or **Form** with a field of type **File** (any field name).
3. **Get Dictionary Value** `message`, then **Show Notification** (or **Speak Text**). You'll see something like "Saved: TEST MART $11.10" or "To review: …".

Leave out `auto_post=true` to get a draft only. To send base64 JSON instead, use **Base64 Encode** → JSON body `{"image": <Base64>}`.

The server recognises the image by its bytes. It accepts any form field name, a missing filename or content type,
`application/octet-stream`, a raw body, base64 text (form, JSON or plain), and JPEG/PNG/WebP/HEIC. Errors come back as
`{"ok": false, "message": "Receipt not saved: …"}`, so the notification says what went wrong.

**Troubleshooting:**
- An updated install that was started without re-running setup used to miss the new packages (Pillow / pytesseract /
  pillow-heif), and every upload failed. `start.bat` / `start.sh` now install them automatically when
  `requirements.txt` changes. Without Pillow, photos are still saved, just not read.
- `GET /api/receipts/status` (with the API key) shows whether OCR is available. The web 📷 flow also shows a warning when it is off.
- Every upload is logged in `data/logs/server.log` as `receipt upload: … bytes`.

## Reminder Center 提醒中心

* **Credit cards**: in the account form set *Statement day 账单日* and either *Due day 还款日* or *due N days after
  statement*. Optional: remind days, minimum payment %, autopay. Each closed cycle creates one reminder. It shows the
  statement date, due date and statement balance (amount owed at the end of the statement day, reconciliation honoured).
  You can edit the statement amount 账单金额, the minimum and the statement number.
  The reminder completes itself once transfers into the card after the statement date cover the amount. Until then it
  shows what is still owed.
* **Scheduled reminders**: in a schedule pick *When due → Remind only 仅提醒*. Works with one-time (`once`) and
  recurring schedules. It creates a reminder instead of a transaction. *Paid* can create the transaction from the
  schedule template.
* **Card expiry 信用卡到期**: in the credit card's account form set *Card expires (MM/YY)*. The card counts as valid
  through the last day of that month. The account list and the account page show *Expires MM/YYYY*, amber inside the
  reminder window and red once expired.
  * **When it shows:** from 60 days before the end of the expiry month (*Reminders → Settings → Card expiry: remind N
    days*, setting `card_expiry_lead_days`, 0–365). It says *Card X expires MM/YYYY*. It lists *Update the payment card
    for:* every active scheduled transaction charged to that card (template account = the card), including *Remind
    only* schedules. Paused and ended schedules are not listed. Each name opens the schedule editor. After the expiry
    date it is overdue (red) until resolved.
  * **How it stops:** each listed schedule is either **✓ Card updated** (for example the merchant's autopay now uses
    the new card, even if it stays the same account in the app) or **Move to account…** (the schedule now uses another
    account; open *Remind only* reminders of that schedule move with it). The card itself also has to be confirmed:
    **New expiry date…** (or entering a later expiry in the account form) marks it *renewed*; **Confirm card (closed /
    nothing to change)** acknowledges it; archiving the account counts too. When the card is confirmed AND no listed
    schedule is left unconfirmed, the reminder completes by itself (`done_reason=resolved`) and stops showing in the
    Reminder Center, the iPhone notify endpoint and reminder emails.
  * **✓ Done** closes it right away. While schedules are unconfirmed, the UI asks first and the API reply carries a
    `warning`.
  * **Next cycle:** each expiry has its own reminder. A new expiry starts a new cycle whose confirmations start from
    scratch, about 60 days before the new card expires. Correcting the expiry to an earlier month (a typo) or clearing
    it retires the old reminder.
  * **API:** `POST /api/reminders/{id}/schedules/{sid}/confirm` (`{"undo": true}` to undo),
    `POST /api/reminders/{id}/schedules/{sid}/reassign` `{"account_id": N}`, and `POST /api/reminders/{id}/card`
    `{"action": "renew", "expiry": "08/29"}` | `{"action": "acknowledge"}` | `{"action": "undo"}`. The account field is
    `card_expiry` (accepts `MM/YY`, `MM/YYYY` or `YYYY-MM`; stored as `YYYY-MM`).
  * **Upgrading** adds `accounts.card_expiry`, `reminders.expiry/card_state/card_state_at` and the
    `card_expiry_checks` table. These are additive changes, and a copy `finance-…-before-migrate.db` is written to the
    backups folder first.
* **Credits 抵用金 and Bills 账单待付** add the reminder kinds *credit* (expiring credit, from `credit_expiry_lead_months`
  before it expires) and *bill* (a bill from an email, `bill_reminder_lead_days` before it is due). See
  [Credits](#credits-抵用金-expiring-refund--cancellation--bonus-credits) and [Bills](#bills-账单待付-email-driven-bill-schedules).
* **Lead window**: reminders appear N days before the due date. The default is 10 (*Reminders → Settings*,
  `reminder_lead_days`). A schedule can override it with *Remind days before*, a card with *Remind days before due*.
  The *All upcoming* filter shows later items. The nav badge counts overdue items plus items inside their window.
* Actions: Paid (optionally records the payment transfer), ✓ done, Snooze, ✕ dismiss, Reopen, edit.
  A background job (about every 10 min) generates and auto-completes reminders. It is idempotent.
  Optional Telegram push (setting `reminder_notify=telegram`, off by default) sends one message per reminder when it
  enters its window. Reminder emails (SMTP): see "Email: SMTP, reminders and reports" above.
* API: `GET /api/reminders?view=open|all_upcoming|later|overdue|snoozed|done|all`, `PUT /api/reminders/{id}`, and
  `POST /api/reminders/{id}/paid|done|dismiss|snooze|reopen`.

### iPhone Shortcut: daily reminder notification

`GET /api/reminders/notify?days=7&include_overdue=true&lang=auto` (header `X-API-Key`). It returns
`{count, title, message, items}`, for example `"3 条提醒"` with one line per item: due date, name, amount and ⚠️ if
overdue. Items are only included inside their lead window. Card expiry reminders are listed during their whole window
(not only the next `days`), with a line such as
`💳 Visa ••1234 expires 11/2026 · Update the payment card for: Streaming, Gym`. Their items carry `expiry_label`,
`pending_names` and `detail`. Reminder emails include the same line.

1. Shortcuts → Automation → **+** → Personal Automation → **Time of Day** (e.g. 09:00, Daily) → Run Immediately.
2. **Get Contents of URL**: `http://xxx.xxx.xxx.xxx:8088/api/reminders/notify?days=7`, Method GET, Header
   `X-API-Key: <your key>`.
3. **Get Dictionary Value** `count` → **If** `count` *is greater than* 0:
   **Get Dictionary Value** `title` and `message` from the URL contents → **Show Notification** (title, message).
4. Optional: a second shortcut can call `POST /api/reminders/{id}/done` to tick an item off.

## Projects 项目

Group transactions across categories and accounts: trips, renovations, events.

* **Projects page** (nav 📁): name, description, start/end dates, optional overall budget and currency, icon, color,
  notes. You can archive or unarchive a project. Archived projects are hidden unless *Show archived* is ticked, and
  aren't offered in pickers or quick add. Their existing transactions stay assigned. Deleting a project only
  unassigns its transactions.
* **Assigning**:
  - Quick add: `#项目:日本旅行`, `#project:"Kitchen reno"` or `#p:kitchen`. An exact name, a unique prefix or a
    unique substring matches. An unknown or archived name is reported in the reply. Typing the project name without
    a tag still works as before.
  - The detail and edit form's *Project* picker.
  - *📁 Assign project* for the selected rows on the transaction list.
  - Filters: transaction list *All projects* filter, and the Reports *project* filter (`/api/reports?project_id=`).
* **Project detail** (`#/projects/{id}`): expense, income and net, budget left, and reimbursable 可报销 spending shown
  separately. Totals follow Reports: reimbursable expenses are excluded, reimbursements aren't income, and transfers
  don't count. It also shows spending by category (donut and table, click to drill into sub-categories), monthly
  income and expense, by account, person and merchant, a time-range filter and the transaction list (infinite scroll).
* **Budget plan 预算计划** by category: plan a parent category, a sub-category, or both. A parent rolls up its
  sub-categories, and a larger explicit parent amount wins. An optional *Other / uncategorized* line takes spending
  in categories without a plan.
  - The total is the project's overall budget if set, else the sum of the plan.
  - The detail page shows planned, actual, remaining and % used per line, with progress bars. Over-budget lines are
    highlighted, and categories with spending but no plan are listed.
  - *Copy from* another project, optionally scaled (e.g. ×1.2).
* **Suishouji import**: the 项目 column maps to projects (created on import).
* **API**:
  - `GET/POST /api/projects` (`include_archived`, `summary=false`)
  - `GET/PUT/DELETE /api/projects/{id}` (detail: `start`, `end`, `parent_id`)
  - `POST /api/projects/{id}/archive` (`{"undo": true}` unarchives)
  - `POST /api/projects/assign` `{ids, project_id|null}`
  - `GET/PUT /api/projects/{id}/plan` `{lines: [{category_id|null, amount, note}]}`
  - `POST /api/projects/{id}/plan/copy` `{from_project_id, scale}`

## Languages (i18n)

The web UI is English by default and also ships in 简体中文 (zh-CN) and 繁體中文 (zh-TW).

- **Which language is used:** the choice saved in this browser (the 🌐 picker in the sidebar, stored in `localStorage` and in the server pref `ui_lang`) > the browser's languages (`navigator.languages`; zh-Hant/TW/HK/MO → zh-TW, other zh → zh-CN) > English. A missing translation falls back to the English text.
- Dates, numbers and currency are formatted with `Intl` for the chosen locale. User data such as account and category names is never translated; a category shows its Chinese name (`name_zh`) in Chinese when it has one.
- **API reply `message` texts** (quick-add, receipts, `/api/reminders/notify`) use `?lang=` (`en`, `zh`, `zh-CN`, `zh-TW`) > the `Accept-Language` header > English. iPhone Shortcuts that want Chinese replies can add `?lang=zh`.

**Adding a language:**
1. Copy `locales/zh-CN.json` to `locales/<code>.json` (e.g. `ja`). Keys are the English UI strings; translate the values and set `_name` to the language's own name. Keep `{n}`-style placeholders.
2. Run `python scripts/i18n_build.py`. It writes `static/locales/<code>.js`.
3. Add `<script src="/static/locales/<code>.js"></script>` next to the other locale scripts in `static/index.html`.

`zh-TW` is generated from zh-CN with `python scripts/i18n_build.py --tw` (needs `pip install opencc-python-reimplemented`, a dev-only tool). Fixes go in `_overrides` in `locales/zh-TW.json`. New UI text should be plain English in the source; add its translation to the locale JSON files.

## Project plan lines

A project's budget plan is a list of lines you name yourself (for a trip: Flights, Hotel, Transportation, Meals, Tickets, Shopping...). Each line has a planned amount and currency, plus optional notes. A line can be mapped to one or more categories or sub-categories, or you can assign transactions to it directly.

- **Where each transaction counts (once):** the line it is assigned to; otherwise the first line mapped to its sub-category, then its top-level category; otherwise the line marked "catches the rest"; otherwise **Unplanned**.
- **Project page:** a plan-vs-actual table showing planned, actual, left, % used and an over-budget highlight. Click a line to see its transactions. Lines can be added, edited, reordered and deleted in place.
- **Templates:** Vacation, Home renovation and Event / Wedding. They suggest category matches from your own categories by name. You can also copy the plan from another project, optionally scaling the amounts.
- **Assigning a line:** pick it in the transaction form after choosing a project, or use quick add: `#p:日本旅行/Flights` or `#p:"Japan trip"/"Hotel"`. Unique prefixes work.
- **API:** `GET/POST /api/projects/{id}/plan-lines`, `PUT/DELETE /api/projects/{id}/plan-lines/{line}`, `POST …/plan-lines/reorder`, `POST …/plan-lines/template`, `POST …/plan-lines/copy`, `GET …/plan-lines/{line|unplanned}/transactions`, and `GET /api/plan-templates`.
- **Older category plans** (`/api/projects/{id}/plan`) were copied into lines once, one line per category. Their original rows are kept, and that endpoint keeps working: it mirrors its changes into the lines.

## Tax & tips 税/小费 (optional)
- **Amount is always the charged total.** Tax and tip are optional breakouts of it (Tax & tip box in the form, for expense/income/refund). Subtotal = amount − tax − tip. Blank means "not recorded" (stored as null, not 0), and tax + tip can't exceed the amount.
- Enter amounts or percentages. A tip % or tax rate is applied to the subtotal (`Tip %` fills the tip; `Tax rate %` fills the tax when it's empty).
- Quick add: `dinner 118 tip 18% chase`, `lunch 25 tax 2.10 tip 4 cash`, `午饭 100 小费 10 税 8 支付宝`.
- Reports → **Tax & tips**: tax, tips, tax % and tip % by month/category/merchant/project/account, with a chart. Home and dashboard show a Tax & tips card for the month when any were recorded. All of these sum only transactions where tax/tip was recorded. Tip % = tips ÷ subtotal of the tipped transactions.
- API: `tax_amount`, `tip_amount`, `tax_rate` on `/api/transactions` (null when blank); filters `has_tax=1|0`, `has_tip=1|0`; `GET /api/reports/tax-tips?start=&end=&group_by=month|category|subcategory|merchant|project|account&kind=expense|income`. CSV export adds `tax_amount, tip_amount, tax_rate, subtotal`.

## Agent API — `POST /api/agent/transactions`
One forgiving endpoint for AI agents, scripts and Shortcuts (`X-API-Key` header). It takes names instead of ids and answers with a short `message` plus the new `id`.

| Field | Notes |
|---|---|
| `account` | id, name, alias, or last 4 digits (`"4321"`). Omit to use the default account. `last4` also works. |
| `amount` | Charged total (a negative number is treated as spending). Optional when a readable receipt is attached. |
| `kind` | `expense` (default), `income`, `refund`, `transfer` (+ `to_account`), `reimbursement` (+ optional `payer`, `expense_ids` to link). |
| `date` | `2026-10-05` or `2026-10-05 12:30`; default now (or the receipt date). |
| `currency` | e.g. `CNY` when it differs from the account currency (converted like the form). |
| `merchant`, `note`, `tags` | Free text. |
| `category` | id or name (English or 中文), or `"Food > Dining"`. Unknown → uncategorized plus a warning. |
| `project`, `plan_line` | Project by id/name (a new name creates the project); plan line by id/name within it. |
| `tax`, `tip` | Optional: `2.50` or `"18%"` (of the subtotal). Omit to leave them empty. `tax_rate` optional. |
| `person` | Family member. |
| `reimbursable` | `true` to track it in Reimbursements. |
| `review` | `true` to put it in To review instead of posting. |
| `receipt` | Base64 image (JSON) or a file part (multipart, any field name). `receipt_ids` links already-uploaded receipts. |

```bash
# JSON
curl -s -X POST http://localhost:8000/api/agent/transactions -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"account":"4321","amount":58.40,"merchant":"Sample Bistro","category":"Dining","tax":4.10,"tip":"18%","note":"team lunch"}'
# → {"ok":true,"id":123,"message":"Recorded expense: Sample Bistro $58.40 (tax $4.10, tip $8.28) · Visa (#123)","transaction":{…},"warnings":[]}

# multipart with a receipt photo (amount/merchant/date/tax/tip are read from it when omitted)
curl -s -X POST http://localhost:8000/api/agent/transactions -H "X-API-Key: $KEY" -F account=Visa -F receipt=@receipt.jpg
```
Errors return HTTP 400 with `{"ok": false, "message": "..."}` (e.g. an ambiguous account lists the candidates). Add `Accept-Language: zh-CN` (or `?lang=zh`) for Chinese messages.

**Prompt hint for an agent:**
> To record a purchase, POST JSON to `/api/agent/transactions` with header `X-API-Key`. Required: `amount` (charged total) and `account` (name or last 4 digits). Add `merchant`, `category`, `date` (YYYY-MM-DD), `note` when known. Include `tax`/`tip` only if they appear on the receipt or the user said them; never guess them or send 0. For a photo, send it as `receipt` (base64) and omit fields you can't read. Show the user the `message` from the response; if `ok` is false, fix the field it names and retry once.

