/* ---------- dashboard ---------- */
VIEWS.dashboard = async (v) => {
  const d = await api(`/api/dashboard?month=${thisMonth()}${S.ledger ? '&ledger_id=' + S.ledger : ''}`);
  const t = d.totals, nw = d.net_worth;
  const empty = !d.accounts.length;
  v.innerHTML = `${demoBanner()}
  ${empty ? `<div class="card" style="margin-bottom:16px"><h3>👋 Getting started</h3><p>Create your first account (bank, credit card, Alipay…) then record transactions with the + button or the quick-add box.</p>
     <div class="row"><a class="btn pri" href="#/accounts">Add an account</a><a class="btn" href="#/settings/import">Import from Suishouji 随手记 / bank CSV</a><button class="btn" onclick="loadDemo()">🧪 Load DEMO data (fake, removable)</button></div></div>` : ''}
  <div class="grid g4">
    <div class="card stat hero"><div class="lbl">Net worth</div><div class="val">${money(nw.net_worth, nw.currency)}</div><div class="small">Assets ${money(nw.assets, nw.currency)} · Liabilities ${money(nw.liabilities, nw.currency)}</div></div>
    <div class="card stat"><div class="lbl">Income this month</div><div class="val pos">${money(t.income, t.currency)}</div></div>
    <div class="card stat"><div class="lbl">Expense this month</div><div class="val neg">${money(t.expense, t.currency)}</div></div>
    <div class="card stat"><div class="lbl">Balance ${d.month}</div><div class="val ${t.net >= 0 ? 'pos' : 'neg'}">${money(t.net, t.currency)}</div>${d.investment_income ? `<div class="small" title="Investment gain/loss + interest + dividends − investment fees this month. Shown separately; not part of income/expense."><a href="#/investments">📈 Investment income <b class="${d.investment_income.total >= 0 ? 'pos' : 'neg'}">${money(d.investment_income.total, d.investment_income.currency)}</b></a></div>` : ''}${d.review_count ? `<div class="small"><a href="#/review">📥 ${d.review_count} to review</a></div>` : ''}</div>
  </div>
  <div class="grid g2" style="margin-top:16px">
    <div class="card"><h3>Spending by category · ${d.month}</h3>${d.by_category.length ? '<div class="chart-box"><canvas id="cPie"></canvas></div>' : '<div class="empty">No expenses this month</div>'}</div>
    <div class="card"><h3>Income vs expense (12 months)</h3><div class="chart-box"><canvas id="cTrend"></canvas></div></div>
    <div class="card"><h3>Net worth over time</h3><div class="chart-box"><canvas id="cNW"></canvas></div><div class="small muted">Past months value investments at cost; the current month uses latest prices.</div></div>
    <div class="card"><h3>Account balances<span class="sp"></span><a class="small" href="#/accounts">All →</a></h3>${accountMini(d.accounts)}</div>
    <div class="card"><h3>Upcoming (30 days)<span class="sp"></span><a class="small" href="#/scheduled">Scheduled →</a></h3>${upcomingList(d.upcoming || [])}</div>
    <div class="card"><h3>Budgets<span class="sp"></span><a class="small" href="#/budgets">Manage →</a></h3>${d.budgets.length ? d.budgets.map(budgetRow).join('') : '<div class="empty">No budgets for this month</div>'}</div>
    <div class="card"><h3>Recent activity<span class="sp"></span><a class="small" href="#/transactions">All →</a></h3>${txTable(d.recent, {compact: true})}</div>
  </div>`;
  bindTxRows(v);
  api(`/api/reports/tax-tips?start=${d.month}-01&end=${d.month}-31${S.ledger ? '&ledger_id=' + S.ledger : ''}`).then(tt => {
    const x = tt.totals; if (x.tax_count || x.tip_count) $('.grid.g4 .card.stat:nth-child(3)', v)?.insertAdjacentHTML('beforeend', taxTipLine(x, tt.currency));
  }).catch(() => {});
  if (d.by_category.length) chart('cPie', {type: 'doughnut', data: {labels: d.by_category.map(x => x.name), datasets: [{data: d.by_category.map(x => x.amount), backgroundColor: PALETTE}]}, options: {plugins: {legend: {position: innerWidth < 760 ? 'bottom' : 'right'}}}});
  chart('cTrend', {type: 'bar', data: {labels: d.trend.map(x => x.month.slice(2)), datasets: [{label: 'Income', data: d.trend.map(x => x.income), backgroundColor: '#14a06f'}, {label: 'Expense', data: d.trend.map(x => x.expense), backgroundColor: '#e8553e'}, {type: 'line', label: 'Investment income', data: d.trend.map(x => x.investment_income || 0), borderColor: '#8a5cd6', backgroundColor: '#8a5cd6', pointRadius: 2, tension: .25}]}});
  chart('cNW', {type: 'line', data: {labels: d.networth_history.map(x => x.month.slice(2)), datasets: [{label: 'Net worth', data: d.networth_history.map(x => x.net_worth), borderColor: '#3a7bd5', backgroundColor: 'rgba(58,123,213,.12)', fill: true, tension: .25}, {label: 'Liabilities', data: d.networth_history.map(x => x.liabilities), borderColor: '#e0483a', borderDash: [4, 4], tension: .25}]}});
};
function accountMini(accts) {
  if (!accts.length) return '<div class="empty">No accounts yet</div>';
  return accts.slice(0, 12).map(a => `<div class="acct"><div class="nm"><b>${esc(a.name)}</b><span class="small muted">${esc(a.group_name || '')}</span></div>
    <div class="bal ${a.balance < 0 ? 'neg' : ''}">${money(a.balance, a.currency)}${a.currency !== S.meta.settings.base_currency ? `<div class="small muted">${money(a.balance_base)}</div>` : ''}</div></div>`).join('');
}
function budgetRow(b) {
  const cls = b.pct > 100 ? 'over' : b.pct > 80 ? 'warn' : '';
  return `<div style="margin-bottom:10px"><div class="row"><b>${esc(b.name)}</b><span class="sp"></span><span class="small">${money(b.spent)} / ${money(b.amount)}</span></div>
   <div class="bar"><i class="${cls}" style="width:${Math.min(100, b.pct)}%"></i></div><div class="small muted">${b.remaining >= 0 ? 'Left ' + money(b.remaining) : 'Over by ' + money(-b.remaining)} · ${b.pct}%</div></div>`;
}
async function loadDemo() { await guard(() => api('/api/demo/load', {method: 'POST'})); toast('Demo data loaded (all marked [DEMO])'); refresh(); }

