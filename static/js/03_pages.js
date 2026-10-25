/* ---------- accounts ---------- */
/* Accounts as a tree: group nodes (collapsible, state in localStorage) with account rows as children. */
const TREE_KEY = 'acctTreeCollapsed', ZERO_KEY = 'acctHideZero', ARCH_KEY = 'acctShowArchived';
const treeState = () => { try { return JSON.parse(localStorage.getItem(TREE_KEY) || '{}'); } catch (e) { return {}; } };
const nodeKey = n => n.id === null || n.id === undefined ? 'ungrouped' : 'g' + n.id;
function acctRow(a) {
  const zero = Math.abs(a.balance) < 0.005;
  return `<div class="acct tree-child${zero ? ' zero' : ''}${a.archived ? ' archived' : ''}" data-aid="${a.id}" title="Open history" data-zero="${zero ? 1 : 0}" data-arch="${a.archived ? 1 : 0}">
      <div class="nm"><b>${isDemoName(a.name) ? '<span class="pill demo">DEMO</span> ' : ''}${esc(a.name)} ${a.last4 ? `<span class="muted small">••${esc(a.last4)}</span>` : ''}</b>
      <span class="small muted">${esc(a.type_label)} · ${a.currency}${a.aliases ? ' · aliases: ' + esc(a.aliases) : ''}${a.archived ? ' · archived' : ''}</span>
      ${a.reconciled_at ? `<div class="small"><a class="rec-link" href="javascript:void 0" data-rhist="${a.id}" title="Reconciliation history">✔ Reconciled ${esc(a.reconciled_at.slice(0, 10))}${a.reconciliation_count > 1 ? ` · ${a.reconciliation_count}×` : ''}</a></div>` : ''}
      ${a.type === 'credit_card' && a.card_expiry ? `<div class="small">${cardExpiryPill(a)}</div>` : ''}
      ${a.type === 'credit_card' && a.credit_limit ? `<div class="small muted">Owed ${money(a.owed, a.currency)} · Limit ${money(a.credit_limit, a.currency)} · Available ${money(a.available_credit, a.currency)}</div><div class="bar" style="max-width:260px"><i class="${a.owed / a.credit_limit > .8 ? 'over' : a.owed / a.credit_limit > .5 ? 'warn' : ''}" style="width:${Math.max(0, Math.min(100, a.owed / a.credit_limit * 100))}%"></i></div>` : ''}
      ${a.on_hold > 0.005 ? `<div class="small"><a href="#/reserves" class="rs-hold" title="Card holds (pre-authorizations): not in the balance">🔒 ${esc(t('On hold {amt}', {amt: money(a.on_hold, a.currency)}))}</a></div>` : ''}
      ${a.is_investment ? `<div class="small muted">Cash ${money(a.cash_balance, a.currency)} · Holdings ${money(a.holdings_value, a.currency)}</div>` : ''}
      </div>
      <div class="bal ${a.is_liability ? (a.owed > 0 ? 'neg' : zero ? '' : 'pos') : a.balance < 0 ? 'neg' : ''}">${a.is_liability ? `<span class="small muted">${a.owed >= 0 ? 'owed' : 'credit'} </span>${money(Math.abs(a.owed), a.currency)}` : money(a.balance, a.currency)}${a.currency !== S.meta.settings.base_currency ? `<div class="small muted">${money(a.balance_base)}</div>` : ''}</div>
      <div class="row acts" style="flex-wrap:nowrap"><a class="btn sm" href="#/account/${a.id}" title="Account history">📜 <span class="hide-m">History</span></a><button class="btn sm" data-adj="${a.id}" title="Enter the real balance; an adjustment transaction records the difference">⚖️ <span class="hide-m">Set balance</span></button><button class="btn sm" data-rec="${a.id}" title="Compare with a statement and lock the balance at that date">✔ <span class="hide-m">Reconcile</span></button><button class="btn sm" data-edit="${a.id}">Edit</button></div></div>`;
}
/* credit card expiry: "Expires 08/2029", amber inside the reminder window, red once expired */
function cardExpiryPill(a) {
  if (!a || !a.card_expiry) return '';
  const y = +a.card_expiry.slice(0, 4), m = +a.card_expiry.slice(5, 7), end = new Date(y, m, 0), now = new Date(); now.setHours(0, 0, 0, 0);
  const days = Math.round((end - now) / 864e5), lead = +(S.meta.settings.card_expiry_lead_days || 60), lbl = a.card_expiry.slice(5, 7) + '/' + y;
  const cls = days < 0 ? 'cx-bad' : days <= lead ? 'warn' : '';
  return `<span class="pill ${cls}" title="${esc(t('Valid through the end of that month'))}">💳 ${esc(days < 0 ? t('Expired {date}', {date: lbl}) : t('Expires {date}', {date: lbl}))}</span>`;
}
/* principal / returns summary line (used by the Investments page; the Accounts tree no longer shows it) */
function pctTxt(p) { return p === null || p === undefined ? '' : ` (${p >= 0 ? '+' : ''}${p.toFixed(1)}%)`; }
function prLine(x) {
  const r = x.returns;
  return `Principal ${money(x.principal, x.currency)} · Returns <span class="${r < 0 ? 'neg' : r > 0 ? 'pos' : ''}">${r >= 0 ? '+' : '−'}${money(Math.abs(r), x.currency)}${pctTxt(x.returns_pct)}</span>`
    + (Math.abs(x.unexplained || 0) >= 0.01 ? ` · <span class="unexp-txt" title="Unclassified balance adjustments (reclassify them) + reconciliation differences">Unexplained ${money(x.unexplained, x.currency)}</span>` : '');
}
function groupTotal(n) {
  const base = n.currency;
  const main = n.liability_group
    ? `<span class="small muted">${n.owed_base >= 0 ? 'owed' : 'credit'} </span><span class="${n.owed_base > 0 ? 'neg' : ''}">${money(Math.abs(n.owed_base), base)}</span>`
    : `<span class="${n.total_base < 0 ? 'neg' : ''}">${money(n.total_base, base)}</span>`;
  const subs = n.by_currency.map(c => `${c.currency} ${num(n.liability_group ? -c.total : c.total)}${c.currency !== base ? ` ≈ ${money(n.liability_group ? -c.total_base : c.total_base, base)}` : ''}`);
  return `<div class="gt-main" title="${esc((n.liability_group ? 'Owed by currency: ' : 'By currency: ') + subs.join(' · '))}">${main}</div>${n.mixed_currency ? `<div class="small muted gt-sub">${esc(subs.join(' · '))}</div>` : ''}`;
}
function treeNode(n, byId, depth = 0) {
  const collapsed = !!treeState()[nodeKey(n)];
  const kids = n.account_ids.map(id => byId[id]).filter(Boolean);
  return `<div class="tnode${collapsed ? ' collapsed' : ''}" data-node="${nodeKey(n)}" style="--depth:${depth}">
    <div class="tgroup" role="button" tabindex="0" aria-expanded="${!collapsed}" data-toggle="${nodeKey(n)}">
      <span class="chev">${collapsed ? '▸' : '▾'}</span>
      <span class="gname">${esc(n.name)} <span class="small muted">· ${n.active_count} account${n.active_count === 1 ? '' : 's'}${n.zero_count ? ` (${n.zero_count} at 0)` : ''}${n.archived_count ? ` · ${n.archived_count} archived` : ''}</span></span>
      <span class="gtotal">${groupTotal(n)}</span></div>
    <div class="tkids">${kids.map(acctRow).join('')}${(n.children || []).map(c => treeNode(c, byId, depth + 1)).join('')}</div></div>`;
}
function applyAcctFilters() {
  const hideZero = localStorage.getItem(ZERO_KEY) === '1', showArch = localStorage.getItem(ARCH_KEY) === '1';
  $$('.tree-child').forEach(r => { r.style.display = (r.dataset.arch === '1' && !showArch) || (r.dataset.zero === '1' && hideZero) ? 'none' : ''; });
}
function setCollapsed(key, val) { const st = treeState(); if (val) st[key] = 1; else delete st[key]; localStorage.setItem(TREE_KEY, JSON.stringify(st)); }
VIEWS.accounts = async (v) => {
  const d = await api('/api/accounts');
  const nw = d.net_worth;
  const byId = Object.fromEntries(d.accounts.map(a => [a.id, a]));
  v.innerHTML = `${demoBanner()}<div class="grid g3">
   <div class="card stat"><div class="lbl">Assets</div><div class="val pos">${money(nw.assets, nw.currency)}</div></div>
   <div class="card stat"><div class="lbl">Liabilities</div><div class="val neg">${money(nw.liabilities, nw.currency)}</div></div>
   <div class="card stat hero"><div class="lbl">Net worth = assets − liabilities</div><div class="val">${money(nw.net_worth, nw.currency)}</div></div></div>
  <div class="card" style="margin-top:16px"><h3 class="tree-h">Accounts<span class="sp"></span>
    <button class="btn sm" id="expAll">▾ Expand all</button><button class="btn sm" id="colAll">▸ Collapse all</button>
    <label class="chk small"><input type="checkbox" id="hideZero" ${localStorage.getItem(ZERO_KEY) === '1' ? 'checked' : ''}> hide zero balances</label>
    <label class="chk small"><input type="checkbox" id="showArch" ${localStorage.getItem(ARCH_KEY) === '1' ? 'checked' : ''}> show archived</label>
    <button class="btn sm" id="grpBtn">Groups</button><button class="btn sm pri" id="addAcct">+ Add account</button></h3>
  ${!d.accounts.length ? '<div class="empty">No accounts yet. Add your bank accounts, cards, e-wallets, brokerage accounts, loans…</div>' : ''}
  <div class="tree" id="acctTree">${(d.tree || []).map(n => treeNode(n, byId)).join('')}</div>
  <p class="small muted" style="margin-top:8px">Group totals are converted to ${esc(nw.currency)} with the rates in Settings → Currencies and add up to net worth. Accounts at 0 are dimmed.</p></div>`;
  const toggle = el => { const node = el.closest('.tnode'), c = !node.classList.contains('collapsed'); node.classList.toggle('collapsed', c); el.setAttribute('aria-expanded', !c); $('.chev', el).textContent = c ? '▸' : '▾'; setCollapsed(node.dataset.node, c); };
  $$('[data-toggle]').forEach(g => { g.onclick = e => { if (!e.target.closest('button,a,input')) toggle(g); }; g.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(g); } }; });
  const all = c => { $$('.tnode').forEach(n => { n.classList.toggle('collapsed', c); const g = $('.tgroup', n); g.setAttribute('aria-expanded', !c); $('.chev', g).textContent = c ? '▸' : '▾'; setCollapsed(n.dataset.node, c); }); };
  $('#expAll').onclick = () => all(false); $('#colAll').onclick = () => all(true);
  $('#hideZero').onchange = e => { localStorage.setItem(ZERO_KEY, e.target.checked ? '1' : '0'); applyAcctFilters(); };
  $('#showArch').onchange = e => { localStorage.setItem(ARCH_KEY, e.target.checked ? '1' : '0'); applyAcctFilters(); };
  applyAcctFilters();
  $('#addAcct').onclick = () => acctForm();
  $$('[data-edit]').forEach(b => b.onclick = () => acctForm(byId[b.dataset.edit]));
  $$('[data-adj]').forEach(b => b.onclick = () => adjustForm(byId[b.dataset.adj]));
  $$('.acct[data-aid]').forEach(r => r.onclick = e => { if (!e.target.closest('button,a,input,select')) location.hash = '#/account/' + r.dataset.aid; });
  $$('[data-rec]').forEach(b => b.onclick = () => reconcileDialog(byId[b.dataset.rec]));
  $$('[data-rhist]').forEach(b => b.onclick = () => reconcileHistory(byId[b.dataset.rhist]));
  $('#grpBtn').onclick = () => groupsEditor(d.groups);
};
function acctForm(a) {
  const isNew = !a; a = a || {type: 'checking', currency: S.meta.settings.base_currency, opening_balance: 0};
  const liab = t => S.meta.account_types[t] && S.meta.account_types[t].liability;
  const bg = modal(isNew ? 'Add account' : 'Edit account', `<form class="form" id="af">
   <label class="f full"><span>Name</span><input name="name" required value="${esc(a.name || '')}" placeholder="e.g. Chase Sapphire, 招商银行储蓄卡, Alipay 支付宝"></label>
   <label class="f"><span>Type</span><select name="type">${typeOptions(a.type)}</select></label>
   <label class="f"><span>Group</span><select name="group_id"><option value="">(default for type)</option>${S.meta.groups.map(g => `<option value="${g.id}" ${g.id == a.group_id ? 'selected' : ''}>${esc(g.name)}</option>`).join('')}</select></label>
   <label class="f"><span>Currency</span><select name="currency">${curOptions(a.currency)}</select></label>
   ${isNew ? `<label class="f"><span id="obLbl">Opening balance</span><input name="opening_balance" type="number" step="0.01" value="0"></label>
   <label class="f"><span>Start date (opening balance is recorded on this day)</span><input name="start_date" type="date" value="${today()}"></label>`
   : `<label class="f"><span>${liab(a.type) ? 'Amount owed now' : 'Current balance'}</span><input name="balance" type="number" step="0.01" value="${liab(a.type) ? a.owed : a.balance}" data-orig="${liab(a.type) ? a.owed : a.balance}"><span class="small muted">Changing this records a balance-adjustment transaction for the difference (like 随手记 余额变更).${a.opening_balance ? ` Legacy opening balance field: ${money(a.opening_balance, a.currency)}.` : ''}</span></label>
   <label class="f" id="brWrap" style="display:none"><span>Reason for difference</span><select name="balance_reason">${reasonOptions(defaultReason(a))}</select></label>`}
   <label class="f" data-inv><span>Prior returns in opening balance</span><input name="opening_returns" type="number" step="0.01" value="${a.opening_returns ?? ''}" placeholder="0 = all principal"><span class="small muted">Part of the opening balance that was earlier gains, not money you put in. Default: all principal 本金.</span></label>
   <label class="f" data-cc><span>Credit limit</span><input name="credit_limit" type="number" step="0.01" value="${a.credit_limit ?? ''}"></label>
   <label class="f" data-cc><span>Statement day (1–31)</span><input name="stmt_day" type="number" min="1" max="31" value="${a.stmt_day ?? ''}" placeholder="blank = no reminders"></label>
   <label class="f" data-cc><span>Due day (day of month)</span><input name="due_day" type="number" min="1" max="31" value="${a.due_day ?? ''}"></label>
   <label class="f" data-cc><span>…or due N days after statement 账单后N天</span><input name="due_after_days" type="number" min="1" max="60" value="${a.due_after_days ?? ''}"></label>
   <label class="f" data-cc><span>Remind days before due</span><input name="remind_days" type="number" min="0" max="60" value="${a.remind_days ?? ''}" placeholder="default (Settings, 10)"></label>
   <label class="f" data-cc><span>Minimum payment %</span><input name="min_pay_pct" type="number" step="0.1" value="${a.min_pay_pct ?? ''}"></label>
   <label class="f" data-cc><span>Card expires (MM/YY)</span><input name="card_expiry" placeholder="MM/YY" maxlength="7" inputmode="numeric" value="${a.card_expiry ? esc(a.card_expiry.slice(5, 7) + '/' + a.card_expiry.slice(2, 4)) : ''}"><span class="small muted">Valid through the end of that month. You'll be reminded to move autopayments to the new card before it expires.</span></label>
   <label class="f" data-cc><span>Autopay</span><span><input type="checkbox" name="autopay" ${a.autopay ? 'checked' : ''}> on</span></label>
   <label class="f"><span>Institution</span><input name="institution" value="${esc(a.institution || '')}"></label>
   <label class="f"><span>Last 4 digits (matches email alerts)</span><input name="last4" maxlength="5" value="${esc(a.last4 || '')}"></label>
   <label class="f full"><span>Aliases (comma separated; used by quick-add, e.g. "sapphire, csp, 招行卡")</span><input name="aliases" value="${esc(a.aliases || '')}"></label>
   <label class="f full"><span>Note</span><input name="note" value="${esc(a.note || '')}"></label>
   ${isNew ? '' : `<label class="chk"><input type="checkbox" name="archived" ${a.archived ? 'checked' : ''}> Archived 隐藏</label>`}
   <label class="chk"><input type="checkbox" name="is_default" ${S.meta.settings.default_account_id == a.id && a.id ? 'checked' : ''}> Default account for quick-add</label>
  </form>`, {buttons: `${isNew ? '' : '<button class="btn danger" data-del>Delete</button><span class="sp"></span>'}<button class="btn" data-x2>Cancel</button><button class="btn pri" data-ok>Save</button>`});
  const f = $('#af', bg);
  const upd = () => { const l = liab(f.type.value); if ($('#obLbl', bg)) $('#obLbl', bg).textContent = l ? 'Amount owed at start' : 'Opening balance'; $$('[data-cc]', bg).forEach(x => x.style.display = f.type.value === 'credit_card' ? '' : 'none'); $('[data-inv]', bg).style.display = isInvType(f.type.value) ? '' : 'none'; };
  f.type.onchange = upd; upd();
  if (f.balance) f.balance.oninput = () => { $('#brWrap', bg).style.display = f.balance.value !== '' && +f.balance.value !== +f.balance.dataset.orig ? '' : 'none'; };
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const o = Object.fromEntries(new FormData(f)); o.archived = f.archived && f.archived.checked ? 1 : 0;
    if (o.type === 'credit_card') o.autopay = f.autopay.checked ? 1 : 0; else ['stmt_day', 'due_day', 'due_after_days', 'remind_days', 'min_pay_pct', 'autopay', 'card_expiry'].forEach(k => delete o[k]);
    const isDef = f.is_default.checked; delete o.is_default;
    if (isNew) o.opening_balance = (+o.opening_balance || 0) * (liab(o.type) ? -1 : 1);
    else { delete o.opening_balance; const bi = f.balance; if (bi.value === '' || +bi.value === +bi.dataset.orig) { delete o.balance; delete o.balance_reason; } else o.balance = +bi.value; }
    if (!o.group_id) delete o.group_id;
    if (!isInvType(o.type)) delete o.opening_returns; else if (o.opening_returns === '') o.opening_returns = null;
    const r = await guard(() => api(isNew ? '/api/accounts' : '/api/accounts/' + a.id, {method: isNew ? 'POST' : 'PUT', body: o}));
    if (isDef) await api('/api/settings', {method: 'PUT', body: {default_account_id: r.id}});
    else if (S.meta.settings.default_account_id == r.id) await api('/api/settings', {method: 'PUT', body: {default_account_id: ''}});
    bg.close(); toast(r.balance_set ? r.balance_set.message : 'Saved'); refresh();
  };
  const del = $('[data-del]', bg); if (del) del.onclick = async () => {
    try { await api('/api/accounts/' + a.id, {method: 'DELETE'}); }
    catch (e) { if (!await confirmBox(e.message + '\n\nDelete the account AND its transactions?')) return; await guard(() => api(`/api/accounts/${a.id}?force=true`, {method: 'DELETE'})); }
    bg.close(); toast('Deleted'); refresh();
  };
}
function adjustForm(a) {
  const owed = !!a.is_liability;
  const cur = owed ? a.owed : a.balance;
  const bg = modal('Set balance — ' + esc(a.name), `<p class="muted">The app currently computes ${owed ? 'an amount owed' : 'a balance'} of <b>${money(cur, a.currency)}</b>. Enter the real ${owed ? 'amount owed (positive number)' : 'balance'}; a <b>balance adjustment</b> transaction records the difference. It changes balances and net worth but is not income or expense.</p>
   <form class="form" id="adf"><label class="f"><span>${owed ? 'Actual amount owed' : 'Actual balance'}</span><input name="target" type="number" step="0.01" required autofocus></label>
   <label class="f"><span>As of</span><input name="date" type="datetime-local" value="${nowLocal()}"></label>
   <label class="f full"><span>Reason for difference</span><select name="reason">${reasonOptions(defaultReason(a))}</select><span class="small muted" id="adWhy"></span></label>
   <label class="f full"><span>Note (optional)</span><input name="note" placeholder="e.g. matched bank app"></label>
   <label class="chk full"><input type="checkbox" name="reconcile"> Also mark as reconciled 同时标记为已对账 <span class="small muted">(locks the balance at this date: later edits to older transactions won't change it)</span></label>
   <div class="full small" id="adPrev"></div></form>`,
   {buttons: '<button class="btn" data-x2>Cancel</button><button class="btn pri" data-ok>Set balance</button>'});
  const f = $('#adf', bg);
  const prev = () => { const v = f.target.value; const d = v === '' ? null : Math.round((+v - cur) * 100) / 100; const sd = d === null ? 0 : (owed ? -d : d);
    $('#adWhy', bg).textContent = (f.reason.value === 'investment_gain' && d ? (sd >= 0 ? 'Recorded as an investment GAIN 投资收益. ' : 'Recorded as an investment LOSS 投资亏损. ') : '') + REASON_HELP[f.reason.value];
    $('#adPrev', bg).innerHTML = d === null ? '' : d === 0 ? '<span class="muted">No difference: nothing will be recorded.</span>' : `Difference: <b class="${sd < 0 ? 'neg' : 'pos'}">${d > 0 ? '+' : ''}${num(d)}</b> ${owed ? '(owed)' : ''} <span class="muted">(vs. today's balance; a back-dated entry compares with the balance on that date)</span>`; };
  f.target.oninput = prev; f.reason.onchange = prev; prev();
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    if (f.target.value === '') return toast('Enter the balance', true);
    const r = await guard(() => api(`/api/accounts/${a.id}/set-balance`, {method: 'POST', body: {balance: +f.target.value, date: f.date.value ? f.date.value.replace('T', ' ') : null, note: f.note.value, reason: f.reason.value, reconcile: f.reconcile.checked}}));
    bg.close(); toast(r.message); refresh();
  };
}
/* ---------- reconciliation 对账 ---------- */
function reconcileDialog(a) {
  const owed = !!a.is_liability, cur = a.currency;
  const bg = modal('Reconcile — ' + esc(a.name), `<form class="form" id="rcf">
   <label class="f"><span>${owed ? 'Statement amount owed' : 'Statement balance'}</span><input name="balance" type="number" step="0.01" inputmode="decimal" required autofocus></label>
   <label class="f"><span>Statement date</span><input name="as_of" type="date" value="${today()}"></label>
   <label class="f full"><span>Note (optional)</span><input name="note" placeholder="e.g. September statement"></label></form>
   <div id="rcSum" class="rec-sum"></div><div id="rcList"></div>`, {wide: true,
    buttons: '<button class="btn" data-hist>History</button><span class="sp"></span><button class="btn" data-x2>Cancel</button><button class="btn pri" data-ok disabled>Finalize</button>'});
  const f = $('#rcf', bg); let P = null, seq = 0;
  const cleared = new Set();
  const sign = owed ? -1 : 1;
  const summary = () => {
    if (!P) return;
    const prevBal = P.previous ? P.previous.balance : null;
    const clearedSum = P.transactions.filter(t => cleared.has(t.id)).reduce((s, t) => s + t.effect, 0);
    const hasSt = P.statement !== undefined, d = hasSt ? P.difference_shown : null, zero = hasSt && Math.abs(d) < 0.005;
    $('#rcSum', bg).innerHTML = `<div class="grid g4 rec-stats">
      <div><div class="lbl">${P.previous ? `Previous checkpoint ${esc(P.previous.as_of.slice(0, 10))}` : 'No previous checkpoint'}</div><div class="v">${P.previous ? money(prevBal * sign, cur) : '—'}</div></div>
      <div><div class="lbl">Computed by the app</div><div class="v">${money(P.computed, cur)}</div><div class="small muted">${P.transactions.length} transaction${P.transactions.length === 1 ? '' : 's'} since${P.previous ? ' the last checkpoint' : ' the start'}</div></div>
      <div><div class="lbl">Statement</div><div class="v">${hasSt ? money(P.statement, cur) : '<span class="muted">enter above</span>'}</div><div class="small muted">Cleared ✔ ${cleared.size} · ${money(clearedSum * sign, cur)}</div></div>
      <div><div class="lbl">Difference</div><div class="v ${!hasSt ? '' : zero ? 'pos' : 'neg'}">${hasSt ? (zero ? '0.00 ✓' : (d > 0 ? '+' : '') + num(d)) : '—'}</div></div></div>
      ${P.added_before_previous ? `<div class="rec-warn">⚠ ${P.added_before_previous} transaction(s) dated before the previous checkpoint were added later; they don't count toward the balance (see History → drift).</div>` : ''}
      ${hasSt && !zero ? `<div class="rec-diff"><b>The statement and the app differ by ${num(Math.abs(d))} ${esc(cur)}.</b> Look for missing or duplicate transactions below, or:
        <label class="chk"><input type="radio" name="rcMode" value="adjust" checked> Record the difference as a balance adjustment 记为余额调整, reason <select id="rcReason">${reasonOptions(defaultReason(a))}</select></label>
        <label class="chk"><input type="radio" name="rcMode" value="accept"> Finalize anyway 直接完成 (no transaction; the statement balance becomes the anchor and the difference shows as drift)</label></div>` : ''}`;
    $('[data-ok]', bg).disabled = !hasSt;
  };
  const list = () => {
    const tx = P.transactions, shown = tx.slice(-400);
    $('#rcList', bg).innerHTML = !tx.length ? '<div class="empty">No transactions since the previous checkpoint</div>' : `
      <div class="row small" style="margin:8px 0"><b>Transactions since ${P.previous ? 'the previous checkpoint' : 'the start'} 待核对</b><span class="sp"></span>
      <button type="button" class="btn sm" id="rcAll">Check all ✔</button><button type="button" class="btn sm" id="rcNone">Clear</button></div>
      ${tx.length > shown.length ? `<div class="small muted">Showing the latest ${shown.length} of ${tx.length}; earlier ones are included in the computed balance.</div>` : ''}
      <div class="scroll-x rec-list"><table class="t"><thead><tr><th>Cleared</th><th>Date</th><th>Details</th><th class="right">Effect</th><th class="right">Running</th></tr></thead><tbody>
      ${(() => { let run = P.computed_signed - tx.reduce((s, t) => s + t.effect, 0) + tx.slice(0, tx.length - shown.length).reduce((s, t) => s + t.effect, 0);
        return shown.map(t => { run += t.effect; return `<tr><td><input type="checkbox" class="rcc" value="${t.id}" ${cleared.has(t.id) ? 'checked' : ''}></td><td class="nowrap small">${esc(t.date.slice(0, 16))}</td>
          <td><span class="pill ${t.kind}">${kindLabel[t.kind] || t.kind}</span> ${esc(t.label)}${t.kind === 'transfer' && t.other_account ? ` <span class="small muted">↔ ${esc(t.other_account)}</span>` : ''}${t.note && t.note !== t.label ? `<div class="small muted">${esc(t.note)}</div>` : ''}</td>
          <td class="right ${t.effect < 0 ? 'neg' : 'pos'}">${t.effect > 0 ? '+' : ''}${num(t.effect)}</td><td class="right small muted">${num(run * sign)}</td></tr>`; }).join(''); })()}
      </tbody></table></div>`;
    $$('.rcc', bg).forEach(c => c.onchange = () => { c.checked ? cleared.add(+c.value) : cleared.delete(+c.value); summary(); });
    const all = $('#rcAll', bg); if (all) { all.onclick = () => { shown.forEach(t => cleared.add(t.id)); list(); summary(); }; $('#rcNone', bg).onclick = () => { cleared.clear(); list(); summary(); }; }
  };
  const load = async (full) => {
    const my = ++seq;
    const q = new URLSearchParams({as_of: f.as_of.value}); if (f.balance.value !== '') q.set('balance', f.balance.value);
    try { const p = await api(`/api/accounts/${a.id}/reconcile?` + q); if (my !== seq) return; P = p; }
    catch (e) { if (my !== seq) return; P = null; $('#rcSum', bg).innerHTML = `<div class="rec-warn">${esc(e.message)}</div>`; $('#rcList', bg).innerHTML = ''; $('[data-ok]', bg).disabled = true; return; }
    if (full) list(); summary();
  };
  let tmr; f.balance.oninput = () => { clearTimeout(tmr); tmr = setTimeout(() => load(false), 200); };
  f.as_of.onchange = () => load(true);
  load(true);
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-hist]', bg).onclick = () => { bg.close(); reconcileHistory(a); };
  $('[data-ok]', bg).onclick = async () => {
    if (f.balance.value === '') return toast('Enter the statement balance', true);
    const mode = $('input[name=rcMode]:checked', bg);
    const body = {balance: +f.balance.value, as_of: f.as_of.value, note: f.note.value, cleared_ids: [...cleared]};
    if (mode && mode.value === 'adjust') body.adjust_reason = $('#rcReason', bg).value;
    const r = await guard(() => api(`/api/accounts/${a.id}/reconcile`, {method: 'POST', body}));
    bg.close(); toast(r.message); await loadMeta(); refresh();
  };
}
async function reconcileHistory(a) {
  const d = await guard(() => api(`/api/accounts/${a.id}/reconciliations`));
  const items = d.items, cur = a.currency;
  const sgn = v => Math.abs(v) < 0.005 ? '<span class="muted">0.00</span>' : `<b class="neg">${v > 0 ? '+' : ''}${num(v)}</b>`;
  const bg = modal('Reconciliation history — ' + esc(a.name), !items.length ? '<div class="empty">Not reconciled yet. Use ✔ Reconcile on the Accounts page.</div>' : `
    <p class="small muted">Today's balance is anchored to the latest checkpoint plus the transactions after it. <b>Drift</b> = what the transactions now add up to (from the previous checkpoint) minus the statement balance; non-zero means a transaction in that period was added, edited or deleted after reconciling (or a difference was accepted). <b>Changed since</b> = change caused by later edits.</p>
    <div class="scroll-x"><table class="t"><thead><tr><th>Statement date</th><th class="right">${a.is_liability ? 'Owed' : 'Balance'}</th><th class="right">Drift</th><th class="right">Changed since</th><th class="right">Cleared</th><th>Note</th><th></th></tr></thead><tbody>
    ${items.map(c => `<tr><td class="nowrap">✔ ${esc(c.as_of.slice(0, 10))}<div class="small muted">made ${esc((c.created_at || '').slice(0, 16).replace('T', ' '))}</div></td>
      <td class="right">${money(c.balance_shown, cur)}</td><td class="right">${sgn(c.drift)}</td><td class="right">${sgn(c.history_changed)}</td>
      <td class="right small">${c.cleared_count} / ${c.tx_count}</td>
      <td class="small">${esc(c.note || '')}${c.adjustment_tx_id ? ` <a href="javascript:void 0" data-otx="${c.adjustment_tx_id}">adjustment #${c.adjustment_tx_id}</a>` : ''}</td>
      <td>${c.is_latest ? '<button class="btn sm danger" data-undo>Undo</button>' : ''}</td></tr>`).join('')}</tbody></table></div>`,
    {wide: true, buttons: `<a class="btn" href="#/transactions/?account_id=${a.id}&reconciled=1">🧾 Reconciled transactions</a><span class="sp"></span><button class="btn" data-x2>Close</button><button class="btn pri" data-new>✔ Reconcile again</button>`});
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-new]', bg).onclick = () => { bg.close(); reconcileDialog(a); };
  $$('a[href^="#/transactions"]', bg).forEach(x => x.addEventListener('click', () => bg.close()));
  $$('[data-otx]', bg).forEach(x => x.onclick = () => openTx(+x.dataset.otx));
  const u = $('[data-undo]', bg); if (u) u.onclick = async () => {
    const top = items[0];
    const del = top.adjustment_tx_id ? await confirmBox(`Undo the ${top.as_of.slice(0, 10)} reconciliation?\n\nIts transactions are un-flagged.\nOK = also delete the balance adjustment #${top.adjustment_tx_id} it created · Cancel = choose next`) : false;
    if (!del && !await confirmBox(`Undo the ${top.as_of.slice(0, 10)} reconciliation${top.adjustment_tx_id ? ' and keep the adjustment transaction' : ''}? Its transactions are un-flagged; the balance is anchored to the previous checkpoint again.`)) return;
    const r = await guard(() => api(`/api/accounts/${a.id}/reconciliations/latest${del ? '?delete_adjustment=true' : ''}`, {method: 'DELETE'}));
    bg.close(); toast(`Undone: ${r.unflagged} transaction(s) un-flagged`); await loadMeta(); refresh();
  };
}
function groupsEditor(groups) {
  const bg = modal('Account groups', `<div id="gl">${groups.map(g => `<div class="row" style="margin-bottom:6px"><input value="${esc(g.name)}" data-g="${g.id}"><input type="number" value="${g.sort}" data-s="${g.id}" style="width:70px"><button class="btn sm danger" data-dg="${g.id}">✕</button></div>`).join('')}</div>
   <div class="row"><input id="ng" placeholder="New group name"><button class="btn" id="ag">Add</button></div>`, {buttons: '<button class="btn pri" data-ok>Save</button>'});
  $('#ag', bg).onclick = async () => { if (!$('#ng', bg).value) return; await guard(() => api('/api/account-groups', {method: 'POST', body: {name: $('#ng', bg).value}})); bg.close(); await refresh(); };
  $$('[data-dg]', bg).forEach(b => b.onclick = async () => { await guard(() => api('/api/account-groups/' + b.dataset.dg, {method: 'DELETE'})); b.parentElement.remove(); });
  $('[data-ok]', bg).onclick = async () => { for (const i of $$('[data-g]', bg)) await api('/api/account-groups/' + i.dataset.g, {method: 'PUT', body: {name: i.value, sort: +$(`[data-s="${i.dataset.g}"]`, bg).value}}); bg.close(); refresh(); };
}

/* ---------- investments ---------- */
VIEWS.investments = async (v, rest = []) => {
  // #/investments/account/<id>: the account detail page, opened inside the Investments section
  if (rest[0] === 'account') { window.scrollTo(0, 0); return VIEWS.account(v, rest.slice(1), {section: 'investments'}); }
  if (S.invPR) S.invPR.fromList = false;
  const d = await api('/api/investments');
  const totMV = d.holdings.reduce((s, h) => s + h.market_value, 0), totCost = d.holdings.reduce((s, h) => s + h.cost, 0);
  v.innerHTML = `${demoBanner()}<div class="grid g3">
   <div class="card stat"><div class="lbl">Market value</div><div class="val">${money(totMV)}</div></div>
   <div class="card stat"><div class="lbl">Cost basis</div><div class="val">${money(totCost)}</div></div>
   <div class="card stat"><div class="lbl">Unrealized P/L</div><div class="val ${totMV - totCost >= 0 ? 'pos' : 'neg'}">${money(totMV - totCost)}</div></div></div>
  <div class="card" style="margin-top:16px" id="prCard"></div>
  <div class="card" style="margin-top:16px" id="retCard"></div>
  <div class="card" style="margin-top:16px"><h3>Holdings<span class="sp"></span><button class="btn sm" id="refresh">↻ Refresh quotes (Yahoo)</button><button class="btn sm" id="manual">Set price manually</button><button class="btn sm pri" id="trade">+ Record trade</button></h3>
  ${!d.accounts.length ? '<div class="empty">Add a Brokerage / Retirement / Crypto account first (Accounts → Add account).</div>' : ''}
  ${d.holdings.length ? `<div class="scroll-x"><table class="t"><thead><tr><th>Account</th><th>Ticker</th><th class="right">Shares</th><th class="right">Avg cost</th><th class="right">Price</th><th class="right">Market value</th><th class="right">Cost</th><th class="right">Unrealized</th><th class="right">Dividends</th><th class="right">Realized</th></tr></thead><tbody>
   ${d.holdings.map(h => `<tr><td class="small">${esc(h.account_name)}</td><td><b>${esc(h.ticker)}</b></td><td class="right">${num(h.shares, 6)}</td><td class="right">${num(h.avg_cost, 4)}</td>
   <td class="right" title="${esc(h.price_source)}">${num(h.price, 4)}<div class="small muted">${esc((h.price_source || '').split(' @ ')[0])}</div></td><td class="right"><b>${money(h.market_value)}</b></td><td class="right">${money(h.cost)}</td>
   <td class="right ${h.unrealized >= 0 ? 'pos' : 'neg'}">${money(h.unrealized)} (${h.unrealized_pct}%)</td><td class="right">${money(h.dividends)}</td><td class="right">${money(h.realized)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No holdings yet</div>'}</div>
  <div class="grid g2" style="margin-top:16px"><div class="card"><h3>Investment accounts</h3>${d.accounts.map(a => `<div class="acct"><div class="nm"><b>${esc(a.name)}</b><span class="small muted">Cash ${money(a.cash_balance, a.currency)} · Holdings ${money(a.holdings_value, a.currency)}</span></div><div class="bal">${money(a.balance, a.currency)}</div></div>`).join('') || '<div class="empty">None</div>'}</div>
  <div class="card"><h3>Recent trades</h3>${txTable(d.trades.slice(0, 20), {compact: true})}</div></div>`;
  bindTxRows(v);
  Promise.all([renderPR(), renderReturns()]).then(() => {  // coming back from an account: restore scroll position
    const y = S.invPR && S.invPR.scroll; if (y && location.hash.replace(/\/$/, '') === '#/investments') { window.scrollTo(0, y); S.invPR.scroll = 0; }
  });
  const inv = S.meta.accounts.find(a => S.meta.account_types[a.type].investment && !a.archived);
  $('#trade').onclick = () => openTx(null, null, {kind: 'buy', account_id: inv ? inv.id : ''});
  $('#refresh').onclick = async () => { const r = await guard(() => api('/api/prices/refresh', {method: 'POST'})); toast(`Updated ${Object.keys(r.updated).length}${r.failed.length ? ', failed: ' + r.failed.join(', ') : ''}`, !!r.failed.length); route(); };
  $('#manual').onclick = () => { const bg = modal('Set price manually', `<form class="form" id="mp"><label class="f"><span>Ticker</span><input name="ticker" required></label><label class="f"><span>Price</span><input name="price" type="number" step="any" required></label><label class="f"><span>Currency</span><select name="currency">${curOptions('USD')}</select></label></form>`, {buttons: '<button class="btn pri" data-ok>Save</button>'});
    $('[data-ok]', bg).onclick = async () => { await guard(() => api('/api/prices', {method: 'POST', body: Object.fromEntries(new FormData($('#mp', bg)))})); bg.close(); route(); }; };
};

/* ---------- investment returns 投资收益 ---------- */
const RET_TIPS = {
  gain: 'Gain = returns, same as the account pages: Investment gain/loss and Interest adjustments, dividends, fees and market gains on tracked holdings. Unclassified adjustments are NOT gain; they are in Unexplained.',
  unexp: 'Unexplained = unclassified balance adjustments (e.g. imported balance changes; reclassify them) + reconciliation gaps. End value − start value = net contributions + gain + unexplained.',
  contrib: 'Principal put in: transfers in, income recorded on the account, and adjustments classified Opening/Unrecorded (same as the account pages).',
  withdraw: 'Money taken out: transfers out to non-investment accounts (for a group: outside the group) and expenses paid from the account.',
  simple: 'Simple return = gain ÷ (start value + net contributions). Easy to read but ignores WHEN money was added.',
  md: 'Money-weighted return (Modified Dietz) = gain ÷ (start value + Σ contribution × share of the period it was invested). Money added late in the period counts less. Not annualized.',
  ann: 'Annualized = (1 + Modified Dietz)^(365 ÷ days) − 1, only for periods longer than a year. XIRR = the yearly rate that makes start value, contributions and end value balance out (money-weighted, annualized).'};
const pctCell = (v, cls = true) => v === null || v === undefined ? '<span class="muted">—</span>' : `<span class="${cls ? (v >= 0 ? 'pos' : 'neg') : ''}">${v >= 0 ? '+' : ''}${num(v)}%</span>`;
async function renderReturns() {
  const el = $('#retCard'); if (!el) return;
  S.ret = S.ret || {period: 'ytd', start: '', end: '', scope: 'all'};
  const r = S.ret;
  const qs = new URLSearchParams({period: r.period, scope: r.scope, ...(r.period === 'custom' ? {start: r.start, end: r.end || today()} : {})});
  let d;
  try { d = await api('/api/investments/returns?' + qs); } catch (e) { el.innerHTML = `<h3>Investment returns</h3><div class="neg">${esc(e.message)}</div>`; return; }
  if (!el.isConnected) return;  // user navigated away while loading (e.g. to Review): nothing to bind
  const cur = d.currency;
  const ordered = [];
  const total = d.rows.find(x => x.level === 'total');
  d.rows.filter(x => x.level === 'group').forEach(g => { ordered.push(g); d.rows.filter(x => x.level === 'account' && x.group_id === g.id).forEach(a => ordered.push(a)); });
  const key = x => x.level === 'total' ? 'all' : `${x.level}:${x.id}`;
  const row = x => `<tr class="ret-${x.level} ${key(x) === r.scope ? 'sel' : ''}" data-scope="${key(x)}" title="Click to chart this ${x.level === 'total' ? 'total' : x.level}">
    <td>${x.level === 'account' ? '<span class="ind"></span>' : ''}${x.level === 'account' ? esc(x.name) : `<b>${esc(x.name)}</b>`}${x.archived ? ' <span class="small muted">(archived)</span>' : ''}</td>
    <td class="right">${money(x.start_value, cur)}</td><td class="right">${money(x.contributions, cur)}</td><td class="right">${money(x.withdrawals, cur)}</td>
    <td class="right">${money(x.end_value, cur)}</td><td class="right"><b class="${x.gain >= 0 ? 'pos' : 'neg'}">${money(x.gain, cur)}</b></td>
    <td class="right">${Math.abs(x.unexplained || 0) >= 0.01 ? `<span class="unexp-txt">${money(x.unexplained, cur)}</span>` : '<span class="muted">—</span>'}</td>
    <td class="right">${pctCell(x.simple_return)}</td><td class="right">${pctCell(x.md_return)}</td>
    <td class="right">${x.days > 365 ? pctCell(x.annualized) + (x.xirr !== null ? `<div class="small muted">XIRR ${pctCell(x.xirr, false)}</div>` : '') : r.period === 'all' && x.xirr !== null ? `<span class="small muted">XIRR</span> ${pctCell(x.xirr)}` : '<span class="muted">—</span>'}</td></tr>`;
  const comp = total ? total.components : {};
  el.innerHTML = `<h3>Investment returns<span class="sp"></span>
    <select id="retP" style="width:auto">${[['month', 'This month'], ['ytd', 'Year to date'], ['1y', 'Last 12 months'], ['all', 'All time'], ['custom', 'Custom']].map(([k, l]) => `<option value="${k}" ${r.period === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
    <span id="retCustom" style="display:${r.period === 'custom' ? 'inline-flex' : 'none'};gap:4px"><input type="date" id="retS" value="${r.start || d.period.start}" style="width:auto"><input type="date" id="retE" value="${r.end || d.period.end}" style="width:auto"><button class="btn sm" id="retGo">Go</button></span>
    <button class="btn sm" id="reclass" title="One-time tool: classify imported balance-change adjustments on investment accounts">🏷 Reclassify adjustments…</button></h3>
   <div class="small muted" style="margin:-4px 0 10px">${esc(d.period.start)} → ${esc(d.period.end)} (${d.period.days} days) · values in ${esc(cur)} · investment-type accounts (brokerage, retirement, crypto). Transfers between them are not contributions in group/total rows.</div>
   ${!d.rows.length || !total || !total.account_ids.length ? '<div class="empty">No investment-type accounts yet (Brokerage / Retirement / Crypto).</div>' : `
   <div class="grid g4 ret-stats">
     <div class="stat"><div class="lbl" title="${RET_TIPS.gain}">Gain 收益 ⓘ</div><div class="val ${total.gain >= 0 ? 'pos' : 'neg'}">${money(total.gain, cur)}</div><div class="small muted">${[['Market', comp.market], ['Gain/loss adj.', comp.investment_gain], ['Interest', comp.interest], ['Dividends', comp.dividends], ['Fees', comp.fees]].filter(([, v]) => v).map(([l, v]) => `${l} ${money(v, cur)}`).join(' · ') || '—'}</div></div>
     <div class="stat"><div class="lbl" title="${RET_TIPS.contrib}">Net contributions 净投入 ⓘ</div><div class="val">${money(total.net_contributions, cur)}</div><div class="small muted">in ${money(total.contributions, cur)} · out ${money(total.withdrawals, cur)}</div></div>
     <div class="stat"><div class="lbl" title="${RET_TIPS.simple}">Simple return 简单收益率 ⓘ</div><div class="val">${pctCell(total.simple_return)}</div></div>
     <div class="stat"><div class="lbl" title="${RET_TIPS.md}">Money-weighted 资金加权 ⓘ</div><div class="val">${pctCell(total.md_return)}</div><div class="small muted" title="${RET_TIPS.ann}">${total.days > 365 ? `annualized ${pctCell(total.annualized, false)} · ` : ''}${total.xirr !== null ? `XIRR ${pctCell(total.xirr, false)}/yr` : ''}</div></div>
   </div>
   <div class="scroll-x" style="margin-top:12px"><table class="t ret-table"><thead><tr><th>Account / group</th><th class="right">Start value</th><th class="right" title="${RET_TIPS.contrib}">Contributions 投入 ⓘ</th><th class="right" title="${RET_TIPS.withdraw}">Withdrawals 取出 ⓘ</th><th class="right">End value</th><th class="right" title="${RET_TIPS.gain}">Gain 收益 ⓘ</th><th class="right" title="${RET_TIPS.unexp}">Unexplained 未分类 ⓘ</th><th class="right" title="${RET_TIPS.simple}">Simple ⓘ</th><th class="right" title="${RET_TIPS.md}">Money-wtd ⓘ</th><th class="right" title="${RET_TIPS.ann}">Annualized ⓘ</th></tr></thead>
   <tbody>${total ? row(total) : ''}${ordered.map(row).join('')}</tbody></table></div>
   <div class="grid g2" style="margin-top:14px">
     <div><h4 style="margin:0 0 6px">Cumulative gain · ${esc(d.scope.name)}</h4><div class="chart-box" style="height:260px"><canvas id="retChart"></canvas></div></div>
     <div><h4 style="margin:0 0 6px">Returns by year · ${esc(d.scope.name)}</h4>${d.scope.years.length ? `<div class="scroll-x"><table class="t"><thead><tr><th>Year</th><th class="right">Start</th><th class="right">Net in</th><th class="right">Gain</th><th class="right" title="${RET_TIPS.md}">Money-wtd</th><th class="right" title="${RET_TIPS.simple}">Simple</th></tr></thead><tbody>${d.scope.years.slice().reverse().map(y => `<tr><td>${y.year}${y.end.slice(5) !== '12-31' ? ' <span class="small muted">to ' + y.end.slice(5) + '</span>' : ''}</td><td class="right">${money(y.start_value, cur)}</td><td class="right">${money(y.net_contributions, cur)}</td><td class="right ${y.gain >= 0 ? 'pos' : 'neg'}">${money(y.gain, cur)}</td><td class="right">${pctCell(y.md_return)}</td><td class="right">${pctCell(y.simple_return)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No data</div>'}</div>
   </div>`}`;
  bind('#retP', 'change', e => { r.period = e.target.value; if (r.period !== 'custom') renderReturns(); else { const c = $('#retCustom', el); if (c) c.style.display = 'inline-flex'; } }, el);
  bind('#retGo', 'click', () => { r.start = ($('#retS', el) || {}).value || ''; r.end = ($('#retE', el) || {}).value || ''; renderReturns(); }, el);
  bind('#reclass', 'click', () => reclassifyTool(), el);
  $$('[data-scope]', el).forEach(tr => tr.onclick = () => { r.scope = tr.dataset.scope; renderReturns(); });
  if ($('#retChart', el)) {
    const sr = d.scope.series;
    chart('retChart', {type: 'line', data: {labels: sr.map(x => x.date), datasets: [
      {label: 'Cumulative gain', data: sr.map(x => x.cumulative_gain), borderColor: '#8a5cd6', backgroundColor: 'rgba(138,92,214,.12)', fill: true, tension: .2, pointRadius: sr.length > 40 ? 0 : 2},
      {label: 'Net contributions', data: sr.map(x => x.net_contributions), borderColor: '#9aa3b2', borderDash: [5, 4], pointRadius: 0, fill: false}]},
      options: {interaction: {mode: 'index', intersect: false}, scales: {x: {ticks: {maxTicksLimit: 8}}}}});
  }
}

/* one-time tool: classify old (e.g. 随手记 余额变更) adjustments — preview first, nothing changes until Apply */
async function reclassifyTool(includeSavings = false, accountIds = null) {
  const d = await guard(() => api('/api/adjustments/reclassify?include_savings=' + (includeSavings ? 1 : 0) + (accountIds ? `&only=1&account_ids=${accountIds.join(',')}` : '')));
  const cnt = d.by_proposed_reason;
  const bg = modal('Reclassify adjustments', `<p class="muted" style="margin-top:0">Unclassified balance adjustments (e.g. imported 随手记 <b>余额变更</b>) on ${accountIds ? 'this account' : `investment-type accounts${includeSavings ? ' and savings accounts' : ''}`}, with a proposed <b>reason for difference</b>. This is a <b>preview</b>: nothing changes until you press <b>Apply</b> (a backup is taken first). An account's first record is proposed as <i>Opening balance</i>, because exports don't include starting balances.</p>
    <div class="row" style="margin-bottom:8px"><label class="chk"><input type="checkbox" id="rcSav" ${includeSavings ? 'checked' : ''}> include savings accounts (propose Interest 利息)</label><span class="sp"></span><label class="chk"><input type="checkbox" id="rcAll" checked> select all</label></div>
    <div class="small" style="margin-bottom:8px"><b>${d.count}</b> adjustments on ${d.accounts} accounts · proposed: ${Object.entries(cnt).map(([k, n]) => `${esc((S.meta.adj_reasons.find(r => r.code === k) || {}).name || k)} ${n}`).join(' · ') || '—'}</div>
    ${d.rows.length ? `<div class="scroll-x" style="max-height:52vh;overflow:auto"><table class="t"><thead><tr><th></th><th>Date</th><th>Account</th><th class="right">Amount</th><th>Reason</th><th>Why</th></tr></thead><tbody>
    ${d.rows.map(x => `<tr><td><input type="checkbox" data-rc="${x.id}" checked></td><td class="nowrap">${esc(x.date.slice(0, 10))}</td><td>${esc(x.account_name)}</td><td class="right ${x.amount >= 0 ? 'pos' : 'neg'}">${money(x.amount, x.currency)}</td>
      <td><select data-rr="${x.id}" style="width:auto">${reasonOptions(x.proposed_reason, {opening: true})}</select></td><td class="small muted">${esc(x.why)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">Nothing to reclassify 🎉</div>'}`,
    {wide: true, buttons: `<button class="btn" data-x2>Close</button><button class="btn pri" data-ok ${d.rows.length ? '' : 'disabled'}>Apply to selected</button>`});
  const upd = () => { const n = $$('[data-rc]:checked', bg).length; const b = $('[data-ok]', bg); b.textContent = `Apply to ${n} selected`; b.disabled = !n; };
  $$('[data-rc]', bg).forEach(c => c.onchange = upd); upd();
  $('#rcAll', bg).onchange = e => { $$('[data-rc]', bg).forEach(c => c.checked = e.target.checked); upd(); };
  $('#rcSav', bg).onchange = e => { bg.close(); reclassifyTool(e.target.checked, accountIds); };
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const items = $$('[data-rc]:checked', bg).map(c => ({id: +c.dataset.rc, reason: $(`[data-rr="${c.dataset.rc}"]`, bg).value}));
    if (!items.length || !await confirmBox(`Reclassify ${items.length} adjustments? A backup is taken first; you can change any of them later on the transaction.`)) return;
    const r = await guard(() => api('/api/adjustments/reclassify', {method: 'POST', body: {items}}));
    bg.close(); toast(`Reclassified ${r.updated} adjustments (backup ${r.backup})`); route();
  };
}

/* ---------- budgets ---------- */
VIEWS.budgets = async (v) => {
  S.bMonth = S.bMonth || thisMonth();
  const d = await api(`/api/budgets?month=${S.bMonth}&ledger_id=${S.ledger || 1}`);
  const prev = (() => { const [y, m] = S.bMonth.split('-').map(Number); const dt = new Date(y, m - 2, 1); return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}`; })();
  v.innerHTML = `${demoBanner()}<div class="card"><h3>Budgets<span class="sp"></span><input type="month" id="bm" value="${S.bMonth}" style="width:auto"><button class="btn sm" id="copy">Copy from ${prev}</button><button class="btn sm pri" id="add">+ Set budget</button></h3>
   ${d.items.length ? d.items.map(b => `<div class="row" style="align-items:flex-start"><div style="flex:1">${budgetRow(b)}</div><button class="btn sm" data-e="${b.category_id ?? ''}" data-a="${b.amount}">Edit</button><button class="btn sm danger" data-d="${b.id}">✕</button></div>`).join('') : '<div class="empty">No budgets for this month. Set a total budget and/or per-category budgets.</div>'}</div>`;
  $('#bm').onchange = e => { S.bMonth = e.target.value; route(); };
  $('#copy').onclick = async () => { await guard(() => api('/api/budgets/copy', {method: 'POST', body: {from: prev, to: S.bMonth, ledger_id: S.ledger || 1}})); route(); };
  const form = (cid = '', amt = '') => { const bg = modal('Budget ' + S.bMonth, `<form class="form" id="bf"><label class="f full"><span>Category (empty = total budget)</span><select name="category_id">${catOptions('expense', cid).replace('— Category —', 'Total budget')}</select></label><label class="f"><span>Monthly amount (${S.meta.settings.base_currency})</span><input name="amount" type="number" step="0.01" value="${amt}"></label></form>`, {buttons: '<button class="btn pri" data-ok>Save</button>'});
    $('[data-ok]', bg).onclick = async () => { const o = Object.fromEntries(new FormData($('#bf', bg))); await guard(() => api('/api/budgets', {method: 'POST', body: {...o, month: S.bMonth, ledger_id: S.ledger || 1}})); bg.close(); route(); }; };
  $('#add').onclick = () => form();
  $$('[data-e]').forEach(b => b.onclick = () => form(b.dataset.e, b.dataset.a));
  $$('[data-d]').forEach(b => b.onclick = async () => { await api('/api/budgets/' + b.dataset.d, {method: 'DELETE'}); route(); });
};

/* ---------- reports ---------- */
VIEWS.reports = async (v) => {
  const y = new Date().getFullYear();
  S.rep = S.rep || {start: `${y}-01-01`, end: today(), group_by: 'category', kind: 'expense'};
  const r = S.rep;
  v.innerHTML = `<div class="card"><form id="rf" class="row"><input type="date" name="start" value="${r.start}" style="width:auto"> → <input type="date" name="end" value="${r.end}" style="width:auto">
   <select name="kind" style="width:auto"><option value="expense" ${r.kind === 'expense' ? 'selected' : ''}>Expense</option><option value="income" ${r.kind === 'income' ? 'selected' : ''}>Income</option></select>
   <select name="group_by" style="width:auto">${[['category', 'Category'], ['subcategory', 'Sub-category'], ['month', 'Month'], ['account', 'Account'], ['member', 'Member'], ['merchant', 'Merchant'], ['project', 'Project'], ['tag', 'Tag']].map(([k, l]) => `<option value="${k}" ${r.group_by === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
   <select name="project_id" style="width:auto">${listOptions(S.meta.projects, r.project_id, 'All projects')}</select>
   <button class="btn pri">Run</button><span class="sp"></span><span id="rt" class="muted"></span></form></div>
   <div class="grid g2" style="margin-top:16px"><div class="card"><h3>Chart</h3><div class="chart-box" style="height:320px"><canvas id="rc"></canvas></div></div><div class="card"><h3>Table</h3><div id="rtab"></div></div></div>
   <div class="card" style="margin-top:16px"><h3>Tax &amp; tips 税/小费<span class="sp"></span><span id="ttTot" class="small muted"></span></h3><div class="small muted">Only transactions where tax or tip was recorded. Tip % = tips ÷ subtotal (amount − tax − tip).</div><div class="grid g2"><div class="chart-box" style="height:260px"><canvas id="rtt"></canvas></div><div id="ttTab"></div></div></div>
   <div class="card" style="margin-top:16px"><h3>Net worth history (24 months)</h3><div class="chart-box"><canvas id="rnw"></canvas></div></div>`;
  $('#rf').onsubmit = e => { e.preventDefault(); Object.assign(r, Object.fromEntries(new FormData(e.target))); route(); };
  const d = await api(`/api/reports?${new URLSearchParams(Object.entries({...r, ...(S.ledger ? {ledger_id: S.ledger} : {})}).filter(([, x]) => x !== '' && x != null))}`);
  $('#rt').innerHTML = `Income ${money(d.totals.income)} · Expense ${money(d.totals.expense)} · Net ${money(d.totals.net)} · <span title="Investment gain/loss + interest + dividends − investment fees (balance adjustments classified by reason). Not part of income/expense.">Investment income <b class="${d.totals.investment_income >= 0 ? 'pos' : 'neg'}">${money(d.totals.investment_income)}</b></span>`;
  const rows = r.group_by === 'month' ? [...d.rows].sort((a, b) => String(a.key).localeCompare(String(b.key))) : d.rows;
  $('#rtab').innerHTML = rows.length ? `<table class="t"><thead><tr><th>${esc(r.group_by)}</th><th class="right">Count</th><th class="right">Amount</th><th class="right">%</th></tr></thead><tbody>${rows.map(x => `<tr><td>${esc(x.name)}</td><td class="right">${x.count}</td><td class="right">${money(x.amount)}</td><td class="right">${x.pct}%</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No data</div>';
  if (rows.length) chart('rc', r.group_by === 'month' ? {type: 'bar', data: {labels: rows.map(x => x.name), datasets: [{label: r.kind, data: rows.map(x => x.amount), backgroundColor: r.kind === 'income' ? '#14a06f' : '#e8553e'}]}} :
    {type: 'pie', data: {labels: rows.map(x => x.name), datasets: [{data: rows.map(x => x.amount), backgroundColor: PALETTE}]}, options: {plugins: {legend: {position: 'right'}}}});
  const ttg = ['category', 'subcategory', 'month', 'account', 'merchant', 'project'].includes(r.group_by) ? r.group_by : 'month';
  const tt = await api(`/api/reports/tax-tips?${new URLSearchParams(Object.entries({start: r.start, end: r.end, kind: r.kind, group_by: ttg, project_id: r.project_id, ledger_id: S.ledger}).filter(([, x]) => x !== '' && x != null))}`);
  const tx = tt.totals, pc = x => x == null ? '—' : x + '%';
  $('#ttTot').innerHTML = tx.count ? `<span>Tax</span> ${money(tx.tax)} (${tx.tax_count}) · <span>Tips</span> ${money(tx.tip)} (${tx.tip_count}) · <span>Tip %</span> ${pc(tx.tip_pct)}` : '';
  $('#ttTab').innerHTML = tt.rows.length ? `<table class="t"><thead><tr><th>${esc(ttg)}</th><th class="right">Count</th><th class="right">Tax</th><th class="right">Tax %</th><th class="right">Tips</th><th class="right">Tip %</th></tr></thead><tbody>${tt.rows.map(x => `<tr><td data-notr>${esc(x.name || '—')}</td><td class="right">${x.count}</td><td class="right">${x.tax_count ? money(x.tax) : '—'}</td><td class="right">${pc(x.tax_pct)}</td><td class="right">${x.tip_count ? money(x.tip) : '—'}</td><td class="right">${pc(x.tip_pct)}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No transactions with tax or tip in this period</div>';
  if (tt.rows.length) chart('rtt', {type: 'bar', data: {labels: tt.rows.map(x => x.name || '—'), datasets: [{label: i18nText('Tax'), data: tt.rows.map(x => x.tax), backgroundColor: '#3a7bd5'}, {label: i18nText('Tips'), data: tt.rows.map(x => x.tip), backgroundColor: '#e3a21a'}]}, options: {scales: {x: {stacked: true}, y: {stacked: true}}}});
  const nw = await api('/api/networth?months=24');
  chart('rnw', {type: 'line', data: {labels: nw.history.map(x => x.month), datasets: [{label: 'Net worth', data: nw.history.map(x => x.net_worth), borderColor: '#3a7bd5', fill: false}, {label: 'Assets', data: nw.history.map(x => x.assets), borderColor: '#14a06f'}, {label: 'Liabilities', data: nw.history.map(x => x.liabilities), borderColor: '#e0483a'}]}});
};

/* ---------- quick add & review ---------- */
VIEWS.review = async (v) => {
  const d = await api('/api/review');
  v.innerHTML = `${demoBanner()}<div class="card"><h3>Quick add</h3>
   <form id="qa2" class="row"><input id="qa2i" placeholder="lunch 25.5 chase sapphire · 午饭 35 支付宝 · salary 5000 to checking · paid 300 from checking to amex" style="flex:1"><label class="chk small"><input type="checkbox" id="qa2r"> always review</label><button type="button" class="btn" id="qa2f">✎ Open in form</button><button class="btn pri">Add</button></form><div class="small muted hide-m" id="qa2hint"></div>
   <div id="qaPrev" class="small muted" style="margin-top:8px;min-height:20px"></div>
   <details class="small muted" style="margin-top:6px"><summary>Syntax help</summary><p>Write the amount, an account name/alias and a keyword. Optional: <code>yesterday</code>/<code>昨天</code>/<code>2026-10-01</code>/<code>10/01</code>, <code>#tag</code>, <code>at Merchant</code>, currency (<code>$</code>, <code>¥</code>, <code>元</code>, <code>usd</code>, <code>rmb</code>). Transfers: <code>from A to B</code>, <code>从A转500到B</code>, <code>pay amex 300 from checking</code>. Refunds: <code>refund 20 amazon chase</code>. Entries below the confidence threshold (${S.meta.settings.review_threshold}) land here for review.</p></details></div>
  ${(d.holds || []).length ? `<div class="card rs-review" style="margin-top:16px"><h3>🔒 Card holds found in emails (${d.holds.length})</h3><div class="small muted">A hold is not spending: confirm it to track it in Reserves, or turn it into a charge if the money was really taken.</div>${d.holds.map(rsReviewRow).join('')}</div>` : ''}
  <div class="card" style="margin-top:16px"><h3>To review (${d.total})<span class="sp"></span>${d.total ? '<button class="btn sm danger" id="dismissAll">Dismiss all</button>' : ''}</h3>
   ${d.items.length ? `<div class="scroll-x"><table class="t resp"><thead><tr><th>When</th><th>Source</th><th>Text / parsed</th><th class="right">Amount</th><th>Conf.</th><th></th></tr></thead><tbody>${d.items.map(t => `<tr>
     <td class="small nowrap hide-m">${esc(t.date.slice(0, 16))}</td><td class="hide-m"><span class="pill">${esc(t.source)}</span></td>
     <td><div class="small mono">${esc((t.raw_text || t.note || '').slice(0, 140))}</div><div class="small muted">${kindLabel[t.kind]} · ${esc(t.account_name || '⚠ no account')}${t.to_account_name ? ' → ' + esc(t.to_account_name) : ''} · ${esc(t.category_label || 'no category')}${t.merchant_name ? ' · ' + esc(t.merchant_name) : ''}</div></td>
     <td class="tx-amt">${money(t.amount, t.currency)}</td><td>${confPill(t.confidence)}</td>
     <td class="nowrap"><button class="btn sm" data-edit="${t.id}" title="Open in the form">✎ Review</button> <button class="btn sm pri" data-ok="${t.id}">✓</button> <button class="btn sm" data-tors="${t.id}" title="It is a card hold or a deposit, not spending">🔒 Reserve</button> <button class="btn sm danger" data-del="${t.id}">✕</button></td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">🎉 Nothing to review</div>'}</div>`;
  let h;
  $('#qa2i').oninput = () => { clearTimeout(h); h = setTimeout(async () => { const tx = $('#qa2i').value.trim(); if (!tx) { $('#qaPrev').innerHTML = ''; return; } let r; try { r = await api('/api/quick-add', {method: 'POST', body: {text: tx, dry_run: true}}); } catch (e) { $('#qaPrev').innerHTML = `<span class="neg">${esc(e.message)}</span>`; return; } $('#qaPrev').innerHTML = previewText(r); }, 250); };
  wireQuickAdd($('#qa2'), $('#qa2i'), $('#qa2f'), async () => { await doQuickAdd($('#qa2i').value, $('#qa2r').checked); $('#qa2i').value = ''; route(); });
  $('#qa2hint').textContent = txPrefs().quickadd_enter === 'form' ? 'Enter opens the pre-filled form · Shift+Enter adds right away (change in Settings → General)' : 'Enter adds right away · Shift+Enter opens the pre-filled form to review (change in Settings → General)';
  $$('[data-edit]').forEach(b => b.onclick = () => openTx(+b.dataset.edit));
  rsBindReview(v, async () => { await loadMeta(); route(); });
  $$('[data-tors]', v).forEach(b => b.onclick = () => { const x = d.items.find(i => i.id == b.dataset.tors) || {};
    reserveForm({fromReview: x.id, preset: {account_id: x.account_id, amount: x.orig_currency ? x.orig_amount : x.amount, currency: x.orig_currency || x.currency, merchant: x.merchant_name || '', start_date: (x.date || '').slice(0, 10), category_id: x.kind === 'expense' ? x.category_id : null, note: x.note || ''},
      after: async () => { await loadMeta(); route(); }}); });
  $$('[data-ok]').forEach(b => b.onclick = async () => { try { await api(`/api/transactions/${b.dataset.ok}/approve`, {method: 'POST', body: {}}); toast('Approved'); await loadMeta(); route(); } catch (e) { toast(e.message + ' — opening editor', true); openTx(+b.dataset.ok); } });
  $$('[data-del]').forEach(b => b.onclick = async () => { await api('/api/transactions/' + b.dataset.del, {method: 'DELETE'}); await loadMeta(); route(); });
  const da = $('#dismissAll'); if (da) da.onclick = async () => { if (!await confirmBox('Delete all items in the review inbox?')) return; await api('/api/transactions/bulk-delete', {method: 'POST', body: {ids: d.items.map(x => x.id)}}); await loadMeta(); route(); };
};
function previewText(r) {
  if (r.action === 'set_balance') return `→ <b>⚖️ Set balance</b> ${esc(r.message || '')}`;
  const p = r.parsed; const a = acctById(p.account_id), b = acctById(p.to_account_id);
  const cat = (() => { for (const root of S.meta.categories) for (const c of root.children) if (c.id === p.category_id) return `${root.name} › ${c.name} ${c.name_zh}`; return ''; })();
  return `→ <b>${kindLabel[p.kind]}</b> ${p.amount ?? '?'} ${p.currency || ''} · ${a ? esc(a.name) : '⚠ no account'}${b ? ' → ' + esc(b.name) : ''}${cat ? ' · ' + esc(cat) : ''}${p.merchant ? ' · @' + esc(p.merchant) : ''}${p.tags.length ? ' · #' + p.tags.map(esc).join(' #') : ''} · ${esc(p.date)} ${confPill(r.confidence)} → <b>${r.status === 'posted' ? 'will post' : 'goes to review'}</b>`;
}
async function doQuickAdd(text, review = false) {
  if (!text.trim()) return;
  const r = await guard(() => api('/api/quick-add', {method: 'POST', body: {text, source: 'web', review}}));
  if (r.action === 'set_balance' || r.action === 'reconcile') { toast(r.detail || r.message); await loadMeta(); return r; }
  toast(r.status === 'posted' ? `✓ Recorded ${r.transaction.amount} ${r.transaction.currency} (${Math.round(r.confidence * 100)}%)` : `📥 Sent to review (confidence ${Math.round(r.confidence * 100)}%)`);
  await loadMeta();
  return r;
}

/* ---------- scheduled transactions 周期记账 ---------- */
const WDAY = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const FREQ_UNIT = {daily: 'day(s)', weekly: 'week(s)', monthly: 'month(s)', quarterly: 'quarter(s)', yearly: 'year(s)'};
function schedFields(s) {
  const st = s.start_date || today(), sd = new Date(st + 'T12:00');
  const wd = (sd.getDay() + 6) % 7;
  const wds = s.weekdays_list || (s.weekdays ? String(s.weekdays).split(',').map(Number) : [wd]);
  const ends = s.end_date ? 'date' : s.max_count ? 'count' : 'never';
  const mm = s.month_mode || 'day';
  return `<fieldset class="full sched-box"><legend>🔁 Repeat</legend>
   <div class="form">
    <label class="f full"><span>Name</span><input name="s_name" value="${esc(s.name || '')}" placeholder="e.g. Rent 房租, Netflix, Salary 工资 (optional)"></label>
    <label class="f"><span>Repeat</span><select name="s_freq">${[['once', 'Once'], ['daily', 'Daily / every N days'], ['weekly', 'Weekly / every N weeks'], ['monthly', 'Monthly / every N months'], ['quarterly', 'Quarterly'], ['yearly', 'Yearly']].map(([k, l]) => `<option value="${k}" ${(s.freq || 'monthly') === k ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
    <label class="f" data-s="daily weekly monthly quarterly yearly"><span>Every</span><span class="row" style="gap:6px;flex-wrap:nowrap"><input name="s_interval" type="number" min="1" value="${s.interval || 1}" style="width:80px"><span id="sUnit" class="muted"></span></span></label>
    <div class="f full" data-s="weekly"><span>On</span><div class="wd-chips">${WDAY.map((l, i) => `<label class="chip"><input type="checkbox" name="s_wd" value="${i}" ${wds.includes(i) ? 'checked' : ''}><span>${l}</span></label>`).join('')}</div></div>
    <div class="f full" data-s="monthly quarterly yearly"><span>On</span>
      <div class="row" style="gap:6px"><label class="chk"><input type="radio" name="s_mm" value="day" ${mm === 'day' ? 'checked' : ''}> day</label>
        <select name="s_mday" style="width:auto">${Array.from({length: 31}, (_, i) => i + 1).map(d => `<option value="${d}" ${(s.month_day ?? sd.getDate()) == d ? 'selected' : ''}>${d}</option>`).join('')}<option value="-1" ${s.month_day == -1 ? 'selected' : ''}>Last day</option></select>
        <span class="small muted">(short months use their last day)</span></div>
      <div class="row" style="gap:6px;margin-top:4px"><label class="chk"><input type="radio" name="s_mm" value="nth" ${mm === 'nth' ? 'checked' : ''}> the</label>
        <select name="s_nth" style="width:auto">${[[1, '1st'], [2, '2nd'], [3, '3rd'], [4, '4th'], [5, '5th'], [-1, 'last']].map(([k, l]) => `<option value="${k}" ${(s.nth ?? Math.min(4, Math.floor((sd.getDate() - 1) / 7) + 1)) == k ? 'selected' : ''}>${l}</option>`).join('')}</select>
        <select name="s_nwd" style="width:auto">${WDAY.map((l, i) => `<option value="${i}" ${(s.nth_weekday ?? wd) == i ? 'selected' : ''}>${l}</option>`).join('')}</select></div></div>
    <label class="f"><span>Start date</span><input name="s_start" type="date" value="${esc(st)}"></label>
    <label class="f"><span>Time of day</span><input name="s_time" type="time" value="${esc(s.time_of_day || '09:00')}"></label>
    <label class="f"><span>Ends</span><select name="s_ends"><option value="never" ${ends === 'never' ? 'selected' : ''}>Never</option><option value="date" ${ends === 'date' ? 'selected' : ''}>On date</option><option value="count" ${ends === 'count' ? 'selected' : ''}>After N times</option></select></label>
    <label class="f" id="sEndD"><span>End date</span><input name="s_end" type="date" value="${esc(s.end_date || '')}"></label>
    <label class="f" id="sEndN"><span>Occurrences</span><input name="s_count" type="number" min="1" value="${s.max_count || 12}"></label>
    <label class="f"><span>When due</span><select name="s_post"><option value="post" ${s.post_mode !== 'review' ? 'selected' : ''}>Post automatically</option><option value="review" ${s.post_mode === 'review' ? 'selected' : ''}>Create in To review</option><option value="remind" ${s.post_mode === 'remind' ? 'selected' : ''}>Remind only (no transaction)</option></select></label>
    <label class="f"><span>Remind days before</span><input name="s_lead" type="number" min="0" max="365" value="${s.lead_days ?? ''}" placeholder="default (Settings, 10)"></label>
    <label class="chk full"><input type="checkbox" name="s_var" ${s.variable_amount ? 'checked' : ''}> Variable amount — confirm each time <span class="small muted">(always goes to To review; the amount above is a suggestion)</span></label>
    <div class="full sched-next"><div class="small"><b id="sDesc"></b></div><div class="small muted">Next 5: <span id="sNext">…</span></div><div class="small neg" id="sPast"></div></div>
   </div></fieldset>`;
}
function readSched(form) {
  const v = n => form.elements[n] ? form.elements[n].value : '';
  const ends = v('s_ends');
  return {name: v('s_name'), freq: v('s_freq'), interval: +v('s_interval') || 1,
    weekdays: $$('[name=s_wd]:checked', form).map(c => +c.value), month_mode: ($('[name=s_mm]:checked', form) || {}).value || 'day',
    month_day: +v('s_mday'), nth: +v('s_nth'), nth_weekday: +v('s_nwd'), start_date: v('s_start'), time_of_day: v('s_time') || '09:00',
    end_date: ends === 'date' ? v('s_end') || null : null, max_count: ends === 'count' ? +v('s_count') || null : null,
    post_mode: v('s_post'), lead_days: v('s_lead') === '' ? null : +v('s_lead'), variable_amount: form.elements.s_var.checked};
}
function bindSchedule(bg, form, SCH, collect, after) {
  let h;
  const refresh = () => {
    const r = readSched(form);
    $$('[data-s]', form).forEach(el => el.style.display = el.dataset.s.split(' ').includes(r.freq) ? '' : 'none');
    $('#sUnit', bg).textContent = FREQ_UNIT[r.freq] || '';
    $('#sEndD', bg).style.display = form.s_ends.value === 'date' ? '' : 'none';
    $('#sEndN', bg).style.display = form.s_ends.value === 'count' ? '' : 'none';
    if (r.freq === 'once') { $('#sEndD', bg).style.display = $('#sEndN', bg).style.display = 'none'; form.s_ends.closest('label').style.display = 'none'; }
    else form.s_ends.closest('label').style.display = '';
    clearTimeout(h); h = setTimeout(async () => {
      try {
        const p = await api('/api/schedules/preview', {method: 'POST', body: r});
        $('#sDesc', bg).textContent = p.description;
        $('#sNext', bg).innerHTML = p.next_dates.length ? p.next_dates.map(d => `<span class="pill">${esc(d)}</span>`).join(' ') : 'none (ended)';
        $('#sPast', bg).textContent = p.past_due && !SCH.id ? `${p.past_due} past date(s) since the start will be created on the next run (catch-up window).` : '';
      } catch (e) { $('#sDesc', bg).textContent = ''; $('#sNext', bg).innerHTML = `<span class="neg">${esc(e.message)}</span>`; }
    }, 200);
  };
  form.addEventListener('input', e => { if ((e.target.name || '').startsWith('s_')) refresh(); });
  form.addEventListener('change', e => { if ((e.target.name || '').startsWith('s_')) refresh(); });
  refresh();
  const saveS = async () => {
    const tpl = collect(); delete tpl.date; delete tpl.raw_text;
    for (const k of Object.keys(tpl)) if (tpl[k] === null || tpl[k] === '') delete tpl[k];
    const body = {...readSched(form), template: tpl};
    const r = await guard(() => api(SCH.id ? '/api/schedules/' + SCH.id : '/api/schedules', {method: SCH.id ? 'PUT' : 'POST', body}));
    bg.close(); toast(`Saved: ${r.description}`); after ? after() : route();
  };
  $('[data-ssave]', bg).onclick = saveS;
  form.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveS(); } });
  const del = $('[data-sdel]', bg);
  if (del) del.onclick = () => { bg.close(); deleteSchedule(SCH); };
}
function openSchedule(s = {}, after) {
  const t = s.template || {};
  const preset = {...t, merchant: t.merchant || ''};
  if (!preset.kind) preset.kind = 'expense';
  return openTx(null, after, preset, {schedule: s});
}
async function deleteSchedule(s) {
  const bg = modal('Delete schedule', `<p>Delete <b>${esc(s.name)}</b>?</p>
    <label class="chk"><input type="radio" name="kh" value="1" checked> Keep the ${s.generated_count || 0} transaction(s) it already created</label>
    <label class="chk"><input type="radio" name="kh" value="0"> Also delete those transactions</label>`,
    {buttons: '<button class="btn" data-x2>Cancel</button><button class="btn danger" data-ok>Delete</button>'});
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => { const keep = $('[name=kh]:checked', bg).value === '1'; const r = await guard(() => api(`/api/schedules/${s.id}?keep_history=${keep}`, {method: 'DELETE'})); bg.close(); toast(keep ? 'Schedule deleted; history kept' : `Deleted schedule and ${r.deleted_transactions} transaction(s)`); route(); };
}
function upcomingList(items, {compact = false} = {}) {
  if (!items.length) return '<div class="empty">Nothing scheduled in the next 30 days</div>';
  return `<table class="t upcoming">${items.map(u => `<tr><td class="nowrap small">${esc(u.date.slice(5, 10))} <span class="muted">${esc(u.date.slice(11))}</span></td>
    <td><b>${esc(u.name)}</b><div class="small muted">${esc(kindLabel[u.kind] || u.kind)} · ${esc(u.account_name || '')}${u.to_account_name ? ' → ' + esc(u.to_account_name) : ''}${u.post_mode === 'review' ? ' · <span class="pill">to review</span>' : ''}</div></td>
    <td class="right nowrap ${u.kind === 'income' ? 'pos' : u.kind === 'expense' ? 'neg' : ''}">${u.amount != null ? money(u.amount, u.currency) : '<span class="muted">variable</span>'}${u.variable_amount && u.amount != null ? '<div class="small muted">≈ variable</div>' : ''}</td>
    <td class="right">${schedPostBtn(u.schedule_id, u.date)}</td></tr>`).join('')}</table>`;
}
/* ✓ Post: record one occurrence now (early or late) with the schedule's transaction and mark it done; a recurring
   schedule carries on with its next date. Used by the upcoming lists (Scheduled, Dashboard) and each schedule's Next. */
const schedPostBtn = (sid, date) => `<button class="btn sm" data-upost="${sid}" data-occ="${esc(String(date).slice(0, 10))}" title="Post this one now and mark it done">✓ Post</button>`;
document.addEventListener('click', e => { const b = e.target.closest && e.target.closest('[data-upost]'); if (b) { e.preventDefault(); schedPost(+b.dataset.upost, b.dataset.occ); } });
async function schedPost(sid, occ) {
  const s = await guard(() => api('/api/schedules/' + sid)), tp = s.template;
  const bg = modal('Post now', `<form class="form" id="spF" autocomplete="off">
    <div class="f full small muted">${esc(s.name)} · due ${esc(occ)}${s.freq === 'once' ? '' : ' · the schedule continues with the next date'}${s.loan_id ? '<br>🤝 Split into principal and interest by the Loan Center' : ''}</div>
    <label class="f"><span>Amount (${esc(s.currency || '')})</span><input name="amount" type="number" step="0.01" value="${tp.amount ?? ''}" required></label>
    <label class="f"><span>Date</span><input name="date" type="date" value="${today()}"></label>
    <label class="f"><span>Account</span><select name="account_id">${acctOptions(tp.account_id ? +tp.account_id : null)}</select></label></form>`,
    {buttons: '<button class="btn" data-x2>Cancel</button><button class="btn pri" data-ok>✓ Post</button>'});
  const f = $('#spF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    if (!f.reportValidity()) return;
    await guard(() => api(`/api/schedules/${sid}/post`, {method: 'POST', body: {occurrence_date: occ, amount: f.amount.value, date: f.date.value, account_id: f.account_id.value || null}}));
    bg.close(); toast('Posted · ' + occ + ' marked done'); await loadMeta(); route();
  };
}
VIEWS.scheduled = async (v) => {
  const d = await api('/api/schedules');
  const stt = d.settings;
  v.innerHTML = `${demoBanner()}<div class="grid g2 sched-top">
   <div class="card"><h3>Upcoming · next 30 days</h3>${upcomingList(d.upcoming.slice(0, 15))}${d.upcoming.length > 15 ? `<div class="small muted">+ ${d.upcoming.length - 15} more</div>` : ''}</div>
   <div class="card"><h3>Runner<span class="sp"></span><button class="btn sm" id="sDry">Preview run</button><button class="btn sm pri" id="sRun">▶ Run now</button></h3>
    <div class="small muted">Runs daily at <b>${esc(stt.schedule_run_time)}</b> (${esc(stt.timezone)}) and when the server starts, creating every occurrence due up to today. Missed days are caught up for at most <b>${esc(stt.schedule_catchup_days)}</b> days. Re-running never duplicates.</div>
    <form class="row" id="sSet" style="margin:8px 0"><label class="small">Run at <input name="schedule_run_time" type="time" value="${esc(stt.schedule_run_time)}" style="width:auto"></label><label class="small">Catch up ≤ <input name="schedule_catchup_days" type="number" min="0" value="${esc(stt.schedule_catchup_days)}" style="width:80px"> days</label><button class="btn sm">Save</button></form>
    <h4 style="margin:10px 0 4px">Run log</h4>${d.runs.length ? `<table class="t small"><thead><tr><th>Time</th><th>Trigger</th><th class="right">Created</th><th>Result</th></tr></thead><tbody>${d.runs.slice(0, 6).map(r => `<tr><td class="nowrap">${esc(r.time.slice(5, 16))}</td><td>${esc(r.trigger)}</td><td class="right">${r.generated}</td><td>${esc(r.summary)}${r.errors.length ? `<div class="neg">${r.errors.map(e => esc(e.error)).join('<br>')}</div>` : ''}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">No runs yet</div>'}</div></div>
   <div class="card" style="margin-top:16px"><h3>Scheduled (${d.items.length})<span class="sp"></span><button class="btn sm pri" id="sAdd">+ New schedule</button></h3>
   ${d.items.length ? `<div class="scroll-x"><table class="t resp"><thead><tr><th>Name</th><th>Repeats</th><th>Account</th><th class="right">Amount</th><th>Next</th><th class="right hide-m">Created</th><th></th></tr></thead><tbody>
   ${d.items.map(s => `<tr class="${s.enabled ? '' : 'paused'}"><td><b>${esc(s.name)}</b><div class="small muted">${esc(kindLabel[s.template.kind] || s.template.kind)}${s.post_mode === 'remind' ? ' · <span class="pill">⏰ remind only</span>' : s.post_mode === 'review' || s.variable_amount ? ' · <span class="pill">to review</span>' : ' · auto-post'}${s.enabled ? '' : ' · <span class="pill warn">paused</span>'}${s.finished ? ' · <span class="pill">ended</span>' : ''}</div></td>
     <td class="small">${esc(s.description)}</td><td class="small">${esc(s.account_name || '')}${s.to_account_name ? ' → ' + esc(s.to_account_name) : ''}</td>
     <td class="right nowrap">${s.template.amount != null ? money(s.template.amount, s.currency) : '<span class="muted">variable</span>'}${s.variable_amount ? '<div class="small muted">variable</div>' : ''}</td>
     <td class="small nowrap">${s.next_dates[0] ? esc(s.next_dates[0]) + (s.enabled ? ' ' + schedPostBtn(s.id, s.next_dates[0]) : '') : '<span class="muted">—</span>'}</td><td class="right small hide-m"><a href="#/transactions/?source=scheduled">${s.generated_count}</a>${s.last_generated ? `<div class="muted">last ${esc(s.last_generated)}</div>` : ''}</td>
     <td class="nowrap"><button class="btn sm" data-se="${s.id}">Edit</button> <button class="btn sm" data-sp="${s.id}" data-on="${s.enabled}">${s.enabled ? '⏸ Pause' : '▶ Resume'}</button> <button class="btn sm danger" data-sd="${s.id}">✕</button></td></tr>`).join('')}</tbody></table></div>`
   : '<div class="empty">No schedules yet. Create one here, or open any transaction and press <b>🔁 Make recurring</b>.</div>'}</div>`;
  const byId = Object.fromEntries(d.items.map(s => [s.id, s]));
  $('#sAdd').onclick = () => openSchedule({});
  $$('[data-se]').forEach(b => b.onclick = () => openSchedule(byId[b.dataset.se]));
  $$('[data-sd]').forEach(b => b.onclick = () => deleteSchedule(byId[b.dataset.sd]));
  $$('[data-sp]').forEach(b => b.onclick = async () => { await guard(() => api(`/api/schedules/${b.dataset.sp}/${b.dataset.on === '1' ? 'pause' : 'resume'}`, {method: 'POST'})); toast(b.dataset.on === '1' ? 'Paused' : 'Resumed — dates missed while paused are skipped'); route(); });
  const runIt = async dry => { const r = await guard(() => api('/api/schedules/run', {method: 'POST', body: {dry_run: dry}})); toast(r.summary, !!r.errors.length); if (!dry) { await loadMeta(); route(); } };
  $('#sRun').onclick = () => runIt(false); $('#sDry').onclick = () => runIt(true);
  $('#sSet').onsubmit = async e => { e.preventDefault(); await guard(() => api('/api/settings', {method: 'PUT', body: Object.fromEntries(new FormData(e.target))})); toast('Saved'); route(); };
};
