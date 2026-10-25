/* ---------- Home 首页: month cards + quick add + this month's transactions (infinite scroll into older months) ---------- */
S.homeMonth = null;
const monthLabel = m => { const [y, mo] = m.split('-').map(Number); return new Date(y, mo - 1, 1).toLocaleString(i18nLocale(), {month: 'short', year: 'numeric'}); };
const monthLong = m => { const [y, mo] = m.split('-').map(Number); return new Date(y, mo - 1, 1).toLocaleString(i18nLocale(), {month: 'long', year: 'numeric'}); };
function homeRowHtml(t) {
  const title = t.kind === 'transfer' ? `${esc(t.account_name || '?')} → ${esc(t.to_account_name || '?')}`
    : t.kind === 'adjustment' ? (t.adj_reason === 'opening' ? 'Opening balance' : `Balance adj. · ${esc(t.adj_reason_label || '')}`)
    : ['buy', 'sell', 'dividend', 'invest_fee'].includes(t.kind) ? `${kindLabel[t.kind]} ${esc(t.ticker || '')}`
    : t.kind === 'reimbursement' ? `Reimbursement${t.reimb_payer ? ' · ' + esc(t.reimb_payer) : ''}`
    : t.category_name ? `${esc(t.category_name)}${t.category_name_zh ? ` <span class="muted">${esc(t.category_name_zh)}</span>` : ''}` : 'Uncategorized';
  const what = [t.merchant_name, t.note].filter(Boolean).map(x => esc(String(x).slice(0, 60))).join(' · ');
  const where = t.kind === 'transfer' ? '' : esc(t.account_name || '');
  return `<div class="hrow" data-tx="${t.id}" role="button" tabindex="0">
    <div class="hico" aria-hidden="true">${catIcon(t)}</div>
    <div class="hmain"><div class="htop">${title}${t.status === 'review' ? ' <span class="pill review">review</span>' : ''}${t.kind === 'expense' && t.reimbursable ? ` <span class="pill reimb">${t.reimb_writeoff ? '已核销' : '可报销'}</span>` : ''}${t.reconciled_id ? ' <span class="rec-badge" title="Reconciled">✔</span>' : ''}</div>
      <div class="hsub">${what ? `<span class="hwhat">${what}</span>` : ''}<span class="muted">${esc(t.date.slice(5, 10))} ${esc(t.date.slice(11, 16))}${where ? ' · ' + where : ''}</span></div></div>
    <div class="hamt">${txAmount(t)}</div></div>`;
}
function bindHomeRows(root, after) {
  $$('.hrow[data-tx]', root).forEach(r => { if (r._b) return; r._b = 1; r.onclick = () => openTx(+r.dataset.tx, after); r.onkeydown = e => { if (e.key === 'Enter') openTx(+r.dataset.tx, after); }; });
}
/* scheduled income/expense still to come this month (active schedules, see README) + projected month-end */
function schedRemLine(sc, which, projected) {
  const items = sc.items.filter(i => which === 'net' || i.kind === which);
  if (!items.length) return '';
  const cur = sc.currency;
  const amt = which === 'net' ? `${sc.net >= 0 ? '+' : '−'}${money(Math.abs(sc.net), cur)}` : money(sc[which], cur);
  const rows = items.map(i => `<tr><td class="nowrap">${esc(i.date.slice(5, 10))}</td><td>${esc(i.name)}${i.post_mode === 'remind' ? ` <span class="pill">⏰ ${t('remind only')}</span>` : ''}</td>
    <td class="right nowrap ${i.kind === 'income' ? 'pos' : 'neg'}">${i.amount == null ? `<span class="muted">${t('variable')}</span>` : (which === 'net' ? (i.kind === 'income' ? '+' : '−') : '') + money(i.amount, i.currency)}${i.amount != null && i.currency !== cur ? `<div class="small muted">≈ ${money(i.amount_base, cur)}</div>` : ''}</td></tr>`).join('');
  return `<div class="sched-rem" tabindex="0" role="button" aria-haspopup="true" aria-expanded="false">
    <div class="small">🔁 ${esc(t(items.length === 1 ? 'Scheduled remaining: {amount} ({n} item)' : 'Scheduled remaining: {amount} ({n} items)', {amount: amt, n: items.length}))}</div>
    <div class="small muted">${esc(t('Projected month-end: {amount}', {amount: money(projected, cur)}))}</div>
    <div class="sr-pop"><div class="small muted">${esc(t('Scheduled, not posted yet ({from} – {to})', {from: sc.from.slice(5), to: sc.to.slice(5)}))}</div><table class="t small">${rows}</table>
    ${sc.unknown_amount_count && items.some(i => i.amount == null) ? `<div class="small muted">${t('Variable amounts without a suggestion add 0.')}</div>` : ''}<a class="small" href="#/scheduled">${t('Scheduled →')}</a></div></div>`;
}
function bindSchedRem(root) {
  $$('.sched-rem', root).forEach(el => {
    const set = on => { $$('.sched-rem.open', root).forEach(x => { if (x !== el) { x.classList.remove('open'); x.setAttribute('aria-expanded', 'false'); } }); el.classList.toggle('open', on); el.setAttribute('aria-expanded', String(on)); };
    el.onclick = e => { if (e.target.closest('a')) return; set(!el.classList.contains('open')); };
    el.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); set(!el.classList.contains('open')); } if (e.key === 'Escape') set(false); };
  });
}
VIEWS.home = async (v) => {
  const month = S.homeMonth || thisMonth();
  const isNow = month === thisMonth();
  const lq = S.ledger ? '&ledger_id=' + S.ledger : '';
  v.innerHTML = `${demoBanner()}<div class="home">
    <div class="month-sw"><button class="btn sm" id="hmPrev" aria-label="Previous month">‹</button><b>${esc(monthLabel(month))}</b><button class="btn sm" id="hmNext" aria-label="Next month">›</button>${isNow ? '' : '<button class="btn sm" id="hmNow">This month</button>'}</div>
    <div class="home-cards" id="hmCards"><div class="card stat"><div class="lbl">Income</div><div class="val">…</div></div><div class="card stat"><div class="lbl">Expense</div><div class="val">…</div></div><div class="card stat"><div class="lbl">Net</div><div class="val">…</div></div><div class="card stat"><div class="lbl">Investment income</div><div class="val">…</div></div></div>
    <div class="card home-add">
      <form class="qa" id="hqForm" autocomplete="off"><input id="hqInput" enterkeyhint="send" placeholder="Quick add: lunch 25.5 chase · 午饭 35 支付宝"><button type="button" class="btn" id="hqFormBtn">✎<span class="hide-s"> Review</span></button><button class="btn pri" id="hqAdd">Add</button></form>
      <button type="button" class="btn" id="hqDetail">+ Detailed</button></div>
    <div id="hmRem"></div>
    <div class="card home-list"><div id="hmList" class="hlist"></div></div></div>`;
  api('/api/reminders?view=open').then(d => { if (!v.isConnected) return; if (d.items.length) $('#hmRem').innerHTML = `<div class="card"><div class="row" style="justify-content:space-between"><b>⏰ Reminders</b><a href="#/reminders" class="small">All 全部 ›</a></div>${d.items.slice(0, 5).map(remLine).join('')}</div>`; }).catch(() => {});
  const go = m => { S.homeMonth = m === thisMonth() ? null : m; route(); };
  api(`/api/summary/month?month=${month}${lq}`).then(s => {
    if (!v.isConnected) return;  // left Home before the totals arrived
    $('#hmPrev').onclick = () => go(s.prev); $('#hmNext').onclick = () => go(s.next);
    const card = (lbl, val, cls, extra = '') => `<div class="card stat"><div class="lbl">${lbl}</div><div class="val ${cls}">${money(val, s.currency)}</div>${extra}</div>`;
    const sc = s.scheduled && s.scheduled.count ? s.scheduled : null, pj = s.projected || {};
    $('#hmCards').innerHTML = card('Income', s.income, 'pos', sc ? schedRemLine(sc, 'income', pj.income) : '') + card('Expense', s.expense, 'neg', sc ? schedRemLine(sc, 'expense', pj.expense) : '')
      + card('Net', s.net, s.net >= 0 ? 'pos' : 'neg', `<div class="small muted">${s.count} transactions</div>${sc ? schedRemLine(sc, 'net', pj.net) : ''}`)
      + card('Investment income', s.investment_income, s.investment_income >= 0 ? 'pos' : 'neg', '<div class="small muted">not part of income/expense</div>');
    bindSchedRem($('#hmCards'));
    return api(`/api/reports/tax-tips?start=${month}-01&end=${month}-31${lq}`).then(tt => {  // only when recorded
      const x = tt.totals; if (x.tax_count || x.tip_count) $('#hmCards .card.stat:nth-child(2)')?.insertAdjacentHTML('beforeend', taxTipLine(x, tt.currency));
    }).catch(() => {});
  }).catch(e => toast(e.message, true));
  const ym = month.split('-').map(Number);
  $('#hmPrev').onclick = () => go(ym[1] > 1 ? `${ym[0]}-${String(ym[1] - 1).padStart(2, '0')}` : `${ym[0] - 1}-12`);
  $('#hmNext').onclick = () => go(ym[1] < 12 ? `${ym[0]}-${String(ym[1] + 1).padStart(2, '0')}` : `${ym[0] + 1}-01`);
  if ($('#hmNow')) $('#hmNow').onclick = () => go(thisMonth());
  const after = () => route();  // cards + list refresh after any save
  wireQuickAdd($('#hqForm'), $('#hqInput'), $('#hqFormBtn'), async () => { const r = await doQuickAdd($('#hqInput').value); if (r) { $('#hqInput').value = ''; after(); } });
  $('#hqDetail').onclick = () => openTx(null, after, isNow ? {} : {date: `${month}-01 12:00`});
  const lastDay = new Date(ym[0], ym[1], 0).getDate();
  const end = `${month}-${String(lastDay).padStart(2, '0')}`;
  let curMonth = null;
  const list = $('#hmList');
  infiniteList(list, {
    load: cursor => api(`/api/transactions?end=${end}&limit=50${lq}${cursor ? '&before=' + encodeURIComponent(cursor) : ''}`),
    append: (r, first) => {
      if (first && !r.items.length) list.innerHTML = `<div class="empty">No transactions in ${esc(monthLabel(month))} or earlier. Use the box above to add one.</div>`;
      let html = '';
      for (const t of r.items) {
        const m = t.date.slice(0, 7);
        if (m !== curMonth) { curMonth = m; html += `<div class="hmonth">${esc(monthLong(m))}${m === month ? '' : ' <span class="muted small">· earlier 更早</span>'}</div>`; }
        html += homeRowHtml(t);
      }
      list.insertAdjacentHTML('beforeend', html);
      bindHomeRows(list, after);
    },
    endHtml: 'That\'s everything',
  });
};