/* ---------- transaction table ---------- */
function txAmount(t) {
  const sign = ['expense', 'buy', 'invest_fee'].includes(t.kind) ? '-' : ['income', 'refund', 'sell', 'dividend', 'reimbursement'].includes(t.kind) ? '+' : t.kind === 'adjustment' && t.amount > 0 ? '+' : '';
  const cls = sign === '-' ? 'neg' : sign === '+' ? 'pos' : '';
  const amt = t.kind === 'adjustment' ? t.amount : Math.abs(t.amount);
  return `<span class="${cls}">${sign}${money(Math.abs(amt), t.currency)}</span>${t.orig_currency ? `<div class="small muted">${num(t.orig_amount)} ${t.orig_currency}</div>` : ''}`;
}
function txDesc(t) {
  let main = t.kind === 'transfer' ? `${esc(t.account_name || '?')} → ${esc(t.to_account_name || '?')}` :
    ['buy', 'sell', 'dividend'].includes(t.kind) ? `${esc(t.ticker || '')} ${t.shares ? num(t.shares, 6) + ' @ ' + num(t.price, 4) : ''}` :
    t.kind === 'reimbursement' ? `Reimbursement${t.reimb_payer ? ' · ' + esc(t.reimb_payer) : ''}` :
    t.kind === 'adjustment' ? (t.adj_reason === 'opening' ? 'Opening balance' : `${t.source === 'balance_set' ? '⚖️ Balance set' : 'Balance adj.'} · <span class="reason r-${esc(t.adj_reason || 'correction')}">${esc(t.adj_reason_label || 'Correction/other')}</span>`) :
    esc(t.category_label || 'Uncategorized');
  const bits = [t.merchant_name, t.member_name, t.project_name].filter(Boolean).map(esc);
  const tags = (t.tags || '').split(',').filter(Boolean).map(x => `<span class="pill">#${esc(x)}</span>`).join(' ');
  const demo = isDemoName(t.account_name) || /^\[DEMO\]/.test(t.note || '') ? '<span class="pill demo">DEMO</span> ' : '';
  const rb = t.kind === 'expense' && t.reimbursable ? txReimbPill(t) : t.kind === 'reimbursement' ? txReimbPill(t) : '';
  const rs = t.reserves && t.reserves.length && typeof txReservePill === 'function' ? txReservePill(t) : '';
  const ln = t.loans && t.loans.length && typeof txLoanPill === 'function' ? txLoanPill(t) : '';
  return `<div>${demo}${rb}${rs}${ln}<b>${main}</b> ${tags}</div><div class="small muted">${t.kind !== 'transfer' ? esc(t.account_name || '— no account —') + ' · ' : ''}${bits.join(' · ')}${t.note ? ' · ' + esc(t.note.slice(0, 80)) : ''}${t.attachment ? ' · 📎' : ''}</div>`;
}
/* reimbursement status badge in transaction lists: expense open / partial / closed, reimbursement unallocated money */
function txReimbPill(x) {
  if (x.kind === 'reimbursement') {
    const un = x.reimb_unallocated;
    if (un === undefined) return '';
    return un > 0.005 ? `<span class="pill reimb rb-un" title="Part of this reimbursement is not linked to an expense yet">${t('Unallocated')} ${money(un, x.currency)}</span> ` : (x.reimb_link_count ? `<span class="pill reimb" title="Fully linked to expenses">🔗 ${t('Linked')}</span> ` : '');
  }
  const stt = x.reimb_state || 'open';
  const lbl = {open: 'Reimbursable', partial: 'Partly reimbursed', closed: x.reimb_closed_by === 'written_off' ? 'Written off' : 'Reimbursed'}[stt];
  return `<span class="pill reimb rb-${stt}" title="${stt === 'closed' ? 'Closed: only the part not reimbursed counts as an expense' : 'Reimbursable — not counted as an expense while open'}">${lbl}${x.reimb_payer ? ' · ' + esc(x.reimb_payer) : ''}</span> `;
}
function txTable(rows, {compact = false, select = false, tbodyId = ''} = {}) {
  if (!rows.length && !tbodyId) return '<div class="empty">No transactions</div>';
  return `<div class="scroll-x"><table class="t resp"><thead><tr>${select ? '<th><input type="checkbox" id="selAll"></th>' : ''}<th>Date</th><th>Type</th><th>Details</th>${compact ? '' : '<th>Source</th>'}<th class="right">Amount</th></tr></thead><tbody${tbodyId ? ` id="${tbodyId}"` : ''}>
  ${txRowsHtml(rows, {compact, select})}
  </tbody></table></div>`;
}
function txRowsHtml(rows, {compact = false, select = false} = {}) {
  return `${rows.map(t => `<tr data-tx="${t.id}" style="cursor:pointer">${select ? `<td class="hide-m" onclick="event.stopPropagation()"><input type="checkbox" class="sel" value="${t.id}"></td>` : ''}
   <td class="nowrap small hide-m">${esc(t.date.slice(0, compact ? 10 : 16))}</td><td class="hide-m"><span class="pill ${t.kind}">${kindLabel[t.kind] || t.kind}</span>${t.status === 'review' ? ' <span class="pill review">review</span>' : ''}</td>
   <td>${t.reconciled_id ? '<span class="rec-badge" title="Reconciled (on or before a reconciliation checkpoint)">✔🔒</span> ' : ''}${txDesc(t)}<div class="small muted" style="display:none" data-m>${esc(t.date.slice(0, 16))}</div></td>${compact ? '' : `<td class="small muted hide-m">${esc(srcLabel[t.source] || t.source)}</td>`}<td class="tx-amt">${txAmount(t)}</td></tr>`).join('')}`;
}
function bindTxRows(root, after) { $$('tr[data-tx]', root).forEach(tr => tr.onclick = () => openTx(+tr.dataset.tx, after)); }

