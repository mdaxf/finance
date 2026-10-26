/* ---------- settings ---------- */
const STABS = [['general', 'General'], ['categories', 'Categories'], ['lists', 'Members · Merchants · Projects · Ledgers'], ['currencies', 'Currencies'],
  ['email', 'Email Monitoring'], ['import', 'Import'], ['backup', 'Backup'], ['notify', 'Email reports & SMTP'], ['api', 'API & Bots'], ['demo', 'Demo data'], ['security', 'Password']];
VIEWS.settings = async (v, rest) => {
  const tab = (rest && rest[0]) || 'general';
  v.innerHTML = `<div class="tabs">${STABS.map(([k, l]) => `<a href="#/settings/${k}" class="${k === tab ? 'active' : ''}">${l}</a>`).join('')}</div><div id="stab"></div>`;
  await SET[tab]($('#stab'));
};
const SET = {};
SET.general = async (el) => {
  const s = await api('/api/settings');
  el.innerHTML = `<div class="card"><form class="form" id="gs">
   <label class="f"><span>Base currency (reports & net worth)</span><select name="base_currency">${curOptions(s.base_currency)}</select></label>
   <label class="f"><span>Quick-add auto-post threshold (0–1). Lower confidence → To review</span><input name="review_threshold" type="number" step="0.05" min="0" max="1.01" value="${s.review_threshold}"></label>
   <label class="f"><span>Default account (quick-add when none is mentioned)</span><select name="default_account_id">${acctOptions(s.default_account_id, {blank: '— none (send to review) —'})}</select></label>
   <label class="f"><span>Default ledger</span><select name="default_ledger_id">${S.meta.ledgers.map(l => `<option value="${l.id}" ${l.id == s.default_ledger_id ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}</select></label>
   <label class="f"><span>Reconciled transactions</span><select name="lock_reconciled"><option value="0" ${S.meta.settings.lock_reconciled !== '1' ? 'selected' : ''}>Warn when changed (default)</option><option value="1" ${S.meta.settings.lock_reconciled === '1' ? 'selected' : ''}>Lock 🔒: ask for confirmation before changing</option></select></label>
   <label class="f"><span>Quick-add box: pressing Enter… (Shift+Enter does the other)</span><select name="quickadd_enter"><option value="add" ${txPrefs().quickadd_enter !== 'form' ? 'selected' : ''}>Adds right away</option><option value="form" ${txPrefs().quickadd_enter === 'form' ? 'selected' : ''}>Opens the pre-filled form to review ✎</option></select></label>
   <div class="full row end"><button class="btn pri">Save</button></div></form></div>`;
  $('#gs').onsubmit = async e => { e.preventDefault(); const o = Object.fromEntries(new FormData(e.target)); const qe = o.quickadd_enter; delete o.quickadd_enter;
    await guard(() => api('/api/settings', {method: 'PUT', body: o})); await savePrefs({quickadd_enter: qe}); toast('Saved'); refresh(); };
};
SET.categories = async (el) => {
  const tree = await api('/api/categories');
  const block = kind => tree.filter(c => c.kind === kind).map(r => `<div class="group-h"><span>${esc(r.name)} ${esc(r.name_zh)}</span><span><button class="btn sm" data-ec="${r.id}">Edit</button> <button class="btn sm" data-addsub="${r.id}">+ sub</button> <button class="btn sm danger" data-dc="${r.id}">✕</button></span></div>
     ${r.children.map(c => `<div class="acct" style="padding:6px 4px 6px 18px"><div class="nm"><b>${esc(c.name)} ${esc(c.name_zh)}</b><span class="small muted">keywords: ${esc(c.keywords || '—')}</span></div><button class="btn sm" data-ec="${c.id}">Edit</button><button class="btn sm danger" data-dc="${c.id}">✕</button></div>`).join('')}`).join('');
  el.innerHTML = `<div class="grid g2"><div class="card"><h3>Expense categories<span class="sp"></span><button class="btn sm pri" data-addroot="expense">+ top-level</button></h3>${block('expense')}</div>
   <div class="card"><h3>Income categories<span class="sp"></span><button class="btn sm pri" data-addroot="income">+ top-level</button></h3>${block('income')}</div></div>
   <p class="small muted">Keywords drive quick-add/email/CSV auto-categorisation (comma separated; English or Chinese; the longest match wins).</p>`;
  const all = tree.flatMap(r => [r, ...r.children]);
  const form = (c, extra) => { const bg = modal(c ? 'Edit category' : 'New category', `<form class="form" id="cf"><label class="f"><span>Name (English)</span><input name="name" value="${esc(c ? c.name : '')}"></label><label class="f"><span>中文名</span><input name="name_zh" value="${esc(c ? c.name_zh : '')}"></label><label class="f full"><span>Keywords</span><textarea name="keywords">${esc(c ? c.keywords : '')}</textarea></label><label class="f"><span>Sort</span><input name="sort" type="number" value="${c ? c.sort : 99}"></label></form>`, {buttons: '<button class="btn pri" data-ok>Save</button>'});
    $('[data-ok]', bg).onclick = async () => { const o = Object.fromEntries(new FormData($('#cf', bg))); await guard(() => c ? api('/api/categories/' + c.id, {method: 'PUT', body: o}) : api('/api/categories', {method: 'POST', body: {...o, ...extra}})); bg.close(); await loadMeta(); route(); }; };
  $$('[data-ec]', el).forEach(b => b.onclick = () => form(all.find(c => c.id == b.dataset.ec)));
  $$('[data-addsub]', el).forEach(b => b.onclick = () => form(null, {parent_id: +b.dataset.addsub}));
  $$('[data-addroot]', el).forEach(b => b.onclick = () => form(null, {kind: b.dataset.addroot}));
  $$('[data-dc]', el).forEach(b => b.onclick = async () => { if (!await confirmBox('Delete category?')) return; await guard(() => api('/api/categories/' + b.dataset.dc, {method: 'DELETE'})); await loadMeta(); route(); });
};
SET.lists = async (el) => {
  const ents = [['members', 'Members'], ['merchants', 'Merchants'], ['projects', 'Projects'], ['ledgers', 'Ledgers']];
  const data = await Promise.all(ents.map(([e]) => api('/api/' + e + (e === 'projects' ? '?summary=false&include_archived=true' : ''))));
  el.innerHTML = `<div class="grid g2">${ents.map(([e, l], i) => `<div class="card"><h3>${l}</h3><div style="max-height:360px;overflow:auto">${data[i].map(x => `<div class="row" style="margin-bottom:5px"><input value="${esc(x.name)}" data-ent="${e}" data-id="${x.id}">${e === 'projects' ? `<label class="chk small"><input type="checkbox" data-arch="${x.id}" ${x.archived ? 'checked' : ''}>archived</label>` : ''}<button class="btn sm danger" data-del="${e}/${x.id}">✕</button></div>`).join('') || '<div class="muted small">None yet</div>'}</div>
    <div class="row" style="margin-top:8px"><input placeholder="Add…" data-new="${e}"><button class="btn sm pri" data-add="${e}">Add</button></div></div>`).join('')}</div>
    <p class="small muted">Merchants are also created automatically from quick-add (<code>at Starbucks</code>), email alerts and imports.</p>`;
  $$('[data-ent]', el).forEach(i => i.onchange = () => guard(() => api(`/api/${i.dataset.ent}/${i.dataset.id}`, {method: 'PUT', body: {name: i.value}})).then(() => { toast('Saved'); loadMeta(); }));
  $$('[data-arch]', el).forEach(i => i.onchange = () => api(`/api/projects/${i.dataset.arch}`, {method: 'PUT', body: {archived: i.checked ? 1 : 0}}).then(loadMeta));
  $$('[data-del]', el).forEach(b => b.onclick = async () => { if (!await confirmBox('Delete? Transactions keep existing but lose this link.')) return; await guard(() => api('/api/' + b.dataset.del, {method: 'DELETE'})); await loadMeta(); route(); });
  $$('[data-add]', el).forEach(b => b.onclick = async () => { const i = $(`[data-new="${b.dataset.add}"]`, el); if (!i.value.trim()) return; await guard(() => api('/api/' + b.dataset.add, {method: 'POST', body: {name: i.value}})); await loadMeta(); route(); });
};
SET.currencies = async (el) => {
  const d = await api('/api/fx');
  el.innerHTML = `<div class="card"><h3>Exchange rates (manual)</h3><p class="muted small">Rate = value of 1 unit in USD terms (e.g. CNY 0.14 means ¥1 = $0.14). Base currency: <b>${d.base}</b>. Conversions use rate(from)/rate(to). Update these occasionally.</p>
   <table class="t"><thead><tr><th>Currency</th><th>Rate (vs USD)</th><th>Updated</th><th></th></tr></thead><tbody>${d.rates.map(r => `<tr><td><b>${r.currency}</b></td><td><input type="number" step="any" value="${r.rate}" data-cur="${r.currency}" style="max-width:160px"></td><td class="small muted">${esc(r.updated_at || '')}</td><td><button class="btn sm danger" data-d="${r.currency}">✕</button></td></tr>`).join('')}</tbody></table>
   <div class="row" style="margin-top:10px"><input id="nc" placeholder="Code e.g. EUR" style="max-width:140px"><input id="nr" type="number" step="any" placeholder="Rate e.g. 1.08" style="max-width:160px"><button class="btn pri" id="addc">Add / update</button></div></div>`;
  $$('[data-cur]', el).forEach(i => i.onchange = () => guard(() => api('/api/fx', {method: 'PUT', body: {currency: i.dataset.cur, rate: +i.value}})).then(() => { toast('Saved'); loadMeta(); }));
  $$('[data-d]', el).forEach(b => b.onclick = () => guard(() => api('/api/fx/' + b.dataset.d, {method: 'DELETE'})).then(refresh));
  $('#addc').onclick = () => guard(() => api('/api/fx', {method: 'PUT', body: {currency: $('#nc').value, rate: +$('#nr').value}})).then(refresh);
};
SET.security = async (el) => {
  el.innerHTML = `<div class="card" style="max-width:480px"><h3>Change password</h3><form class="grid" id="pf"><label class="f"><span>Current password</span><input type="password" name="old_password" required></label><label class="f"><span>New password (≥ 8 chars)</span><input type="password" name="new_password" required minlength="8"></label><button class="btn pri">Change</button></form>
   <p class="small muted">Forgot it? On the server: <code>cd /workspace/finance-app && .venv/bin/python -m app.cli reset-password</code> → the first-run setup page appears again.</p></div>`;
  $('#pf').onsubmit = async e => { e.preventDefault(); await guard(() => api('/api/auth/change-password', {method: 'POST', body: Object.fromEntries(new FormData(e.target))})); toast('Password changed'); e.target.reset(); };
};
SET.demo = async (el) => {
  el.innerHTML = `<div class="card" style="max-width:640px"><h3>🧪 Demo data</h3><p>Loads ~6 months of <b>fake</b> sample data so you can explore the app: 10 accounts (all named <code>[DEMO] …</code>), transactions, budgets, holdings and two review items. Nothing here is your data.</p>
   <div class="row"><button class="btn pri" id="ld" ${S.meta.has_demo ? 'disabled' : ''}>Load DEMO data</button><button class="btn danger" id="cd" ${S.meta.has_demo ? '' : 'disabled'}>Clear DEMO data</button></div>
   <p class="small muted">Clearing removes only rows flagged as demo; your own accounts and transactions stay.</p></div>`;
  $('#ld').onclick = loadDemo; $('#cd').onclick = clearDemo;
};
SET.api = async (el) => {
  const s = await api('/api/settings');
  const host = location.origin;
  el.innerHTML = `<div class="card"><h3>API key</h3><p class="small muted">Send it as header <code>X-API-Key</code>. Also stored on the server in <code>data/API_KEY.txt</code> (chmod 600).</p>
   <div class="row"><input id="ak" class="mono" readonly value="${esc(s.api_key)}" style="flex:1"><button class="btn" id="cp">Copy</button><button class="btn danger" id="rg">Regenerate</button></div></div>
  <div class="card" style="margin-top:16px"><h3>Examples</h3><pre>curl -s -X POST ${host}/api/quick-add \\
  -H "X-API-Key: ${esc(s.api_key)}" -H "Content-Type: application/json" \\
  -d '{"text":"lunch 25.5 chase sapphire","source":"message"}'

curl -s -X POST ${host}/api/ingest/email -H "X-API-Key: …" -H "Content-Type: application/json" \\
  -d '{"from":"alerts@chase.com","subject":"Your transaction","body":"You made a $25.50 transaction with STARBUCKS on card ending in 4242","received_at":"2026-10-11T12:30:00"}'

curl -s "${host}/api/transactions?limit=20" -H "X-API-Key: …"
curl -s "${host}/api/dashboard" -H "X-API-Key: …"</pre><p class="small">Full interactive API docs: <a href="/api/docs" target="_blank">/api/docs</a></p></div>
  <div class="card" style="margin-top:16px"><h3>Telegram bot ${s.telegram_enabled ? '<span class="pill income">enabled</span>' : '<span class="pill">not configured</span>'}</h3><p class="small">Set <code>TELEGRAM_BOT_TOKEN</code> and <code>TELEGRAM_ALLOWED_CHAT_IDS</code> in <code>finance-app/.env</code>, then <code>./stop.sh && ./start.sh</code>. See README → Telegram.</p></div>`;
  $('#cp').onclick = () => { $('#ak').select(); navigator.clipboard ? navigator.clipboard.writeText($('#ak').value).then(() => toast('Copied')) : document.execCommand('copy'); };
  $('#rg').onclick = async () => { if (!await confirmBox('Regenerate the API key? Old key stops working immediately.')) return; await guard(() => api('/api/settings/regenerate-api-key', {method: 'POST'})); route(); };
};
SET.backup = async (el) => {
  const [b, s] = await Promise.all([api('/api/backups'), api('/api/settings')]);
  el.innerHTML = `<div class="grid g2"><div class="card"><h3>Backups</h3><div class="kv small"><div>Location</div><div class="mono">${esc(b.dir)}</div><div>Last backup</div><div>${esc(b.last_backup_at || 'never')}</div></div>
    <div class="row" style="margin-top:12px"><button class="btn pri" id="bn">Back up now</button><a class="btn" href="/api/backups/download">⬇ Download a fresh backup</a></div>
    <form class="form" id="bs" style="margin-top:14px"><label class="f"><span>Automatic backup every (hours, 0 = off)</span><input name="backup_interval_hours" type="number" value="${s.backup_interval_hours}"></label><label class="f"><span>Keep last N backups</span><input name="backup_keep" type="number" value="${s.backup_keep}"></label><label class="f full"><span>Backup folder <small class="muted">(absolute path; blank = data/backups)</small></span><input name="backup_dir" value="${esc(s.backup_dir || '')}" placeholder="D:\\Backups\\Finance  ·  /mnt/nas/finance"></label><label class="chk full"><input type="hidden" name="backup_receipts" value="0"><input type="checkbox" name="backup_receipts" value="1" ${s.backup_receipts !== '0' ? 'checked' : ''}> Include receipt photos (copies only new/changed files, also when the receipts folder is outside the data folder)</label><div class="full row end"><button class="btn">Save schedule</button></div></form></div>
   <div class="card"><h3>Restore</h3><p class="small muted">Upload a <code>.db</code> backup. The current database is backed up first (<code>…-before-restore.db</code>). The restored file's password applies afterwards.</p>
    <div class="row"><input type="file" id="rf" accept=".db,.sqlite"><button class="btn danger" id="rb">Restore</button></div>
    <h3 style="margin-top:20px">Google Drive sync (rclone)</h3><div class="small">${b.rclone.installed ? '✓ rclone installed' : '✗ rclone not installed'} · ${b.rclone.remote ? '✓ remote <code>gdrive:</code> found → uploads to <code>gdrive:FinanceAppBackups</code>' : '✗ remote <code>gdrive</code> not configured (see README → Google Drive)'}</div>
    <div class="small muted">Last sync: ${esc(b.last_gdrive_sync || '—')}</div><button class="btn" id="gs" style="margin-top:8px" ${b.rclone.remote ? '' : 'disabled'}>Upload newest backup now</button></div></div>
   <div class="card" style="margin-top:16px"><h3>Existing backups (${b.items.length})</h3><table class="t"><tbody>${b.items.map(x => `<tr><td class="mono small">${esc(x.name)}</td><td class="small muted">${esc(x.modified)}</td><td class="small right">${(x.size / 1024).toFixed(0)} KB</td><td><a class="btn sm" href="/api/backups/file/${encodeURIComponent(x.name)}">⬇</a></td></tr>`).join('') || '<tr><td class="muted">None yet</td></tr>'}</tbody></table></div>`;
  $('#bn').onclick = async () => { const r = await guard(() => api('/api/backups', {method: 'POST'})); toast('Backup ' + r.name); route(); };
  $('#bs').onsubmit = async e => { e.preventDefault(); await guard(() => api('/api/settings', {method: 'PUT', body: Object.fromEntries(new FormData(e.target))})); toast('Saved'); route(); };
  offsiteCard(el);
  $('#rb').onclick = async () => { const f = $('#rf').files[0]; if (!f) return toast('Choose a file', true); if (!await confirmBox('Replace ALL current data with this backup?')) return; const fd = new FormData(); fd.append('file', f); const r = await guard(() => api('/api/backups/restore', {method: 'POST', body: fd})); alert('Restored. Safety copy: ' + r.safety_backup); location.reload(); };
  $('#gs').onclick = async () => { const r = await guard(() => api('/api/backups/gdrive-sync', {method: 'POST'})); toast(r.ok ? 'Uploaded ' + r.file : 'Failed: ' + (r.reason || r.stderr), !r.ok); route(); };
};

/* ---------- email monitoring ---------- */
SET.email = async (el) => {
  const [mbs, rules, logs] = await Promise.all([api('/api/mailboxes'), api('/api/email-rules'), api('/api/ingest-log?limit=100')]);
  el.innerHTML = `<div class="card"><h3>Mailboxes to watch<span class="sp"></span><button class="btn sm pri" id="addMb">+ Add mailbox</button></h3>
   <p class="small muted">The app polls these via IMAP, filters by sender allowlist / subject keywords, dedupes by Message-ID, parses amounts with the rules below and puts results in <a href="#/review">To review</a> (or posts them when auto-post is on and confidence ≥ threshold). Passwords are encrypted at rest with <code>data/secret.key</code> and never shown again. Use an <b>app password</b> (Gmail: imap.gmail.com:993, Google Account → Security → App passwords).</p>
   ${mbs.length ? `<div class="scroll-x"><table class="t"><thead><tr><th>Name</th><th>Server / user</th><th>Folder</th><th>Schedule</th><th>Filters</th><th>Last run</th><th></th></tr></thead><tbody>${mbs.map(m => `<tr>
    <td><b>${esc(m.name)}</b><div class="small">${m.enabled ? '<span class="pill income">enabled</span>' : '<span class="pill">disabled</span>'} ${m.auto_post ? '<span class="pill transfer">auto-post</span>' : ''} ${m.watch_rules ? '<span class="pill" title="Also takes emails a bill / schedule / loan / credit / points rule targets, and bill-looking emails, outside its sender / subject filter">+ bills & rules</span>' : ''}</div></td>
    <td class="small">${esc(m.host)}:${m.port} ${m.use_ssl ? 'SSL' : ''}<div class="muted">${esc(m.username)} · pw ${m.password_set ? '•••••• (set)' : '⚠ not set'}</div></td><td class="small">${esc(m.folder)}${m.after_action !== 'none' ? `<div class="muted">then ${m.after_action === 'move' ? 'move → ' + esc(m.move_folder) : 'mark read'}</div>` : ''}</td>
    <td class="small">${m.schedule_mode === 'fixed' ? '🕘 Fixed times' : '🔁 Interval'}<div>${esc(m.schedule_text || '')}</div><div class="muted">Next: ${m.enabled ? esc(m.next_run || '—') : 'disabled'}${m.schedule_mode === 'fixed' ? ` <span title="App time zone (Settings)">(${esc(m.timezone || '')})</span>` : ''}</div></td><td class="small muted">${esc(m.sender_allowlist || 'any sender')}${m.subject_keywords ? '<br>subj: ' + esc(m.subject_keywords) : ''}</td>
    <td class="small muted">${esc(m.last_checked_at || 'never')}<div>${esc(m.last_status || '')}</div></td>
    <td class="nowrap"><button class="btn sm" data-test="${m.id}">Test connection</button> <button class="btn sm" data-run="${m.id}">Check now</button> <button class="btn sm" data-edit="${m.id}">Edit</button> <button class="btn sm danger" data-del="${m.id}">✕</button></td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No mailboxes yet.</div>'}</div>
  <div class="card" style="margin-top:16px"><h3>Parsing rules (per sender)<span class="sp"></span><button class="btn sm" id="testRules">Test with a sample email</button><button class="btn sm" id="tplRule">From template</button><button class="btn sm pri" id="addRule">+ Add rule</button></h3>
   <p class="small muted">Rules are tried by priority (lower first) when the sender (and optional subject) regex matches. Each regex's first non-empty capture group is used. Presets are best effort — alert wording changes; edit them freely. Anything unmatched falls back to a generic extractor.</p>
   <div class="scroll-x"><table class="t"><thead><tr><th>Pri</th><th>Name</th><th>Sender regex</th><th>Kind</th><th>Enabled</th><th></th></tr></thead><tbody>${rules.map(r => `<tr><td>${r.priority}</td><td><b>${esc(r.name)}</b>${r.is_preset ? ' <span class="pill">preset</span>' : ''}<div class="small muted">${esc(r.note || '')}</div></td><td class="mono small">${esc(r.sender_pattern)}${r.subject_pattern ? '<br>subj: ' + esc(r.subject_pattern) : ''}</td><td>${r.kind === 'balance' ? `Balance adjust<div class="small muted">${esc(r.adjust_reason ? (REASON_NAMES[r.adjust_reason] || r.adjust_reason) : 'account default')}</div>` : esc(KIND_NAMES[r.kind] || r.kind)}</td>
     <td><input type="checkbox" data-en="${r.id}" ${r.enabled ? 'checked' : ''}></td><td class="nowrap"><button class="btn sm" data-er="${r.id}">Edit</button> <button class="btn sm danger" data-dr="${r.id}">✕</button></td></tr>`).join('')}</tbody></table></div></div>
  <div class="card" style="margin-top:16px"><h3>Ingest log<span class="sp"></span><button class="btn sm" onclick="route()">↻</button></h3>
   ${logs.length ? `<div class="scroll-x"><table class="t"><thead><tr><th>Time</th><th>Via</th><th>Sender</th><th>Subject</th><th>Result</th><th>Detail</th><th>Txn / item</th></tr></thead><tbody>${logs.map(l => `<tr><td class="small nowrap">${esc(l.time)}</td><td class="small">${esc(l.mailbox_name || l.source)}</td><td class="small">${esc((l.sender || '').slice(0, 40))}</td><td class="small">${esc((l.subject || '').slice(0, 60))}</td>
    <td><span class="pill ${l.result === 'posted' ? 'income' : l.result === 'review' ? 'review' : l.result === 'error' ? 'expense' : LOG_RESULTS[l.result] ? 'transfer' : ''}">${esc(LOG_RESULTS[l.result] || l.result)}</span> ${confPill(l.confidence)}</td><td class="small muted">${esc((l.detail || '').slice(0, 120))}${l.rule_name ? ' · rule ' + esc(l.rule_name) : ''}</td>
    <td class="nowrap">${l.transaction_id ? (l.tx_status ? `<a href="javascript:openTx(${l.transaction_id})">#${l.transaction_id}</a> <span class="small muted">${esc(l.tx_status)}</span>` : `<span class="small muted">#${l.transaction_id} (deleted)</span>`) : ''} ${logRefLink(l)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">Nothing ingested yet. POST /api/ingest/email or add a mailbox.</div>'}</div>`;
  $('#addMb').onclick = () => mailboxForm();
  $$('[data-edit]', el).forEach(b => b.onclick = () => mailboxForm(mbs.find(m => m.id == b.dataset.edit)));
  $$('[data-del]', el).forEach(b => b.onclick = async () => { if (!await confirmBox('Delete this mailbox?')) return; await api('/api/mailboxes/' + b.dataset.del, {method: 'DELETE'}); route(); });
  $$('[data-test]', el).forEach(b => b.onclick = async () => { b.disabled = true; b.textContent = 'Testing…'; const r = await guard(() => api(`/api/mailboxes/${b.dataset.test}/test`, {method: 'POST'})).finally(() => { b.disabled = false; b.textContent = 'Test connection'; }); r.ok ? toast(`✓ Connected · ${r.messages_in_folder} messages in folder`) : toast('✗ ' + r.error, true); });
  $$('[data-run]', el).forEach(b => b.onclick = async () => { b.disabled = true; b.textContent = 'Running…'; const r = await guard(() => api(`/api/mailboxes/${b.dataset.run}/run`, {method: 'POST'})).finally(() => { b.disabled = false; }); toast(`${r.status} · checked ${r.checked}, matched ${r.matched}, created ${r.created}`, r.status !== 'ok'); await loadMeta(); route(); });
  $$('[data-en]', el).forEach(c => c.onchange = () => api('/api/email-rules/' + c.dataset.en, {method: 'PUT', body: {enabled: c.checked}}));
  $$('[data-er]', el).forEach(b => b.onclick = () => ruleForm(rules.find(r => r.id == b.dataset.er)));
  $$('[data-dr]', el).forEach(b => b.onclick = async () => { if (!await confirmBox('Delete rule?')) return; await api('/api/email-rules/' + b.dataset.dr, {method: 'DELETE'}); route(); });
  $('#addRule').onclick = () => ruleForm();
  $('#tplRule').onclick = async () => {
    const tpls = await api('/api/email-rules/templates');
    const bg = modal('New rule from a template', `<p class="small muted" style="margin-top:0">Opens a prefilled rule form. Nothing is saved until you press Save, and templates are saved <b>disabled</b>; fill in the sender regex, test, then enable.</p>${tpls.map((t, i) => `<div class="acct"><div class="nm"><b>${esc(t.name)}</b><span class="small muted">${esc(t.note || '')}</span></div><button class="btn sm pri" data-tpl="${i}">Use</button></div>`).join('')}`);
    $$('[data-tpl]', bg).forEach(b => b.onclick = () => { bg.close(); ruleForm(null, tpls[+b.dataset.tpl]); });
  };
  $('#testRules').onclick = () => ruleTester();
};
function mailboxForm(m) {
  const isNew = !m; m = m || {port: 993, use_ssl: 1, folder: 'INBOX', interval_minutes: 15, enabled: 1, after_action: 'none', lookback_days: 7};
  const bg = modal(isNew ? 'Add mailbox' : 'Edit mailbox — ' + esc(m.name), `<form class="form" id="mf">
   <label class="f"><span>Name</span><input name="name" value="${esc(m.name || '')}" placeholder="Gmail bank alerts"></label>
   <label class="f"><span>IMAP host</span><input name="host" value="${esc(m.host || '')}" placeholder="imap.gmail.com"></label>
   <label class="f"><span>Port</span><input name="port" type="number" value="${m.port}"></label>
   <label class="chk" style="margin-top:20px"><input type="checkbox" name="use_ssl" ${m.use_ssl ? 'checked' : ''}> SSL/TLS (993). Unchecked = STARTTLS on 143</label>
   <label class="f"><span>Username</span><input name="username" value="${esc(m.username || '')}" autocomplete="off"></label>
   <label class="f"><span>App password ${m.password_set ? '(leave blank to keep the saved one)' : ''}</span><input name="password" type="password" autocomplete="new-password" placeholder="${m.password_set ? '•••••••• saved' : ''}"></label>
   <label class="f"><span>Folder / label to watch</span><input name="folder" value="${esc(m.folder || 'INBOX')}"></label>
   <label class="f"><span>Schedule</span><select name="schedule_mode"><option value="interval" ${m.schedule_mode !== 'fixed' ? 'selected' : ''}>Every N minutes</option><option value="fixed" ${m.schedule_mode === 'fixed' ? 'selected' : ''}>Fixed times</option></select></label>
   <label class="f" data-sm="interval"><span>Poll every (minutes)</span><input name="interval_minutes" type="number" min="1" value="${m.interval_minutes || 15}"></label>
   <label class="f" data-sm="fixed"><span>Times (HH:MM, comma separated, app time zone${m.timezone ? ' ' + esc(m.timezone) : ''})</span><input name="schedule_times" value="${esc(m.schedule_times || '')}" placeholder="08:00, 12:30, 21:00"></label>
   <div class="f full" data-sm="fixed"><span>Days (none checked = every day). If the app was off at a time, that check runs once when it starts again.</span><div class="row">${['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((l, i) => `<label class="chk"><input type="checkbox" data-wd="${i}" ${(m.schedule_weekdays || '').split(',').includes(String(i)) ? 'checked' : ''}> ${l}</label>`).join('')}</div></div>
   <label class="f full"><span>Sender allowlist (comma separated; substring of the From address, e.g. <code>chase.com, americanexpress.com, alipay</code>). Empty = any sender</span><input name="sender_allowlist" value="${esc(m.sender_allowlist || '')}"></label>
   <label class="f full"><span>Subject keywords (optional; any of)</span><input name="subject_keywords" value="${esc(m.subject_keywords || '')}" placeholder="transaction, purchase, 消费, 交易"></label>
   <label class="f"><span>After processing</span><select name="after_action"><option value="none" ${m.after_action === 'none' ? 'selected' : ''}>Leave as is</option><option value="mark_read" ${m.after_action === 'mark_read' ? 'selected' : ''}>Mark as read</option><option value="move" ${m.after_action === 'move' ? 'selected' : ''}>Move to folder…</option></select></label>
   <label class="f"><span>Move to folder</span><input name="move_folder" value="${esc(m.move_folder || '')}" placeholder="Processed"></label>
   <label class="f"><span>Default account when none detected</span><select name="default_account_id">${acctOptions(m.default_account_id, {blank: '— none —'})}</select></label>
   <label class="f"><span>Look back (days) on each poll</span><input name="lookback_days" type="number" min="1" value="${m.lookback_days || 7}"></label>
   <label class="chk"><input type="checkbox" name="enabled" ${m.enabled ? 'checked' : ''}> Enabled</label>
   <label class="chk"><input type="checkbox" name="auto_post" ${m.auto_post ? 'checked' : ''}> Auto-post high-confidence results (otherwise everything goes to review)</label>
   <label class="chk full"><input type="checkbox" name="watch_rules" ${m.watch_rules ? 'checked' : ''}> Also watch for bills and rule actions: take emails outside the sender / subject filter above when a bill, schedule, loan, credit or miles & points rule matches them, or when the subject reads like a bill or a payment confirmation (those only ever become bills, never transactions)</label>
   <div class="full small" id="mtest"></div></form>`, {wide: true, buttons: `<button class="btn" data-t>Test connection</button><span class="sp"></span><button class="btn" data-x3>Cancel</button><button class="btn pri" data-ok>Save</button>`});
  const f = $('#mf', bg);
  const smShow = () => $$('[data-sm]', bg).forEach(x => { x.hidden = x.dataset.sm !== f.schedule_mode.value; });
  f.schedule_mode.onchange = smShow; smShow();
  const collect = () => { const o = Object.fromEntries(new FormData(f)); ['use_ssl', 'enabled', 'auto_post', 'watch_rules'].forEach(k => o[k] = f[k].checked ? 1 : 0); if (!o.password) delete o.password;
    o.schedule_weekdays = $$('[data-wd]', bg).filter(c => c.checked).map(c => c.dataset.wd).join(','); return o; };
  $('[data-x3]', bg).onclick = bg.close;
  $('[data-t]', bg).onclick = async () => { $('#mtest', bg).textContent = 'Testing…'; const o = collect(); if (!isNew) o.id = m.id;
    try { const r = await api('/api/mailboxes/test', {method: 'POST', body: o}); $('#mtest', bg).innerHTML = r.ok ? `✅ Connected. ${r.messages_in_folder} messages in folder. Folders: <span class="muted">${esc((r.folders || []).join(', '))}</span>` : `❌ ${esc(r.error)}`; } catch (e) { $('#mtest', bg).textContent = '❌ ' + e.message; } };
  $('[data-ok]', bg).onclick = async () => { await guard(() => api(isNew ? '/api/mailboxes' : '/api/mailboxes/' + m.id, {method: isNew ? 'POST' : 'PUT', body: collect()})); bg.close(); toast('Saved'); route(); };
}
const REASON_NAMES = {investment_gain: 'Gain/loss', interest: 'Interest', fees: 'Fees', unrecorded_spending: 'Unrecorded spending', unrecorded_income: 'Unrecorded income', correction: 'Correction/other', opening: 'Opening balance'};
const KIND_NAMES = {auto: 'auto', expense: 'expense', income: 'income', refund: 'refund', balance: 'Balance adjust', hold: 'Card hold → Reserve',
  bill: 'Bill → Bills', bill_paid: 'Bill paid (confirmation)', schedule_post: 'Post a scheduled item', schedule_new: 'New one-time schedule',
  loan_payment: 'Loan payment', credit: 'Credit', reward_balance: 'Miles & points balance'};
/* per action kind: which extra rule fields show, and what they mean (date regex / payee / account / target) */
const RULE_ACTIONS = {
  bill: {date: 'Due date regex (blank = detect)', payee: 'Payee (blank = from the email)', acct: 'Pay from account (optional)',
    hint: '🧾 A bill or payment request: it becomes a payment task in Bills with a reminder before the due date. No transaction is recorded.'},
  bill_paid: {date: 'Payment date regex (blank = detect / email date)', payee: 'Payee (blank = from the email)',
    hint: '✅ A payment confirmation: it marks the matching open bill paid, or waits in Bills → Payments to review.'},
  schedule_post: {date: 'Date regex (optional: which occurrence)', acct: "Account (blank = the schedule's)", target: 'schedules', tlabel: 'Scheduled item',
    hint: "🔁 The email is this scheduled item happening (e.g. a paycheck alert): the occurrence nearest the email date is posted with the email's amount and marked done, so it is never posted twice. Already posted: nothing new."},
  schedule_new: {date: 'Scheduled date regex (blank = detect)', payee: 'Name / payee (blank = from the email)', acct: 'Account (blank = mailbox default)',
    hint: '🗓 A future charge (autopay scheduled, subscription renews …): a one-time scheduled item on that date with a reminder; nothing is posted until it happens.'},
  loan_payment: {date: 'Payment date regex (blank = email date)', acct: "Paid from / into (blank = the loan's)", target: 'loans', tlabel: 'Loan',
    hint: "🤝 A loan payment: recorded on the loan in Loan Center, split into principal / interest / escrow (blank amount = the expected payment). A loan with its own schedule posts that installment."},
  credit: {date: 'Expiry date regex (blank = detect)', payee: 'Issuer / title (blank = sender)', acct: 'Card (optional)',
    hint: '🎟 A travel / store credit: it is added to Credits with its expiry date.'},
  reward_balance: {date: 'Expiry date regex (blank = detect)', target: 'rewards', tlabel: 'Miles & points program',
    hint: '✈️ A miles / points statement: the amount regex finds the balance (blank = a number followed by miles / points); the program is updated.'},
};
const LOG_RESULTS = {bill: 'bill', bill_paid: 'bill paid', schedule_post: 'schedule posted', schedule_new: 'schedule added',
  loan_payment: 'loan payment', credit: 'credit', reward_balance: 'points', similar: 'likely duplicate'};
function logRefLink(l) {
  const go = {bill: '#/bills', schedule: '#/scheduled', credit: '#/credits', reward: '#/credits/rewards'}[l.ref_kind];
  if (l.ref_kind === 'loan' && l.ref_id) return `<a href="javascript:openLoan(${l.ref_id})">🤝 #${l.ref_id}</a>`;
  return go ? `<a href="${go}">${{bill: '🧾', schedule: '🔁', credit: '🎟', reward: '✈️'}[l.ref_kind]} #${l.ref_id || ''}</a>` : '';
}
function actionPreviewTxt(a) {
  if (!a) return '';
  return (a.action === 'skipped' ? '⚠️ ' : '➡️ ') + a.detail + (a.action === 'skipped' ? ' → it would become a transaction to review' : '');
}
function balancePreviewTxt(p) {
  if (!p) return '';
  if (!p.ok) return `⚠️ ${p.problem}`;
  const d = p.difference;
  return `${p.account_name}: balance ${money(p.previous, p.currency)} → statement ${money(p.statement_balance, p.currency)} on ${p.date ? p.date.slice(0, 10) : 'today'} · `
    + (Math.abs(d) < 0.005 ? 'no change, nothing recorded' : `adjustment ${d > 0 ? '+' : '−'}${money(Math.abs(d), p.currency)} (${p.reason_label})`)
    + (p.before_reconciliation ? ` · on/before reconciliation ${p.reconciled_at.slice(0, 10)}: goes to review` : '');
}
function ruleForm(r, tpl) {
  const isNew = !r; r = r || tpl || {kind: 'auto', priority: 10, enabled: 1};
  const bg = modal(isNew ? 'New parsing rule' : 'Edit rule — ' + esc(r.name), `<form class="form" id="rf2">
   <label class="f"><span>Name</span><input name="name" value="${esc(r.name || '')}"></label><label class="f"><span>Priority (lower runs first)</span><input name="priority" type="number" value="${r.priority}"></label>
   <label class="f"><span>Sender regex (matched against From)</span><input class="mono" name="sender_pattern" value="${esc(r.sender_pattern || '')}" placeholder="alerts@mybank\\.com"></label>
   <label class="f"><span>Subject regex (optional)</span><input class="mono" name="subject_pattern" value="${esc(r.subject_pattern || '')}"></label>
   <label class="f full"><span>Amount regex</span><input class="mono" name="amount_regex" value="${esc(r.amount_regex || '')}" placeholder="\\$([\\d,]+\\.\\d{2})"></label>
   <label class="f full"><span>Merchant regex</span><input class="mono" name="merchant_regex" value="${esc(r.merchant_regex || '')}"></label>
   <label class="f"><span>Last-4 regex</span><input class="mono" name="last4_regex" value="${esc(r.last4_regex || '')}" placeholder="ending in (\\d{4})"></label>
   <label class="f"><span>Kind</span><select name="kind">${Object.entries(KIND_NAMES).map(([k, l]) => `<option value="${k}" ${r.kind === k ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
   <div class="f full small muted" id="rfAct" hidden></div>
   <label class="f" data-ak="date"><span data-akl>Date regex</span><input class="mono" name="date_regex" value="${esc(r.date_regex || '')}" placeholder="due (?:date|on)[:\\s]*([\\w/ ,-]+\\d)"></label>
   <label class="f" data-ak="payee"><span data-akl>Payee</span><input name="payee" value="${esc(r.payee || '')}"></label>
   <label class="f" data-ak="acct"><span data-akl>Account</span><select name="account_id">${acctOptions(r.account_id, {blank: '— auto —'})}</select></label>
   <label class="f" data-ak="target"><span data-akl>Target</span><select name="target_id"><option value="">…</option></select></label>
   <div class="f full small muted" id="rfHold"${r.kind === 'hold' ? '' : ' hidden'}>🔒 A card authorization / pre-authorization alert: it becomes a card hold in Reserves to review (never a transaction). Authorization wording is also detected with kind auto.</div>
   <label class="f" id="rfReason"${r.kind === 'balance' ? '' : ' hidden'}><span>Adjustment reason</span><select name="adjust_reason"><option value="">Account default (gain/loss for investment accounts)</option>${Object.entries(REASON_NAMES).map(([k, l]) => `<option value="${k}" ${r.adjust_reason === k ? 'selected' : ''}>${l}</option>`).join('')}</select><span class="small muted">The amount found is the NEW account balance (statement value); the account is matched by its last 4 digits and the difference is recorded like Set balance, dated the email date.</span></label>
   <label class="f"><span>Enabled</span><select name="enabled"><option value="1" ${r.enabled ? 'selected' : ''}>Yes</option><option value="0" ${r.enabled ? '' : 'selected'}>No (disabled)</option></select></label>
   <label class="f"><span>Currency (blank = detect / account currency)</span><input name="currency" value="${esc(r.currency || '')}"></label>
   <label class="f"><span>Note</span><input name="note" value="${esc(r.note || '')}"></label>
   <label class="f full"><span>Try it: paste a sample email body (the subject is included automatically)</span><textarea id="sample" placeholder="You made a $12.34 transaction with TEST MERCHANT on your card ending in 0000"></textarea></label>
   <div class="full"><button type="button" class="btn sm" id="tryR">Test this rule</button> <span id="tryOut" class="small mono"></span></div></form>`, {wide: true, buttons: '<button class="btn pri" data-ok>Save</button>'});
  const f = $('#rf2', bg);
  const targets = {};
  const loadTargets = async (what) => {
    if (!targets[what]) targets[what] = await api({schedules: '/api/schedules', loans: '/api/loans?state=all', rewards: '/api/rewards'}[what]).then(d => (d.items || []).map(x =>
      ({id: x.id, label: what === 'loans' ? x.label : what === 'rewards' ? `${x.name} (${Math.round(x.balance || 0).toLocaleString()} ${x.unit})` : `${x.name}${x.enabled ? '' : ' (paused)'}`})));
    return targets[what];
  };
  const actShow = async () => {
    const a = RULE_ACTIONS[f.kind.value];
    $('#rfAct', bg).hidden = !a; if (a) $('#rfAct', bg).textContent = a.hint;
    for (const k of ['date', 'payee', 'acct', 'target']) {
      const box = $(`[data-ak="${k}"]`, bg), lab = a && (k === 'target' ? a.tlabel : a[k]);
      box.hidden = !lab; if (lab) $('[data-akl]', box).textContent = lab;
    }
    if (a && a.target) {
      const sel = f.target_id, want = sel.dataset.t === a.target ? sel.value : (RULE_ACTIONS[r.kind] || {}).target === a.target ? r.target_id || '' : '';
      sel.dataset.t = a.target;
      const list = await loadTargets(a.target);
      sel.innerHTML = `<option value="">— choose —</option>` + list.map(x => `<option value="${x.id}" ${String(x.id) === String(want) ? 'selected' : ''}>${esc(x.label)}</option>`).join('');
    }
  };
  f.kind.onchange = () => { $('#rfReason', bg).hidden = f.kind.value !== 'balance'; $('#rfHold', bg).hidden = f.kind.value !== 'hold'; actShow(); };
  actShow();
  $('#tryR', bg).onclick = async () => { const rule = Object.fromEntries(new FormData(f)); const res = await guard(() => api('/api/email-rules/test', {method: 'POST', body: {from: (rule.sender_pattern || '').replace(/\\/g, ''), subject: '', body: $('#sample', bg).value, rule}}));
    const bp = res.balance_preview, ap = res.action_preview; delete res.balance_preview; delete res.action_preview;
    $('#tryOut', bg).textContent = (bp ? '🧮 ' + balancePreviewTxt(bp) + '\n' : '') + (ap ? actionPreviewTxt(ap) + '\n' : '') + JSON.stringify(res); };
  $('[data-ok]', bg).onclick = async () => { await guard(() => api(isNew ? '/api/email-rules' : '/api/email-rules/' + r.id, {method: isNew ? 'POST' : 'PUT', body: Object.fromEntries(new FormData(f))})); bg.close(); route(); };
}
function ruleTester() {
  const bg = modal('Test parsing rules with a sample email', `<form class="form" id="tf"><label class="f"><span>From</span><input name="from" placeholder="no-reply@alerts.chase.com"></label><label class="f"><span>Subject</span><input name="subject"></label><label class="f full"><span>Body (text or HTML)</span><textarea name="body" style="min-height:140px"></textarea></label></form><pre id="tout" class="small"></pre>`, {wide: true, buttons: '<button class="btn" data-ingest>Ingest into review inbox</button><button class="btn pri" data-ok>Parse</button>'});
  const f = $('#tf', bg);
  $('[data-ok]', bg).onclick = async () => { const res = await guard(() => api('/api/email-rules/test', {method: 'POST', body: Object.fromEntries(new FormData(f))})); const ap = res.action_preview; delete res.action_preview;
    $('#tout', bg).textContent = (res.balance_preview ? '🧮 Adjustment preview: ' + balancePreviewTxt(res.balance_preview) + '\n\n' : '') + (ap ? actionPreviewTxt(ap) + '\n\n' : '') + JSON.stringify(res, null, 2); };
  $('[data-ingest]', bg).onclick = async () => { const r = await guard(() => api('/api/ingest/email', {method: 'POST', body: Object.fromEntries(new FormData(f))})); $('#tout', bg).textContent = (r.detail ? '➡️ ' + r.detail + '\n\n' : '') + JSON.stringify({result: r.result, action: r.action, confidence: r.confidence, reasons: r.reasons, transaction_id: r.transaction && r.transaction.id, bill_id: r.bill && r.bill.id, ref: r.ref_kind ? r.ref_kind + ' #' + r.ref_id : undefined}, null, 2); loadMeta(); };
}