/* ---------- Account detail / history 账户明细 (#/account/<id>) ---------- */
S.acctHist = {};
const PERIODS = [['1M', '1M'], ['3M', '3M'], ['6M', '6M'], ['YTD', 'YTD'], ['1Y', '1Y'], ['3Y', '3Y'], ['ALL', 'All'], ['CUSTOM', 'Custom']];
VIEWS.account = async (v, rest, ctx = {}) => {
  const id = parseInt(rest[0]);
  const inInv = ctx.section === 'investments';  // opened from Investments: back goes to the Investments list
  if (!id) { location.hash = inInv ? '#/investments' : '#/accounts'; return; }
  const st = S.acctHist[id] = S.acctHist[id] || {period: '1M'};
  const meta = acctById(id);
  if (meta) $('#title').textContent = meta.name;
  v.innerHTML = `<div class="acct-head card" id="ahHead"><div class="empty">Loading…</div></div><div id="ahInv"></div>
   <div class="card" style="margin-top:12px"><form id="ahFlt" class="ah-flt" autocomplete="off">
     <div class="chips" role="group" aria-label="Period">${PERIODS.map(([k, l]) => `<button type="button" class="chip${st.period === k ? ' on' : ''}" data-p="${k}">${l}</button>`).join('')}</div>
     <span class="ah-custom"${st.period === 'CUSTOM' ? '' : ' hidden'}><input type="date" name="start" value="${esc(st.start || '')}" aria-label="From"> – <input type="date" name="end" value="${esc(st.end || '')}" aria-label="To"></span>
     <input type="search" name="q" placeholder="Search" value="${esc(st.q || '')}">
     <select name="category_id">${catOptions('', st.category_id || '').replace('— Category —', 'All categories')}</select>
   </form><div id="ahSum" class="ah-sum small"></div><div id="ahList"></div></div>`;
  const f = $('#ahFlt');
  const apply = () => { st.q = f.q.value.trim(); st.category_id = f.category_id.value; st.start = f.start.value; st.end = f.end.value; route(); };
  $$('[data-p]', f).forEach(b => b.onclick = () => { st.period = b.dataset.p; if (st.period !== 'CUSTOM') { route(); return; }
    $$('[data-p]', f).forEach(x => x.classList.toggle('on', x === b)); $('.ah-custom', f).hidden = false; f.start.focus(); });
  f.start.onchange = f.end.onchange = () => { if (f.start.value || f.end.value) apply(); };
  f.category_id.onchange = apply;
  let tmr; f.q.oninput = () => { clearTimeout(tmr); tmr = setTimeout(apply, 450); };
  f.onsubmit = e => { e.preventDefault(); apply(); };
  if (st.q) setTimeout(() => { f.q.focus(); f.q.setSelectionRange(f.q.value.length, f.q.value.length); }, 0);
  const params = () => { const p = new URLSearchParams({period: st.period, limit: '50'}); if (st.period === 'CUSTOM') { if (st.start) p.set('start', st.start); if (st.end) p.set('end', st.end); }
    if (st.q) p.set('q', st.q); if (st.category_id) p.set('category_id', st.category_id); return p; };
  let A = null;
  const after = () => route();
  const head = (h) => {
    const a = h.account; A = a; $('#title').textContent = a.name;
    const owed = a.is_liability, b = h.balances;
    $('#ahHead').innerHTML = `<div class="ah-top">${inInv ? '<a href="#/investments" class="small" id="ahBack">← Investments</a>' : '<a href="#/accounts" class="small">← Accounts</a>'}</div>
      <div class="ah-main"><div><h2>${isDemoName(a.name) ? '<span class="pill demo">DEMO</span> ' : ''}${esc(a.name)} ${a.last4 ? `<span class="muted small">••${esc(a.last4)}</span>` : ''}</h2>
        <div class="small muted">${esc(a.type_label)} · ${a.currency}${a.institution ? ' · ' + esc(a.institution) : ''}${a.type === 'credit_card' && a.card_expiry ? ' · ' + cardExpiryPill(a) : ''}${a.reconciled_at ? ` · <a href="javascript:void 0" class="rec-link" id="ahRec">✔ Reconciled ${esc(a.reconciled_at.slice(0, 10))}</a>` : ''}</div></div>
        <div class="ah-bal"><div class="lbl small muted">${owed ? 'Owed' : 'Balance'}</div><div class="val ${owed ? (a.owed > 0 ? 'neg' : '') : a.balance < 0 ? 'neg' : ''}">${money(owed ? a.owed : a.balance, a.currency)}</div>
        ${a.is_investment ? `<div class="small muted">Cash ${money(a.cash_balance, a.currency)} · Holdings ${money(a.holdings_value, a.currency)}</div>` : ''}</div></div>
      <div class="row ah-acts"><button class="btn pri" id="ahAdd">+ Add 记一笔</button><button class="btn" id="ahSet">⚖️ Set balance</button><button class="btn" id="ahReconcile">✔ Reconcile</button><button class="btn" id="ahEdit">Edit</button>
        <a class="btn" href="#/transactions/?account_id=${a.id}">🧾 <span class="hide-s">In Transactions</span></a></div>`;
    if ($('#ahBack')) $('#ahBack').onclick = e => { if (S.invPR && S.invPR.fromList) { e.preventDefault(); S.invPR.fromList = false; history.back(); } };
    $('#ahAdd').onclick = () => openTx(null, after, {account_id: a.id, currency: a.currency});
    $('#ahSet').onclick = () => adjustForm(a);
    $('#ahReconcile').onclick = () => reconcileDialog(a);
    $('#ahEdit').onclick = () => acctForm(a);
    if ($('#ahRec')) $('#ahRec').onclick = () => reconcileHistory(a);
    const lbl = h.start ? `${h.start}${h.end ? ' – ' + h.end : ' – today'}` : (h.end ? `… – ${h.end}` : 'All time');
    $('#ahSum').innerHTML = `<span>${esc(lbl)}</span><span>${owed ? 'Owed' : 'Balance'} at start <b>${money(b.opening, a.currency)}</b> → end <b>${money(b.closing, a.currency)}</b></span>
      <span>In <b class="pos">+${num(b.inflow)}</b></span><span>Out <b class="neg">−${num(b.outflow)}</b></span><span>${h.total} transactions${st.q || st.category_id ? ' (filtered)' : ''}</span>
      ${h.cash_only ? '<span class="muted">Running balance = cash part (holdings excluded)</span>' : ''}`;
    if (h.invest) invSection(h, a, id); else $('#ahInv').innerHTML = '';
  };
  const list = $('#ahList');
  const balHdr = () => A && A.is_liability ? 'Owed' : A && A.is_investment ? 'Cash' : 'Balance';
  const rowHtml = t => `<tr data-tx="${t.id}" style="cursor:pointer"><td class="nowrap small">${esc(t.date.slice(0, 10))}<div class="muted hide-m">${esc(t.date.slice(11, 16))}</div></td>
    <td><div class="ah-det"><span class="hico-s" aria-hidden="true">${catIcon(t)}</span><div>${t.reconciled_id ? '<span class="rec-badge" title="Reconciled">✔🔒</span> ' : ''}${txDesc(t)}</div></div></td>
    <td class="right nowrap ${t.account_effect < 0 ? 'neg' : t.account_effect > 0 ? 'pos' : ''}">${t.account_effect > 0 ? '+' : t.account_effect < 0 ? '−' : ''}${money(Math.abs(t.account_effect), A ? A.currency : undefined)}${t.pr_badge ? `<div><span class="pr-badge pr-${t.pr_class}" title="${esc(t.pr_label || '')}">${t.pr_badge}</span></div>` : ''}</td>
    <td class="right nowrap rb">${t.running_shown === null || t.running_shown === undefined ? '—' : money(t.running_shown, A ? A.currency : undefined)}</td>${A && A.is_investment ? `<td class="right nowrap prc">${t.running_principal === undefined ? '—' : money(t.running_principal, A.currency)}</td><td class="right nowrap prc">${t.running_returns === undefined ? '—' : money(t.running_returns, A.currency)}</td><td class="right nowrap prc">${t.running_unexplained === undefined ? '—' : money(t.running_unexplained, A.currency)}</td>` : ''}</tr>`;
  const NEXT = {'1M': '3M', '3M': '6M', '6M': '1Y', '1Y': '3Y', '3Y': 'ALL', 'YTD': 'ALL', 'CUSTOM': 'ALL'};
  const inf = infiniteList(list, {
    load: cursor => { const p = params(); if (cursor) p.set('before', cursor); return api(`/api/accounts/${id}/history?` + p); },
    append: (h, first) => {
      if (first) {
        head(h);
        list.innerHTML = h.items.length ? `<div class="scroll-x"><table class="t ah-table"><thead><tr><th>Date</th><th>Details</th><th class="right">Amount</th><th class="right">${balHdr()}</th>${A && A.is_investment ? '<th class="right prc">Principal</th><th class="right prc">Returns</th><th class="right prc">Unexplained</th>' : ''}</tr></thead><tbody id="ahBody"></tbody></table></div>`
          : `<div class="empty">No transactions in this period${st.q || st.category_id ? ' matching the filter' : ''}.</div>`;
      }
      if (h.items.length) { $('#ahBody').insertAdjacentHTML('beforeend', h.items.map(rowHtml).join('')); bindTxRows(list, after); }
    },
    endHtml: st.period === 'ALL' ? 'Start of the account' : `Start of the selected period. <button type="button" class="btn sm" data-older>Show older (${NEXT[st.period] === 'ALL' ? 'All' : NEXT[st.period]})</button>`,
  });
  inf.foot.addEventListener('click', e => { if (e.target.closest('[data-older]')) { st.period = NEXT[st.period] || 'ALL'; route(); } });
};