/* ---------- transactions page ---------- */
S.txf = {limit: 100};
VIEWS.transactions = async (v, rest) => {
  if (rest && rest[0]) { const p = new URLSearchParams(rest[0].replace(/^\?/, '')); S.txf = {limit: 100}; p.forEach((val, k) => S.txf[k] = val); }
  const f = S.txf; const m = S.meta;
  v.innerHTML = `${demoBanner()}<div class="card"><form id="flt" class="grid g4" style="gap:8px">
    <input type="search" name="q" placeholder="Search note / merchant / tag" value="${esc(f.q || '')}">
    <input type="date" name="start" value="${esc(f.start || '')}" title="From"><input type="date" name="end" value="${esc(f.end || '')}" title="To">
    <select name="kind"><option value="">All types</option>${Object.entries(kindLabel).map(([k, l]) => `<option value="${k}" ${f.kind === k ? 'selected' : ''}>${l}</option>`).join('')}</select>
    <select name="account_id">${acctOptions(f.account_id, {blank: 'All accounts'})}</select>
    <select name="category_id">${catOptions('', f.category_id).replace('— Category —', 'All categories')}</select>
    <select name="member_id">${listOptions(m.members, f.member_id, 'All members')}</select>
    <select name="merchant_id">${listOptions(m.merchants, f.merchant_id, 'All merchants')}</select>
    <select name="project_id">${listOptions(m.projects, f.project_id, 'All projects')}</select>
    <input name="tag" placeholder="Tag" value="${esc(f.tag || '')}">
    <select name="source"><option value="">Any source</option>${['web', 'api', 'email', 'bot', 'message', 'import', 'balance_set', 'opening_balance', 'scheduled', 'reconcile'].map(s => `<option value="${s}" ${f.source === s ? 'selected' : ''}>${srcLabel[s] || s}</option>`).join('')}</select>
    <select name="has_taxtip"><option value="">Tax/tip: any</option><option value="tax" ${f.has_tax === '1' ? 'selected' : ''}>With tax</option><option value="tip" ${f.has_tip === '1' ? 'selected' : ''}>With tip</option></select>
    <select name="reconciled"><option value="">Reconciled or not</option><option value="1" ${f.reconciled === '1' ? 'selected' : ''}>✔ Reconciled</option><option value="0" ${f.reconciled === '0' ? 'selected' : ''}>Not reconciled</option></select>
    <div class="row"><button class="btn pri">Filter</button><button type="button" class="btn" id="fReset">Reset</button></div></form></div>
  <div class="card" style="margin-top:14px"><h3><span id="txCount"></span><span class="sp"></span>
    <button class="btn sm" id="bulkPrj" disabled>📁 Assign project</button> <button class="btn sm danger" id="bulkDel" disabled>Delete selected</button>
    <a class="btn sm" id="expCsv">⬇ Export CSV</a><a class="btn sm" href="#/settings/import">⬆ Import</a><button class="btn sm pri" onclick="openTx()">+ New</button></h3><div id="txList"></div>
    </div>`;
  const load = async () => {
    const base = Object.entries({...f, ledger_id: S.ledger || ''}).filter(([k, x]) => x !== '' && x != null && !['limit', 'offset', 'before'].includes(k));
    const qs = new URLSearchParams(base);
    $('#expCsv').href = '/api/transactions/export.csv?' + qs.toString();
    S.txRows = [];
    const old = $('#txList').nextElementSibling; if (old && old.classList.contains('inf-foot')) old.remove();
    (S.observers || []).forEach(o => o.disconnect()); S.observers = [];
    $('#txList').innerHTML = txTable([], {select: true, tbodyId: 'txBody'});
    $('#txCount').textContent = 'Loading…';
    const upd = () => { const n = !$$('.sel:checked').length; $('#bulkDel').disabled = n; $('#bulkPrj').disabled = n; };
    infiniteList($('#txList'), {
      load: cursor => api('/api/transactions?' + new URLSearchParams([...base, ['limit', '50'], ...(cursor ? [['before', cursor]] : [])])),
      append: (r, first) => {
        if (first) {
          const sm = r.summary || {};
          $('#txCount').innerHTML = `${r.total} transactions${r.total ? ` <span class="small muted">· income <b class="pos">${money(sm.income, sm.currency)}</b> · expense <b class="neg">${money(sm.expense, sm.currency)}</b></span>` : ''}`;
          if (!r.items.length) { $('#txList').innerHTML = '<div class="empty">No transactions match</div>'; return; }
        }
        S.txRows = S.txRows.concat(r.items);
        $('#txBody').insertAdjacentHTML('beforeend', txRowsHtml(r.items, {select: true}));
        bindTxRows($('#txList'), () => load());
        $$('.sel').forEach(c => c.onchange = upd);
        const sa = $('#selAll'); if (sa) sa.onchange = () => { $$('.sel').forEach(c => c.checked = sa.checked); upd(); };
      },
    });
  };
  $('#flt').onsubmit = e => { e.preventDefault(); const fd = new FormData(e.target); S.txf = {limit: 100}; fd.forEach((val, k) => { if (val) S.txf[k] = val; }); if (S.txf.has_taxtip) { S.txf['has_' + S.txf.has_taxtip] = '1'; delete S.txf.has_taxtip; } Object.assign(f, S.txf); Object.keys(f).forEach(k => { if (!(k in S.txf)) delete f[k]; }); load(); };
  $('#fReset').onclick = () => { S.txf = {limit: 100}; location.hash = '#/transactions'; route(); };
  $('#bulkPrj').onclick = () => assignProjectDialog($$('.sel:checked').map(c => +c.value), () => load());
  $('#bulkDel').onclick = async () => { const ids = $$('.sel:checked').map(c => +c.value); if (!await confirmBox(`Delete ${ids.length} transactions?`)) return; await guard(() => api('/api/transactions/bulk-delete', {method: 'POST', body: {ids}})); toast('Deleted'); load(); loadMeta(); };
  await load();
};

/* ---------- transaction editor ---------- */
// openTx(id, after, preset, {uncertain: [...], banner: '...'}) — id=null opens a new form (optionally pre-filled)
const KEEP_FIELDS = ['account_id', 'from_account_id', 'to_account_id', 'category_id', 'currency', 'member_id'];
async function openTx(id, after, preset = {}, opts = {}) {
  await (S.meta || loadMeta());
  const m = S.meta;
  const isNew = !id;
  const prefs = txPrefs();
  const lastFor = k => prefs.tx_last[k] || {};
  let t;
  if (id) t = await api('/api/transactions/' + id);
  else {
    const kind0 = preset.kind || 'expense', last = lastFor(kind0);
    const known = x => x && (m.accounts || []).some(a => a.id == x && !a.archived);
    t = Object.assign({kind: kind0, date: localDT(), status: 'posted', ledger_id: S.ledger || m.settings.default_ledger_id || 1}, preset);
    if (!t.account_id) t.account_id = known(last.account_id) ? last.account_id : (m.settings.default_account_id || '');
    if (kind0 === 'transfer' && !t.to_account_id && known(last.to_account_id)) t.to_account_id = last.to_account_id;
    if (!t.category_id && last.category_id && kind0 !== 'transfer') t.category_id = last.category_id;
    if (!t.member_id && last.member_id) t.member_id = last.member_id;
    if (!t.currency) t.currency = last.currency || (acctById(t.account_id) || {}).currency || m.settings.base_currency;
  }
  const isReview = t.status === 'review';
  const SCH = opts.schedule || null;  // schedule mode: the same form edits a schedule's transaction template
  // fields the parser was unsure about (quick-add pre-fill) or that a review item is missing
  const unsure = new Set(opts.uncertain || []);
  if (isReview) { if (!t.amount) unsure.add('amount'); if (!t.account_id) unsure.add('account_id'); if (['expense', 'income', 'refund'].includes(t.kind) && !t.category_id) unsure.add('category_id'); if (t.kind === 'transfer' && !t.to_account_id) unsure.add('to_account_id'); }
  if (unsure.has('account_id')) unsure.add('from_account_id');
  const U = name => unsure.has(name) ? ' unsure' : '';
  const html = `<div class="kind-tabs${U('kind')}">${['expense', 'income', 'transfer', 'refund', 'reimbursement', 'adjustment', 'buy', 'sell', 'dividend', 'invest_fee'].map(k => `<button type="button" data-k="${k}" class="${t.kind === k ? 'on' : ''}">${kindLabel[k]}</button>`).join('')}${(isNew || isReview) && !SCH ? `<button type="button" data-rsv class="rs-tab" title="${isReview ? 'Convert this item into a card hold or deposit' : 'Hotel / rental deposit or card hold'}">🔒 Reserve</button>` : ''}</div>
  ${isReview ? `<div class="demo-banner" style="background:#fff8e6;border-color:#e4a11b;color:#7a5200">📥 Review item from <b>${esc(t.source)}</b> ${confPill(t.confidence)} — check the fields${unsure.size ? ' (highlighted ones especially)' : ''} and press <b>Approve</b>.${t.raw_text ? `<div class="small mono" style="white-space:pre-wrap;max-height:120px;overflow:auto;margin-top:4px">${esc(t.raw_text.slice(0, 1500))}</div>` : ''}</div>` : ''}
  ${opts.banner ? `<div class="demo-banner prefill-banner"><div>✎ ${opts.banner}${unsure.size ? ' <b>Highlighted fields</b> are guesses — please check them.' : ''}</div></div>` : ''}
  <form id="txForm" class="form" autocomplete="off">
    ${SCH ? '' : `<div class="f full rc-row"><span>Receipt</span><div class="rc-strip"><span id="rcThumbs">${rcThumbsHtml(opts.receipts)}</span><button type="button" class="btn sm" id="rcBtn" title="Take or choose a receipt photo; ${id ? 'it is attached to this transaction' : 'the form is filled from it'}">📷 ${id ? 'Add photo' : 'Fill from photo'}</button></div></div>`}
    <label class="f${U('amount')}"><span>Amount</span><input name="amount" type="number" step="0.01" inputmode="decimal" enterkeyhint="done" value="${(t.orig_currency ? t.orig_amount : t.amount) ?? ''}" ${['buy', 'sell'].includes(t.kind) ? 'readonly' : ''}></label>
    <label class="f${U('currency')}"><span>Currency <small class="muted" id="curHint"></small></span><select name="currency">${curOptions(t.orig_currency || t.currency || (acctById(t.account_id) || {}).currency || m.settings.base_currency)}</select></label>
    <label class="f${U('account_id')}" data-show="!transfer"><span>Account</span><select name="account_id">${acctOptions(t.account_id)}</select></label>
    <div class="full transfer-box" data-show="transfer">
      <label class="f${U('from_account_id')}"><span>From</span><select name="from_account_id">${acctOptions(t.account_id)}</select></label>
      <div class="arrow" aria-hidden="true">→</div>
      <label class="f${U('to_account_id')}"><span>To</span><select name="to_account_id">${acctOptions(t.to_account_id)}</select></label>
      <label class="f full" id="toAmtWrap"><span id="toAmtLbl">Amount received</span><input name="to_amount" type="number" step="0.01" inputmode="decimal" value="${t.to_amount ?? ''}" placeholder="optional, converted with your rates if empty"></label>
    </div>
    <label class="f${U('category_id')}" data-show="expense income refund dividend"><span>Category</span><select name="category_id"></select></label>
    <label class="f" data-show="adjustment"><span>Reason for difference</span><select name="adj_reason">${reasonOptions(t.adj_reason || (isNew ? defaultReason(acctById(t.account_id)) : 'correction'), {opening: true})}</select><span class="small muted" id="reasonWhy"></span></label>
    <details class="full taxtip-box" data-show="expense income refund" ${t.tax_amount != null || t.tip_amount != null ? 'open' : ''}><summary class="small">Tax &amp; tip <span class="muted">(optional, part of the amount)</span></summary>
      <div class="row" style="gap:8px;flex-wrap:wrap;margin-top:6px">
        <label class="f"><span>Tax</span><input name="tax_amount" type="number" step="0.01" min="0" inputmode="decimal" value="${ttConv(t, t.tax_amount)}" placeholder="—"></label>
        <label class="f"><span>Tax rate %</span><input name="tax_rate" type="number" step="0.001" min="0" max="100" inputmode="decimal" value="${t.tax_rate ?? ''}" placeholder="—"></label>
        <label class="f"><span>Tip</span><input name="tip_amount" type="number" step="0.01" min="0" inputmode="decimal" value="${ttConv(t, t.tip_amount)}" placeholder="—"></label>
        <label class="f"><span>Tip %</span><input name="tip_pct" type="number" step="0.1" min="0" inputmode="decimal" placeholder="—"></label>
      </div><div class="small muted" id="ttSub"></div></details>
    <div class="full reimb-box" data-show="expense"><label class="chk"><input type="checkbox" name="reimbursable" ${t.reimbursable ? 'checked' : ''}> <b>Reimbursable</b> <span class="small muted">— not counted as an expense while open; link the reimbursements that pay it back after saving</span></label>
      <div class="reimb-fields form" ${t.reimbursable ? '' : 'hidden'}>
        <label class="f"><span>Reimburse from (company / person)</span><input name="reimb_payer" list="dlPayer" value="${esc(t.reimb_payer || '')}"></label>
        <label class="f"><span>Expected amount <small class="muted">(default = amount)</small></span><input name="reimb_expected" type="number" step="0.01" inputmode="decimal" value="${t.reimb_expected ?? ''}"></label>
        <label class="f full"><span>Reimbursement note</span><input name="reimb_note" value="${esc(t.reimb_note || '')}"></label></div></div>
    <label class="f" data-show="reimbursement"><span>Paid back by (company / person)</span><input name="rb_from" list="dlPayer" value="${esc(t.kind === 'reimbursement' ? t.reimb_payer || '' : '')}"></label>
    ${id && !isReview ? '<div class="full rb-linkbox" id="rsTxBox" hidden></div><div class="full rb-linkbox" id="lnTxBox" hidden></div>' : ''}
    ${id ? '<div class="full rb-linkbox" id="rbLinkBox" hidden></div>' : '<div class="full small muted" data-show="reimbursement">💡 A reimbursement is not income. After saving, link it to the reimbursable expenses it pays back (you can also do that later here or in Reimbursements).</div>'}
    <label class="f" id="prWrap" style="display:none"><span>Investment: counts as</span><select name="pr_class">${[['', 'Auto'], ['principal', 'Principal'], ['returns', 'Returns'], ['unexplained', 'Unexplained']].map(([v, l]) => `<option value="${v}" ${((t.pr_class === 'other' ? 'unexplained' : t.pr_class) || '') === v ? 'selected' : ''}>${l}</option>`).join('')}</select><span class="small muted">Only for investment accounts, e.g. mark a deposit as returns.</span></label>
    <label class="f" data-show="buy sell dividend invest_fee"><span>Ticker</span><input name="ticker" value="${esc(t.ticker || '')}" placeholder="AAPL, VTI, 600519.SS, BTC-USD"></label>
    <label class="f" data-show="buy sell"><span>Shares</span><input name="shares" type="number" step="any" inputmode="decimal" value="${t.shares ?? ''}"></label>
    <label class="f" data-show="buy sell"><span>Price</span><input name="price" type="number" step="any" inputmode="decimal" value="${t.price ?? ''}"></label>
    <label class="f" data-show="buy sell"><span>Fee</span><input name="fee" type="number" step="any" inputmode="decimal" value="${t.fee ?? ''}"></label>
    <label class="f${U('date')}" ${SCH ? 'style="display:none" data-show="none"' : ''}><span>Date & time</span><input name="date" type="datetime-local" value="${esc((t.date || localDT()).replace(' ', 'T').slice(0, 16))}"></label>
    <label class="f${U('merchant')}" data-show="expense income refund"><span>Merchant / payee</span><input name="merchant" list="dlMerch" value="${esc(t.merchant_name || t.merchant || '')}"></label>
    <label class="f${U('member_id')}" data-show="expense income refund transfer"><span>Member</span><select name="member_id">${listOptions(m.members, t.member_id, '—')}</select></label>
    <label class="f" data-show="expense income refund transfer"><span>Project</span><select name="project_id">${listOptions(m.projects.filter(p => !p.archived || p.id == t.project_id), t.project_id, '—')}</select></label>
    <label class="f" data-show="expense income refund transfer" id="txPlanLine" hidden><span>Plan line</span><select name="plan_line_id"></select></label>
    <label class="f"><span>Ledger</span><select name="ledger_id">${m.ledgers.map(l => `<option value="${l.id}" ${l.id == t.ledger_id ? 'selected' : ''}>${esc(l.name)}</option>`).join('')}</select></label>
    <label class="f"><span>Tags (comma separated)</span><input name="tags" value="${esc(Array.isArray(t.tags) ? t.tags.join(',') : (t.tags || ''))}"></label>
    <label class="f full"><span>Note</span><textarea name="note" style="min-height:54px">${esc(t.note || '')}</textarea></label>
    <label class="f full" ${SCH ? 'style="display:none" data-show="none"' : ''}><span>Attachment (receipt/photo)</span>${t.attachment ? `<span><a href="/api/transactions/${t.id}/attachment" target="_blank">📎 ${esc(t.attachment.split('_').slice(2).join('_'))}</a> <button type="button" class="btn sm danger" id="rmAtt">remove</button></span>` : ''}<input type="file" name="file" accept="image/*,application/pdf"></label>
    <div class="full rec-warn" id="recWarn" style="display:none"></div>
    <datalist id="dlMerch">${m.merchants.map(x => `<option value="${esc(x.name)}">`).join('')}</datalist>
    ${id ? `<div class="full small muted">#${t.id} · source ${esc(t.source)} · created ${esc(t.created_at)}${t.external_id ? ' · ext ' + esc(t.external_id.slice(0, 40)) : ''}</div>` : ''}
    ${isNew && !SCH ? '<div class="full small muted kbd-hint hide-m">Enter in Amount = Save · Ctrl/⌘+Enter = Save &amp; add another</div>' : ''}
    ${SCH ? schedFields(SCH) : ''}
  </form>`;
  const buttons = `${id ? '<button class="btn danger" data-del>Delete</button><span class="sp"></span>' : ''}<button class="btn" data-cancel>Cancel</button>${isReview ? '<button class="btn" data-save>Save (keep in review)</button><button class="btn pri" data-approve>Approve ✓</button>' : isNew ? '<button class="btn" data-again>Save &amp; add another</button><button class="btn pri" data-save>Save</button>' : '<button class="btn pri" data-save>Save</button>'}`;
  const sButtons = SCH ? `${SCH.id ? '<button class="btn danger" data-sdel>Delete schedule</button><span class="sp"></span>' : ''}<button class="btn" data-cancel>Cancel</button><button class="btn pri" data-ssave>Save schedule</button>` : null;
  const tButtons = id && !isReview ? buttons.replace('<button class="btn" data-cancel>', '<button class="btn" data-recur title="Create a schedule from this transaction">🔁 Make recurring</button><button class="btn" data-cancel>') : buttons;
  const bg = modal(SCH ? (SCH.id ? 'Edit schedule' : 'New schedule') : id ? (isReview ? 'Review item' : 'Edit transaction') : 'New transaction', html, {buttons: sButtons || tButtons, wide: false});
  const form = $('#txForm', bg);
  const ttSync = (from) => {  // tip % → tip amount (of the subtotal); show subtotal = amount − tax − tip
    const A = +form.amount.value || 0, tax = form.tax_amount.value === '' ? null : +form.tax_amount.value;
    if (from === 'pct' && form.tip_pct.value !== '' && A) { const p = +form.tip_pct.value / 100; form.tip_amount.value = (A - (tax || 0) - (A - (tax || 0)) / (1 + p)).toFixed(2); }
    if (from === 'rate' && form.tax_rate.value !== '' && A && form.tax_amount.value === '') { const r = +form.tax_rate.value / 100, tip = +form.tip_amount.value || 0; form.tax_amount.value = ((A - tip) - (A - tip) / (1 + r)).toFixed(2); }
    const tip = form.tip_amount.value === '' ? null : +form.tip_amount.value, tx = form.tax_amount.value === '' ? null : +form.tax_amount.value;
    $('#ttSub', bg).textContent = (tx != null || tip != null) && A ? `${i18nText('Subtotal')} ${money(A - (tx || 0) - (tip || 0), form.currency ? form.currency.value : undefined)}` : '';
  };
  if (form.tax_amount) { form.tip_pct.oninput = () => ttSync('pct'); form.tax_rate.oninput = () => ttSync('rate'); ['amount', 'tax_amount', 'tip_amount'].forEach(k => form[k].addEventListener('input', () => ttSync())); ttSync(); }
  // plan line 预算明细: options follow the chosen project (blank = matched by category)
  const syncPlanLines = (keep) => {
    const pid = +form.project_id.value || null, ls = (S.meta.plan_lines || []).filter(l => l.project_id === pid);
    const want = keep !== undefined ? keep : form.plan_line_id.value;
    form.plan_line_id.innerHTML = '<option value="">— by category —</option>' + ls.map(l => `<option value="${l.id}" ${l.id == want ? 'selected' : ''}>${esc(l.name)}</option>`).join('');
    $('#txPlanLine', bg).hidden = !ls.length;
  };
  syncPlanLines(t.plan_line_id || '');
  form.project_id.addEventListener('change', () => syncPlanLines(''));
  let kind = t.kind;
  const dirty = new Set(Object.keys(preset).filter(k => preset[k] !== null && preset[k] !== undefined && preset[k] !== ''));
  if (dirty.has('account_id')) dirty.add('from_account_id');
  const syncTransfer = () => {
    const a = acctById(form.from_account_id.value), b = acctById(form.to_account_id.value);
    const differ = a && b && a.currency !== b.currency;
    $('#toAmtWrap', bg).style.display = differ ? '' : 'none';
    if (differ) $('#toAmtLbl', bg).textContent = `Amount received (${curLabel(b.currency)})`;
  };
  const curHint = () => { const a = acctById(kind === 'transfer' ? form.from_account_id.value : form.account_id.value); $('#curHint', bg).textContent = a && form.currency.value !== a.currency ? `(converted into ${a.currency})` : ''; };
  const sync = () => {
    $$('.kind-tabs button', bg).forEach(b => b.classList.toggle('on', b.dataset.k === kind));
    $$('[data-show]', form).forEach(el => {
      const s = el.dataset.show; const show = s.startsWith('!') ? !s.slice(1).split(' ').includes(kind) : s.split(' ').includes(kind);
      el.style.display = show ? '' : 'none';
    });
    const ck = kind === 'income' || kind === 'dividend' ? 'income' : 'expense';
    const cs = form.category_id; const cur = cs.value || t.category_id; cs.innerHTML = catOptions(ck, cur);
    form.amount.readOnly = ['buy', 'sell'].includes(kind);
    if (kind === 'adjustment') form.amount.title = 'Balance adjustment (not income/expense): positive increases the balance, negative decreases it. To set an account to an exact balance use Accounts → Set balance.';
    syncTransfer(); curHint();
    const rw = $('#reasonWhy', bg); if (rw) rw.textContent = REASON_HELP[form.adj_reason.value] || '';
    const invA = id => { const a = acctById(id); return !!(a && isInvType(a.type)); };
    $('#prWrap', bg).style.display = !['buy', 'sell'].includes(kind) && (kind === 'transfer' ? invA(form.from_account_id.value) || invA(form.to_account_id.value) : invA(form.account_id.value)) ? '' : 'none';
    recWarn();
  };
  const recWarn = () => {
    if (SCH) return;
    const w = reconWarning(form.date.value, ...(kind === 'transfer' ? [form.from_account_id.value, form.to_account_id.value] : [form.account_id.value]));
    const old = id ? reconWarning(t.date, t.account_id, t.kind === 'transfer' ? t.to_account_id : null) : '';
    const el = $('#recWarn', bg); const msg = w || old; el.innerHTML = msg; el.style.display = msg ? '' : 'none';
  };
  ['date', 'account_id', 'from_account_id', 'to_account_id'].forEach(n => form[n].addEventListener('change', () => sync()));
  form.adj_reason.addEventListener('change', () => { dirty.add('adj_reason'); sync(); });
  form.account_id.addEventListener('change', () => { if (isNew && !dirty.has('adj_reason')) { form.adj_reason.value = defaultReason(acctById(form.account_id.value)); sync(); } });
  // new forms: switching type applies that type's remembered defaults to fields the user hasn't touched
  const applyLast = () => {
    if (!isNew) return;
    const last = lastFor(kind);
    const setIf = (name, v) => { if (!dirty.has(name) && v && form[name] && [...form[name].options].some(o => o.value == v)) form[name].value = v; };
    setIf('account_id', last.account_id); setIf('from_account_id', last.account_id); setIf('to_account_id', last.to_account_id);
    if (!dirty.has('category_id') && last.category_id) t.category_id = last.category_id;
    setIf('member_id', last.member_id);
    if (!dirty.has('currency')) { const a = acctById(kind === 'transfer' ? form.from_account_id.value : form.account_id.value); const c = last.currency || (a && a.currency); if (c) form.currency.value = c; }
  };
  $$('.kind-tabs button', bg).forEach(b => b.onclick = () => { kind = b.dataset.k; $('.kind-tabs', bg).classList.remove('unsure'); form.category_id.value = ''; applyLast(); sync(); });
  const recalc = () => { if (['buy', 'sell'].includes(kind)) { const g = (+form.shares.value || 0) * (+form.price.value || 0), fee = +form.fee.value || 0; form.amount.value = (kind === 'buy' ? g + fee : g - fee).toFixed(2); } };
  ['shares', 'price', 'fee'].forEach(n => form[n].oninput = recalc);
  // touching a field marks it as chosen (and clears its "unsure" highlight)
  form.addEventListener('change', e => { const n = e.target.name; if (n) { dirty.add(n); const l = e.target.closest('label.f'); if (l) l.classList.remove('unsure'); } });
  form.addEventListener('input', e => { const l = e.target.closest('label.f'); if (l) l.classList.remove('unsure'); });
  const acctCur = sel => () => { const a = acctById(sel.value); if (a) { form.currency.value = a.currency; dirty.add('currency'); } syncTransfer(); curHint(); };
  form.account_id.addEventListener('change', acctCur(form.account_id));
  form.from_account_id.addEventListener('change', acctCur(form.from_account_id));
  form.to_account_id.addEventListener('change', syncTransfer);
  form.currency.addEventListener('change', curHint);
  form.reimbursable.addEventListener('change', () => { $('.reimb-fields', bg).hidden = !form.reimbursable.checked; });
  if (!document.getElementById('dlPayer')) document.body.insertAdjacentHTML('beforeend', '<datalist id="dlPayer"></datalist>');
  api('/api/reimbursements?status=all').then(d => { $('#dlPayer').innerHTML = d.payers.map(p => `<option value="${esc(p)}">`).join(''); }).catch(() => {});
  sync();
  const rsBtn = $('[data-rsv]', bg);
  if (rsBtn) rsBtn.onclick = () => {
    const a = +(form.account_id.value || form.from_account_id.value) || null;
    const preset = {account_id: a, amount: form.amount.value || '', currency: form.currency.value, merchant: form.merchant.value, note: form.note.value,
      start_date: (form.date.value || '').slice(0, 10) || today(), category_id: kind === 'expense' ? (+form.category_id.value || null) : null,
      project_id: +form.project_id.value || null};
    bg.close(); reserveForm({preset, fromReview: isReview ? id : null, after: async () => { await loadMeta(); if (after) after(); else route(); }});
  };
  const rsBox = $('#rsTxBox', bg);
  if (rsBox && typeof rsTxBox === 'function') rsTxBox(rsBox, id, () => { bg.close(); after ? after() : route(); }).catch(() => {});
  const lnBox = $('#lnTxBox', bg);
  if (lnBox && typeof lnTxBox === 'function') lnTxBox(lnBox, id, () => { bg.close(); after ? after() : route(); }).catch(() => {});
  const lbox = $('#rbLinkBox', bg);
  if (lbox && (t.kind === 'reimbursement' || (t.kind === 'expense' && t.reimbursable))) rbLinkBox(lbox, id, () => { after ? after() : route(); }).catch(() => {});
  /* receipt photos: new tx = fill the form from OCR and attach on save; existing tx = attach right away */
  const rcIds = [...(opts.receipt_ids || [])]; let rcCount = (opts.receipts || []).length;
  if (id && $('#rcThumbs', bg)) api(`/api/transactions/${id}/receipts`).then(l => { rcCount = l.length; $('#rcThumbs', bg).innerHTML = rcThumbsHtml(l); }).catch(() => {});
  const rcb = $('#rcBtn', bg);
  if (rcb) rcb.onclick = () => rcPick(async fl => {
    const res = await rcProcess(fl); if (!res) return;
    if (id) { const l = await guard(() => Promise.all(res.receipt_ids.map(r => api(`/api/receipts/${r}/link`, {method: 'POST', body: {transaction_id: id}})))); const all = l[l.length - 1] || []; rcCount = all.length; $('#rcThumbs', bg).innerHTML = rcThumbsHtml(all); toast('Photo attached'); return; }
    rcIds.push(...res.receipt_ids); $('#rcThumbs', bg).insertAdjacentHTML('beforeend', rcThumbsHtml(res.receipts));
    const d = res.draft || {}, setv = (n, v) => { if (v !== null && v !== undefined && v !== '' && form[n]) { form[n].value = v; form[n].closest('.f') && form[n].closest('.f').classList.add('unsure'); } };
    setv('amount', d.amount); setv('merchant', d.merchant); if (d.currency) setv('currency', d.currency);
    if (d.date) setv('date', (d.date.length > 10 ? d.date : d.date + ' 12:00').replace(' ', 'T'));
    if (d.category_id) setv('category_id', d.category_id); if (d.account_id) setv('account_id', d.account_id);
    if (!opts.raw_text) opts.raw_text = res.ocr_text;
    toast(res.message || 'Filled from receipt — please check');
  });
  const collect = () => {
    const fd = new FormData(form); const o = {kind};
    for (const k of ['amount', 'currency', 'category_id', 'ticker', 'shares', 'price', 'fee', 'merchant', 'member_id', 'project_id', 'plan_line_id', 'ledger_id', 'tags', 'note', 'to_amount']) o[k] = fd.get(k);
    o.date = (fd.get('date') || '').replace('T', ' ');
    o.account_id = kind === 'transfer' ? fd.get('from_account_id') : fd.get('account_id');
    o.to_account_id = kind === 'transfer' ? fd.get('to_account_id') : null;
    if (kind !== 'transfer' || $('#toAmtWrap', bg).style.display === 'none') o.to_amount = null;
    for (const k of ['to_amount', 'shares', 'price', 'fee']) if (o[k] === '') o[k] = null;
    if (o.amount === '') o.amount = null;
    if (!['expense', 'income', 'refund', 'dividend'].includes(kind)) o.category_id = null;
    o.adj_reason = kind === 'adjustment' ? fd.get('adj_reason') : null;
    if (!o.adj_reason) delete o.adj_reason;
    if (['expense', 'income', 'refund'].includes(kind)) for (const k of ['tax_amount', 'tip_amount', 'tax_rate']) { const v = fd.get(k); o[k] = v === '' || v == null ? null : +v; }
    if (kind === 'reimbursement') o.reimb_payer = fd.get('rb_from') || null;
    if (kind === 'expense') { o.reimbursable = form.reimbursable.checked; o.reimb_payer = fd.get('reimb_payer') || null; o.reimb_expected = fd.get('reimb_expected') || null; o.reimb_note = fd.get('reimb_note') || null; }
    if ($('#prWrap', bg).style.display === 'none') delete o.pr_class; else o.pr_class = fd.get('pr_class') || 'auto';
    if (isNew && opts.raw_text) o.raw_text = opts.raw_text;
    if (isNew && rcIds.length) o.receipt_ids = rcIds;
    return o;
  };
  let saved = 0, busy = false;
  const save = async (approve, again = false) => {
    if (busy) return; busy = true;
    try {
      const o = collect();
      let res;
      if (id) res = await reconApi(approve ? `/api/transactions/${id}/approve` : `/api/transactions/${id}`, {method: approve ? 'POST' : 'PUT', body: o});
      else res = await reconApi('/api/transactions', {method: 'POST', body: Object.assign({remember: true}, o)});
      const file = form.file.files[0];
      if (file) { const fd = new FormData(); fd.append('file', file); await guard(() => api(`/api/transactions/${res.id}/attachment`, {method: 'POST', body: fd})); }
      if (isNew) rememberLocal(kind, {account_id: +o.account_id || null, to_account_id: kind === 'transfer' ? +o.to_account_id || null : undefined, category_id: +o.category_id || null, currency: o.currency, member_id: +o.member_id || null});
      await loadMeta(); after ? after() : route();
      if (again) {
        saved += 1;
        toast(`Saved ✓ ${kindLabel[kind]} ${o.amount ?? ''} ${o.currency || ''} — ready for the next one`);
        for (const n of ['amount', 'merchant', 'note', 'tags', 'to_amount']) form[n].value = '';
        form.file.value = ''; opts.raw_text = null;
        $$('.unsure', bg).forEach(x => x.classList.remove('unsure')); const pb = $('.prefill-banner', bg); if (pb) pb.remove();
        $('.modal header h2', bg).textContent = `New transaction · ${saved} saved`;
        form.amount.focus();
      } else {
        bg.close(); toast(approve ? 'Approved ✓' : 'Saved');
        // a new reimbursement: offer to link it to the open reimbursable expenses it pays back
        if (isNew && kind === 'reimbursement' && res && res.status === 'posted') {
          const sg = await api('/api/reimbursements/suggest?reimbursement_id=' + res.id + '&limit=1').catch(() => null);
          if (sg && sg.items.length) rbLinkDialog({side: 'reimb', id: res.id, after: () => { after ? after() : route(); }});
        }
      }
      if (res && res.reconcile_warning) toast('Saved — note: ' + res.reconcile_warning + '. Today\'s balance is unchanged; see the account\'s reconciliation history for drift.');
    } finally { busy = false; }
  };
  $('[data-cancel]', bg).onclick = bg.close;
  if (SCH) { bindSchedule(bg, form, SCH, collect, after); return bg; }
  const rc = $('[data-recur]', bg); if (rc) rc.onclick = async () => { const pre = await guard(() => api('/api/schedules/from-transaction/' + id)); bg.close(); openSchedule(pre); };
  $('[data-save]', bg).onclick = () => save(false);
  const ag = $('[data-again]', bg); if (ag) ag.onclick = () => save(false, true);
  const ap = $('[data-approve]', bg); if (ap) ap.onclick = () => save(true);
  form.addEventListener('keydown', e => {
    if (e.key !== 'Enter' || e.target.tagName === 'TEXTAREA' && !(e.ctrlKey || e.metaKey)) return;
    if (e.ctrlKey || e.metaKey) { e.preventDefault(); save(isReview, isNew); }
    else if (e.target.name === 'amount') { e.preventDefault(); save(isReview); }
  });
  const del = $('[data-del]', bg); if (del) del.onclick = async () => { if (!await confirmBox('Delete this transaction?')) return;
    const keep = rcCount ? !(await confirmBox(`Also delete its ${rcCount} receipt photo(s)? Cancel keeps the photos in the receipts folder.`)) : false;
    const dr = await reconApi('/api/transactions/' + id + (keep ? '?keep_receipts=true' : ''), {method: 'DELETE'}); bg.close(); toast(dr.reconcile_warning ? 'Deleted — note: ' + dr.reconcile_warning : 'Deleted'); await loadMeta(); after ? after() : route(); };
  const rm = $('#rmAtt', bg); if (rm) rm.onclick = async () => { await api(`/api/transactions/${id}/attachment`, {method: 'DELETE'}); rm.parentElement.remove(); };
  const focusEl = unsure.size ? $('.unsure input, .unsure select', form) : form.amount;
  if (focusEl && !matchMedia('(max-width:760px)').matches) setTimeout(() => focusEl.focus(), 30);
  return bg;
}

/* quick add → pre-filled form (dry-run parse, nothing saved until the user presses Save) */
async function openQuickForm(text) {
  if (!text || !text.trim()) return openTx();
  let r;
  try { r = await api('/api/quick-add', {method: 'POST', body: {text, dry_run: true}}); }
  catch (e) { toast(e.message, true); return; }
  if (r.action === 'set_balance' || r.action === 'reconcile') { toast('Balance commands are applied with Add (or Accounts → Set balance)', true); return; }
  const p = r.prefill || {};
  const preset = {};
  for (const k of ['kind', 'amount', 'currency', 'account_id', 'to_account_id', 'category_id', 'merchant', 'member_id', 'project_id', 'plan_line_id', 'tags', 'date', 'note', 'reimbursable', 'reimb_payer', 'tax_amount', 'tip_amount', 'tax_rate']) if (p[k] !== null && p[k] !== undefined && p[k] !== '') preset[k] = p[k];
  return openTx(null, null, preset, {uncertain: r.uncertain || [], raw_text: text, banner: `Pre-filled from “${esc(text)}” (confidence ${Math.round((r.confidence || 0) * 100)}%). Nothing is saved until you press Save.`});
}// tax/tip are stored in the account currency; show them in the entered currency when the amount was converted
const ttConv = (t, v) => v == null ? '' : (t.orig_currency && t.amount ? Math.round(v * t.orig_amount / t.amount * 100) / 100 : v);