/* investment accounts: principal 本金投入 vs returns 投资收益 (cards, period figures, chart following the period filter) */
const sgn = (v, cur) => `${v > 0 ? '+' : v < 0 ? '−' : ''}${money(Math.abs(v), cur)}`;
const pctS = p => p === null || p === undefined ? '—' : `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`;
function invSection(h, a, id) {
  const x = h.invest, p = x.period, cur = a.currency;
  const box = $('#ahInv');
  box.innerHTML = `<div class="card inv-card" style="margin-top:12px">
    <div class="inv-cards">
      <div class="ic"><div class="lbl">Current value</div><div class="val">${money(x.value, cur)}</div></div>
      <div class="ic"><div class="lbl">Principal</div><div class="val">${money(x.principal, cur)}</div><div class="small muted">net contributions</div></div>
      <div class="ic"><div class="lbl">Returns</div><div class="val ${x.returns < 0 ? 'neg' : x.returns > 0 ? 'pos' : ''}">${sgn(x.returns, cur)}</div><div class="small ${x.returns < 0 ? 'neg' : 'pos'}">${pctS(x.returns_pct)} of principal</div></div>
      <div class="ic${Math.abs(x.unexplained) >= 0.01 ? ' unexp' : ''}"><div class="lbl">Unexplained</div><div class="val">${sgn(x.unexplained, cur)}</div><div class="small muted">${x.correction_count ? `${x.correction_count} unclassified adj. ${money(x.unclassified, cur)}` : 'no unclassified adjustments'}${Math.abs(x.reconciliation_diff) >= 0.01 ? ` · reconciliation diff ${money(x.reconciliation_diff, cur)}` : ''}</div>
        ${x.correction_count ? '<button class="btn sm" id="ahReclass" style="margin-top:6px">🏷 Reclassify</button>' : ''}</div>
    </div>
    <div class="small muted" style="margin-top:6px">Value 市值 = principal 本金 + returns 收益 + unexplained 未分类. Unclassified balance adjustments (Correction/other, e.g. imported 余额变更) stay unexplained until you reclassify them${x.likely_opening_count ? ` — the first record (${money(x.likely_opening_amount, cur)}) is probably the opening balance` : ''}.</div>
    ${x.opening_returns ? `<div class="small muted">Opening balance ${money(x.opening_total, cur)}: ${money(x.opening_total - x.opening_returns, cur)} principal + ${money(x.opening_returns, cur)} prior returns.</div>` : ''}
    <div class="inv-period small" id="ahInvSum"></div>
    <div class="row inv-ctl"><b class="small">Principal vs value 本金 vs 市值</b><span class="sp"></span>
      <label class="small muted">Granularity <select id="ahIv" style="width:auto"><option value="auto">Auto</option><option value="day">Daily</option><option value="week">Weekly</option><option value="month">Monthly</option></select></label></div>
    <div style="height:240px"><canvas id="ahInvChart"></canvas></div>
    <div class="small muted" style="margin-top:6px">Value = principal + returns + unexplained. Click legend items to hide/show lines. ${esc(x.running_note || '')}</div>
  </div>`;
  if ($('#ahReclass')) $('#ahReclass').onclick = () => reclassifyTool(false, [id]);
  const L = $('#ahList'), key = 'ahShowPR';
  L.classList.toggle('show-pr', localStorage.getItem(key) === '1');
  $('#ahSum').insertAdjacentHTML('beforeend', `<button type="button" class="btn sm pr-tog" id="ahPrTog">${L.classList.contains('show-pr') ? 'Hide' : 'Show'} 本金/收益/未分类 columns</button>`);
  $('#ahPrTog').onclick = e => { const on = L.classList.toggle('show-pr'); localStorage.setItem(key, on ? '1' : '0'); e.target.textContent = `${on ? 'Hide' : 'Show'} 本金/收益/未分类 columns`; };
  const draw = sr => {
    const s = sr.summary, P = sr.points;
    $('#ahInvSum').innerHTML = `<span>${esc(sr.from)} – ${esc(sr.to)}</span>
      <span>Principal <b>${money(s.start_principal, cur)}</b> → <b>${money(s.end_principal, cur)}</b></span>
      <span>Contributions <b class="pos">+${num(s.contributions)}</b>${s.withdrawals ? ` · withdrawals <b class="neg">−${num(s.withdrawals)}</b>` : ''}</span>
      <span>Returns <b class="${s.period_returns < 0 ? 'neg' : 'pos'}">${sgn(s.period_returns, cur)}</b> (cumulative ${money(s.start_returns, cur)} → ${money(s.end_returns, cur)})</span>
      ${s.start_unexplained || s.end_unexplained ? `<span>Unexplained ${money(s.start_unexplained, cur)} → ${money(s.end_unexplained, cur)}</span>` : ''}
      <span>Return <b>${pctS(s.return_pct)}</b> <span class="muted">Modified Dietz, explicit returns only</span></span>`;
    const pr = P.length > 60 ? 0 : 2;
    chart('ahInvChart', {type: 'line', data: {labels: P.map(q => q.date), datasets: [
      {label: 'Market value', data: P.map(q => q.value), borderColor: '#3a7bd5', backgroundColor: 'rgba(58,123,213,.10)', fill: '+1', tension: .2, pointRadius: pr},
      {label: 'Principal', data: P.map(q => q.principal), borderColor: '#9aa3b2', backgroundColor: 'rgba(154,163,178,.15)', fill: 'origin', stepped: true, pointRadius: 0},
      {label: 'Returns', data: P.map(q => q.returns), borderColor: '#14a06f', borderDash: [5, 4], fill: false, tension: .2, pointRadius: pr},
      {label: 'Unexplained', data: P.map(q => q.unexplained), borderColor: '#d98c00', borderDash: [2, 3], fill: false, tension: .2, pointRadius: 0}]},
      options: {interaction: {mode: 'index', intersect: false}, scales: {x: {ticks: {maxTicksLimit: 8}}},
        plugins: {tooltip: {callbacks: {label: c => `${c.dataset.label}: ${money(c.parsed.y, cur)}`}}}}});
  };
  draw(x.series);
  $('#ahIv').value = 'auto';
  $('#ahIv').onchange = async e => {
    const q = new URLSearchParams({interval: e.target.value});
    if (h.start) q.set('from', h.start); if (h.end) q.set('to', h.end);
    draw(await guard(() => api(`/api/accounts/${id}/investment-series?` + q)));
  };
}

// Tax & tips 税/小费 KPI line (inside the Expense card): only transactions where they were recorded
function taxTipLine(x, cur) {
  const parts = [];
  if (x.tax_count) parts.push(`<span>Tax</span> ${money(x.tax, cur)}${x.tax_pct != null ? ` (${x.tax_pct}%)` : ''}`);
  if (x.tip_count) parts.push(`<span>Tips</span> ${money(x.tip, cur)}${x.tip_pct != null ? ` (${x.tip_pct}%)` : ''}`);
  return `<div class="small tt-kpi" title="Tax & tips recorded this month (only transactions where they were entered)"><a href="#/reports">🧾 ${parts.join(' · ')}</a></div>`;
}
