/* ---------- Reimbursement center 报销中心 (#/reimb) ----------
   Reimbursable expenses (state open / partial / closed) and reimbursements (allocated / unallocated), linked many-to-many
   with partial amounts. Linking works from either side, here and in the transaction dialog (rbLinkBox). */
S.rb = S.rb || {states: ['open', 'partial'], tab: 'items'};
if (!S.rb.states) S.rb.states = ['open', 'partial'];
const RB_STATE = {open: 'Open', partial: 'Partial', closed: 'Closed'};
const rbBadge = x => `<span class="rb-st rb-${x.reimb_state || 'open'}" title="${x.reimb_closed_by === 'written_off' ? 'Closed by hand: the rest was written off and counts as an expense' : x.reimb_state === 'closed' ? 'Fully reimbursed' : ''}">${RB_STATE[x.reimb_state] || 'Open'}${x.reimb_closed_by === 'written_off' ? ' · ' + t('Written off') : ''}</span>`;
const rbWho = l => esc(l.expense_merchant || l.expense_category_zh && I18N.lang.startsWith('zh') && l.expense_category_zh || l.expense_category || l.expense_note || '#' + l.expense_id);
const rbFrom = l => esc(l.reimbursement_payer || l.reimbursement_note || l.reimbursement_account || '#' + l.reimbursement_id);
/* one link line: "↩ date · who · amount [✎] [✕]"; side = which end is described */
function rbLinkLine(l, side) {
  const amt = side === 'reimb' ? money(l.amount, l.expense_currency) : money(l.alloc_amount, l.reimbursement_currency);
  const other = side === 'reimb' ? (l.expense_currency !== l.reimbursement_currency ? ` <span class="muted">(${money(l.alloc_amount, l.reimbursement_currency)})</span>` : '') : (l.expense_currency !== l.reimbursement_currency ? ` <span class="muted">(${money(l.amount, l.expense_currency)})</span>` : '');
  const what = side === 'reimb' ? `${esc((l.reimbursement_date || '').slice(0, 10))} · ${rbFrom(l)}` : `${esc((l.expense_date || '').slice(0, 10))} · ${rbWho(l)}`;
  return `<span class="rb-link" data-lid="${l.id}">${side === 'reimb' ? '↩' : '🧾'} ${what} <b>${amt}</b>${other}<button type="button" class="lnk" data-ledit="${l.id}" data-amt="${l.amount}" data-alloc="${l.alloc_amount}" data-xc="${l.expense_currency !== l.reimbursement_currency ? 1 : ''}" data-cur="${esc(l.expense_currency)}" title="Edit the linked amount" aria-label="Edit the linked amount">✎</button><button type="button" class="lnk danger" data-lun="${l.id}" title="Unlink" aria-label="Unlink">✕</button></span>`;
}
/* edit / unlink buttons inside root (after() refreshes) */
function rbBindLinkButtons(root, after) {
  $$('[data-lun]', root).forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    if (!await confirmBox('Unlink? The expense becomes outstanding again by this amount and the reimbursement money becomes unallocated.')) return;
    await guard(() => api('/api/reimbursements/links/' + b.dataset.lun, {method: 'DELETE'})); toast(t('Unlinked')); after();
  });
  $$('[data-ledit]', root).forEach(b => b.onclick = ev => {
    ev.stopPropagation();
    const xc = !!b.dataset.xc;
    const bg = modal(t('Edit linked amount'), `<form class="form" id="rbEd" autocomplete="off">
      <label class="f"><span>${t('Amount of the expense paid back')} (${esc(b.dataset.cur)})</span><input name="amount" type="number" step="0.01" min="0.01" inputmode="decimal" value="${b.dataset.amt}"></label>
      ${xc ? `<label class="f"><span>${t('Part of the reimbursement used')}</span><input name="alloc" type="number" step="0.01" min="0.01" inputmode="decimal" value="${b.dataset.alloc}"></label>` : ''}</form>`,
      {buttons: '<button class="btn" data-x2>Cancel</button><button class="btn pri" data-ok>Save</button>'});
    $('[data-x2]', bg).onclick = bg.close;
    $('[data-ok]', bg).onclick = async () => {
      const f = $('#rbEd', bg); const body = {amount: +f.amount.value};
      if (xc) body.alloc_amount = +f.alloc.value;
      await guard(() => api('/api/reimbursements/links/' + b.dataset.ledit, {method: 'PUT', body})); bg.close(); toast(t('Saved')); after();
    };
  });
}

VIEWS.reimb = async (v) => {
  const st = S.rb;
  const q = new URLSearchParams({status: st.states.length ? st.states.join(',') : 'none'});
  for (const k of ['start', 'end', 'payer', 'account_id']) if (st[k]) q.set(k, st[k]);
  const [d, rc] = await Promise.all([api('/api/reimbursements?' + q), api(`/api/reimbursements/receipts?limit=200${st.unallocOnly ? '&unallocated=1' : ''}`)]);
  const o = d.outstanding, un = d.unallocated;
  const sel = new Set();
  v.innerHTML = `<div class="card"><div class="rb-cards">
      <div class="ic"><div class="lbl">Outstanding</div><div class="val">${o.by_currency.length ? o.by_currency.map(c => money(c.amount, c.currency)).join('<br>') : money(0)}</div>${o.by_currency.length > 1 ? `<div class="small muted">≈ ${money(o.total_base, o.base_currency)}</div>` : ''}</div>
      <div class="ic"><div class="lbl">Unallocated reimbursements</div><div class="val">${un.by_currency.length ? un.by_currency.map(c => money(c.amount, c.currency)).join('<br>') : money(0)}</div><div class="small muted">${t('{n} to link', {n: un.count})}</div></div>
      ${o.by_payer.slice(0, 4).map(p => `<div class="ic"><div class="lbl">${esc(p.payer === '(none)' ? t('No payer') : p.payer)}</div><div class="val">${p.by_currency.map(c => money(c.amount, c.currency)).join('<br>')}</div></div>`).join('')}
    </div>
    <div class="chips" style="margin-top:12px">${[['items', '🧾 ' + t('Reimbursable expenses')], ['receipts', `💼 ${t('Reimbursements received')} (${rc.items.length})`]].map(([k, l]) => `<button class="chip${st.tab === k ? ' on' : ''}" data-tab="${k}">${l}</button>`).join('')}</div>
    <div id="rbBody"></div></div>`;
  $$('[data-tab]', v).forEach(b => b.onclick = () => { st.tab = b.dataset.tab; route(); });
  const body = $('#rbBody');
  if (st.tab === 'receipts') {
    body.innerHTML = `<div class="rb-flt"><label class="chk"><input type="checkbox" id="rbUn" ${st.unallocOnly ? 'checked' : ''}> ${t('Only with unallocated money')}</label><span class="sp"></span><span class="small muted">${t('Enter a reimbursement anywhere (form, quick add, import) and link it here.')}</span></div>
      ${rc.items.length ? `<div class="scroll-x"><table class="t rb-rc"><thead><tr><th>Date</th><th>Reimbursement</th><th class="right">Amount</th><th class="right">Allocated</th><th class="right">Unallocated</th><th>Linked expenses</th><th></th></tr></thead><tbody>
      ${rc.items.map(r => `<tr data-rid="${r.id}"><td class="nowrap small">${esc(r.date.slice(0, 10))}</td><td class="rb-open-tx"><b>${esc(r.reimb_payer || t('Reimbursement'))}</b><div class="small muted">${esc(r.account_name || '')}${r.note ? ' · ' + esc(r.note) : ''}</div></td>
        <td class="right nowrap pos">+${money(r.amount, r.currency)}</td><td class="right nowrap">${money(r.reimb_allocated, r.currency)}</td>
        <td class="right nowrap ${r.reimb_unallocated > 0.005 ? 'warn-t' : 'muted'}"><b>${money(r.reimb_unallocated, r.currency)}</b>${r.reimb_done ? `<div class="small muted">✓ ${t('Complete')}${r.reimb_leftover > 0.005 ? ' · ' + esc(t('{amt} left as is', {amt: money(r.reimb_leftover, r.currency)})) : ''}</div>` : ''}</td>
        <td class="rb-links">${r.links.map(l => rbLinkLine(l, 'expense')).join('') || '<span class="muted">—</span>'}</td>
        <td class="rb-act">${r.reimb_unallocated > 0.005 ? `<button class="btn sm pri" data-lnr="${r.id}">🔗 ${t('Link expenses')}</button> <button class="btn sm" data-rdone="${r.id}" title="${esc(t('Nothing more to link: the unallocated rest is left as is and it is no longer offered when linking'))}">✓ ${t('Mark complete')}</button>` : ''}${r.reimb_done ? `<button class="btn sm" data-rreopen="${r.id}">${t('Reopen')}</button>` : ''} <button class="btn sm danger" data-delr="${r.id}" title="Delete the reimbursement: its links are removed and the expenses become outstanding again">${t('Delete')}</button></td></tr>`).join('')}</tbody></table></div>`
      : `<div class="empty">${st.unallocOnly ? t('Every reimbursement is fully linked ✓') : t('No reimbursements recorded yet')}</div>`}`;
    $('#rbUn').onchange = e => { st.unallocOnly = e.target.checked; route(); };
    $$('[data-lnr]', body).forEach(b => b.onclick = () => rbLinkDialog({side: 'reimb', id: +b.dataset.lnr, after: route}));
    $$('[data-rdone]', body).forEach(b => b.onclick = () => rbComplete(rc.items.find(r => r.id === +b.dataset.rdone), route));
    $$('[data-rreopen]', body).forEach(b => b.onclick = () => rbComplete(rc.items.find(r => r.id === +b.dataset.rreopen), route, true));
    $$('[data-delr]', body).forEach(b => b.onclick = async () => {
      if (!await confirmBox('Delete this reimbursement? Its links are removed, the linked expenses become outstanding again (the account balance changes back).')) return;
      await guard(() => api('/api/transactions/' + b.dataset.delr, {method: 'DELETE'})); toast(t('Deleted')); route();
    });
    $$('td.rb-open-tx', body).forEach(td => { td.style.cursor = 'pointer'; td.onclick = () => openTx(+td.parentElement.dataset.rid, () => route()); });
    rbBindLinkButtons(body, route);
    return;
  }
  const sc = d.state_counts || {};
  body.innerHTML = `<form class="rb-flt" id="rbFlt" autocomplete="off">
      <span class="chips rb-states" role="group" aria-label="Status">${Object.entries(RB_STATE).map(([k, l]) => `<button type="button" class="chip${st.states.includes(k) ? ' on' : ''}" data-st="${k}" aria-pressed="${st.states.includes(k)}">${t(l)} <span class="cnt">${sc[k] || 0}</span></button>`).join('')}</span>
      <input type="date" name="start" value="${esc(st.start || '')}" aria-label="From"> – <input type="date" name="end" value="${esc(st.end || '')}" aria-label="To">
      <select name="payer"><option value="">All payers</option><option value="(none)" ${st.payer === '(none)' ? 'selected' : ''}>(no payer)</option>${d.payers.map(p => `<option ${st.payer === p ? 'selected' : ''}>${esc(p)}</option>`).join('')}</select>
      <select name="account_id">${acctOptions(st.account_id || '', {blank: 'All accounts'})}</select>
      <span class="sp"></span><button type="button" class="btn sm" id="rbFlag">＋ Flag expenses…</button></form>
    ${d.items.length ? `<div class="scroll-x"><table class="t rb-ex"><thead><tr><th><input type="checkbox" id="rbAll" aria-label="Select all"></th><th>Date</th><th>Expense</th><th>Payer</th><th class="right">Expected</th><th class="right">Reimbursed</th><th class="right">Outstanding</th><th>Status</th><th></th></tr></thead><tbody>
      ${d.items.map(x => `<tr data-id="${x.id}"><td>${x.reimb_outstanding > 0.005 ? `<input type="checkbox" data-sel="${x.id}" aria-label="Select">` : ''}</td><td class="nowrap small">${esc(x.date.slice(0, 10))}</td>
        <td><div class="ah-det"><span class="hico-s" aria-hidden="true">${catIcon(x)}</span><div><div class="rb-open-tx">${txDesc(x)}</div>${x.reimb_note ? `<div class="small muted">📝 ${esc(x.reimb_note)}</div>` : ''}${x.reimb_links.length ? `<div class="rb-links">${x.reimb_links.map(l => rbLinkLine(l, 'reimb')).join('')}</div>` : ''}</div></div></td>
        <td>${esc(x.reimb_payer || '—')}</td><td class="right nowrap">${money(x.reimb_expected_amount, x.currency)}${Math.abs(x.reimb_expected_amount - x.amount) > 0.005 ? `<div class="small muted">${t('paid')} ${money(x.amount, x.currency)}</div>` : ''}</td>
        <td class="right nowrap">${x.reimb_settled ? money(x.reimb_settled, x.currency) : '—'}</td><td class="right nowrap ${x.reimb_outstanding > 0 ? 'neg' : ''}"><b>${money(x.reimb_outstanding, x.currency)}</b>${x.reimb_writeoff_amount ? `<div class="small muted" title="Counts as a personal expense">${t('written off')} ${money(x.reimb_writeoff_amount, x.currency)}</div>` : ''}</td>
        <td>${rbBadge(x)}</td>
        <td class="rb-act">${x.reimb_state !== 'closed' ? `<button class="btn sm pri" data-lne="${x.id}" title="Link reimbursements that pay this back">🔗 ${t('Link')}</button> <button class="btn sm" data-close="${x.id}" title="Close it: the outstanding rest won't be reimbursed and counts as a normal expense">${t('Close &amp; write off')}</button>` : x.reimb_closed_by === 'written_off' ? `<button class="btn sm" data-reopen="${x.id}">${t('Reopen')}</button>` : ''}
          ${x.reimb_links.length ? '' : `<button class="btn sm" data-unmark="${x.id}" title="Not reimbursable: counts as a normal expense">${t('Unmark')}</button>`}</td></tr>`).join('')}</tbody></table></div>
      <div class="rb-bar"><span id="rbSelInfo" class="small muted">${t('Select expenses to record a new reimbursement, or use 🔗 Link for one already entered')}</span><span class="sp"></span><button class="btn pri" id="rbRecord" disabled>💼 ${t('Record reimbursement')}</button></div>`
      : `<div class="empty">${st.states.join() === 'open,partial' ? 'Nothing outstanding 🎉 Flag an expense as reimbursable in its form, with “reimb …” / “#reimb” in quick add, or with the button above.' : t('No matching expenses')}</div>`}`;
  const f = $('#rbFlt');
  f.onchange = () => { for (const k of ['start', 'end', 'payer', 'account_id']) st[k] = f[k].value; route(); };
  $$('[data-st]', f).forEach(b => b.onclick = () => { const k = b.dataset.st; st.states = st.states.includes(k) ? st.states.filter(x => x !== k) : [...st.states, k]; route(); });
  $('#rbFlag').onclick = () => flagDialog();
  const items = Object.fromEntries(d.items.map(x => [x.id, x]));
  const upd = () => {
    const ch = [...sel].map(i => items[i]); const by = {};
    ch.forEach(x => by[x.currency] = (by[x.currency] || 0) + x.reimb_outstanding);
    const info = $('#rbSelInfo'); if (!info) return;
    info.innerHTML = ch.length ? `<b>${ch.length}</b> ${t('selected')} · ${Object.entries(by).map(([c, a]) => money(a, c)).join(' + ')}` : t('Select expenses to record a new reimbursement, or use 🔗 Link for one already entered');
    $('#rbRecord').disabled = !ch.length;
  };
  $$('[data-sel]', body).forEach(c => c.onchange = () => { c.checked ? sel.add(+c.dataset.sel) : sel.delete(+c.dataset.sel); upd(); });
  if ($('#rbAll')) $('#rbAll').onchange = e => { $$('[data-sel]', body).forEach(c => { c.checked = e.target.checked; c.onchange(); }); };
  $$('[data-lne]', body).forEach(b => b.onclick = () => rbLinkDialog({side: 'expense', id: +b.dataset.lne, after: route}));
  $$('[data-close]', body).forEach(b => b.onclick = async () => {
    const x = items[b.dataset.close];
    if (!await confirmBox(t('Close this expense? The outstanding {amt} won\'t be reimbursed and counts as a normal expense (you can reopen it).', {amt: money(x.reimb_outstanding, x.currency)}))) return;
    await guard(() => api(`/api/reimbursements/${b.dataset.close}/close`, {method: 'POST'})); toast(t('Closed')); route();
  });
  $$('[data-reopen]', body).forEach(b => b.onclick = async () => { await guard(() => api(`/api/reimbursements/${b.dataset.reopen}/reopen`, {method: 'POST'})); toast(t('Reopened')); route(); });
  $$('[data-unmark]', body).forEach(b => b.onclick = async () => {
    await guard(() => api('/api/reimbursements/mark', {method: 'POST', body: {ids: [+b.dataset.unmark], reimbursable: false}})); toast(t('No longer reimbursable — counts as a normal expense')); route();
  });
  $$('.rb-open-tx', body).forEach(el => { el.style.cursor = 'pointer'; el.onclick = () => openTx(+el.closest('tr').dataset.id, () => route()); });
  rbBindLinkButtons(body, route);
  if ($('#rbRecord')) $('#rbRecord').onclick = () => recordDialog([...sel].map(i => items[i]));
};

/* ---------- link dialog (either side) ----------
   side 'reimb': from a reimbursement, pick open reimbursable expenses; side 'expense': from an expense, pick
   reimbursements with unallocated money. Amounts are in the expense's currency (the part of the reimbursement used is
   asked for separately when the currencies differ). Suggestions come best-first (amount / date / payer). */
async function rbLinkDialog({side, id, after}) {
  const d = await guard(() => api(`/api/reimbursements/suggest?${side === 'reimb' ? 'reimbursement_id' : 'expense_id'}=${id}&limit=100`));
  const me = side === 'reimb' ? d.reimbursement : d.expense;
  const cands = d.items;
  const pool = side === 'reimb' ? me.reimb_unallocated : me.reimb_outstanding;  // what the selection may use up
  const poolCur = me.currency;
  const MATCH = {amount: '💯 ' + t('amount'), fits: t('fits'), date: '📅 ' + t('date'), payer: '👤 ' + t('payer')};
  const head = side === 'reimb'
    ? `<div class="rb-head"><div><b>💼 ${esc(me.reimb_payer || t('Reimbursement'))}</b> <span class="small muted">${esc(me.date.slice(0, 10))} · ${esc(me.account_name || '')}${me.note ? ' · ' + esc(me.note) : ''}</span></div>
       <div class="small">${t('Amount')} <b>${money(me.amount, me.currency)}</b> · ${t('Allocated')} ${money(me.reimb_allocated, me.currency)} · ${t('Left to allocate')} <b id="rbLeft">${money(pool, poolCur)}</b></div></div>`
    : `<div class="rb-head"><div><b>🧾 ${esc(me.merchant_name || me.category_label || me.note || '#' + me.id)}</b> <span class="small muted">${esc(me.date.slice(0, 10))} · ${esc(me.account_name || '')}${me.reimb_payer ? ' · ' + esc(me.reimb_payer) : ''}</span> ${rbBadge(me)}</div>
       <div class="small">${t('Expected')} <b>${money(me.reimb_expected_amount, me.currency)}</b> · ${t('Reimbursed')} ${money(me.reimb_settled, me.currency)} · ${t('Outstanding')} <b id="rbLeft">${money(pool, poolCur)}</b></div></div>`;
  const mine = (side === 'reimb' ? me.links : me.reimb_links) || [];
  const rowHtml = c => {
    const xc = (side === 'reimb' ? c.currency : me.currency) !== (side === 'reimb' ? me.currency : c.currency);
    const ecur = side === 'reimb' ? c.currency : me.currency;
    const desc = side === 'reimb'
      ? `${txDesc(c)}`
      : `<div><b>${esc(c.reimb_payer || t('Reimbursement'))}</b></div><div class="small muted">${esc(c.account_name || '')}${c.note ? ' · ' + esc(c.note) : ''}</div>`;
    const avail = side === 'reimb' ? `${money(c.reimb_outstanding, c.currency)}<div class="small muted">${t('outstanding')}</div>` : `${money(c.reimb_unallocated, c.currency)}<div class="small muted">${t('unallocated')}</div>`;
    return `<tr data-c="${c.id}" class="${c.match.includes('amount') ? 'rb-best' : ''}"><td><input type="checkbox" data-pick="${c.id}" aria-label="Select"></td><td class="nowrap small">${esc(c.date.slice(0, 10))}</td><td>${desc}<div class="rb-match">${c.match.map(m => `<span class="pill">${MATCH[m] || m}</span>`).join(' ')}</div></td>
      <td class="right nowrap">${avail}</td>
      <td class="nowrap"><input type="number" step="0.01" min="0" inputmode="decimal" class="rb-amt" data-amt="${c.id}" placeholder="0.00" aria-label="Amount"> <span class="small muted">${esc(ecur)}</span>
        ${xc ? `<div class="small"><input type="number" step="0.01" min="0" inputmode="decimal" class="rb-amt" data-alloc="${c.id}" placeholder="${t('reimb. part')}" aria-label="Part of the reimbursement used"> <span class="muted">${esc(side === 'reimb' ? me.currency : c.currency)}</span></div>` : ''}</td>
      <td><button type="button" class="btn sm" data-fill="${c.id}" title="Fill the outstanding amount (limited by what is left)">${t('Fill outstanding')}</button></td></tr>`;
  };
  const bg = modal(side === 'reimb' ? t('Link expenses to this reimbursement') : t('Link reimbursements to this expense'), `${head}
    ${mine.length ? `<div class="rb-mine"><div class="small muted">${t('Linked')}</div><div class="rb-links">${mine.map(l => rbLinkLine(l, side === 'reimb' ? 'expense' : 'reimb')).join('')}</div></div>` : ''}
    <div class="row rb-tools"><input type="search" id="rbQ" placeholder="${t('Search')}" aria-label="Search"><span class="sp"></span><button type="button" class="btn sm" id="rbFillAll">${t('Fill outstanding')} ✓</button></div>
    ${cands.length ? `<div class="scroll-x rb-cands"><table class="t"><thead><tr><th></th><th>Date</th><th>${side === 'reimb' ? t('Open reimbursable expense') : t('Reimbursement with unallocated money')}</th><th class="right">${side === 'reimb' ? t('Outstanding') : t('Unallocated')}</th><th>${t('Link amount')}</th><th></th></tr></thead><tbody>${cands.map(rowHtml).join('')}</tbody></table></div>`
      : `<div class="empty">${side === 'reimb' ? t('No open reimbursable expenses. Flag expenses as reimbursable first.') : t('No reimbursements with unallocated money. Record a reimbursement (kind Reimbursement) first.')}</div>`}
    <div class="small muted" id="rbWarn" role="status"></div>`,
    {wide: true, buttons: `<span class="small muted" id="rbSum"></span><span class="sp"></span><button class="btn" data-x2>Cancel</button><button class="btn pri" data-ok disabled>🔗 ${t('Link selected')}</button>`});
  const byId = Object.fromEntries(cands.map(c => [c.id, c]));
  const rates = Object.fromEntries((S.meta.currencies || []).map(c => [c.currency, c.rate]));
  const conv = (v, from, to) => from === to ? v : rates[from] && rates[to] ? v * rates[from] / rates[to] : v;
  /* used = Σ in the pool's currency: reimbursement side -> alloc parts; expense side -> expense amounts */
  const used = () => cands.reduce((s, c) => {
    if (!$(`[data-pick="${c.id}"]`, bg).checked) return s;
    const a = +($(`[data-amt="${c.id}"]`, bg).value || 0), al = $(`[data-alloc="${c.id}"]`, bg);
    return s + (side === 'reimb' ? (al ? +(al.value || 0) : a) : a);
  }, 0);
  const refresh = () => {
    const u = used(), left = Math.round((pool - u) * 100) / 100, n = $$('[data-pick]:checked', bg).length;
    $('#rbLeft', bg).textContent = money(Math.max(left, 0), poolCur);
    $('#rbLeft', bg).classList.toggle('neg', left < -0.005);
    const bad = cands.filter(c => $(`[data-pick="${c.id}"]`, bg).checked && !(+$(`[data-amt="${c.id}"]`, bg).value > 0));
    $('#rbWarn', bg).textContent = left < -0.005 ? t('Over by {amt}: lower the amounts', {amt: money(-left, poolCur)}) : bad.length ? t('Enter an amount for every selected row') : '';
    $('#rbSum', bg).textContent = n ? t('{n} selected · {amt}', {n, amt: money(u, poolCur)}) : '';
    $('[data-ok]', bg).disabled = !n || left < -0.005 || bad.length > 0;
  };
  const fill = cid => {  // the smaller of the row's outstanding/unallocated and what the pool has left (excluding this row)
    const c = byId[cid]; const amt = $(`[data-amt="${cid}"]`, bg), al = $(`[data-alloc="${cid}"]`, bg), pick = $(`[data-pick="${cid}"]`, bg);
    amt.value = ''; if (al) al.value = ''; pick.checked = false;
    const leftPool = pool - used();
    if (side === 'reimb') {
      const leftInExp = conv(leftPool, me.currency, c.currency);
      const v = Math.max(0, Math.min(c.reimb_outstanding, leftInExp));
      amt.value = v.toFixed(2); if (al) al.value = Math.min(leftPool, conv(v, c.currency, me.currency)).toFixed(2);
    } else {
      const unInExp = conv(c.reimb_unallocated, c.currency, me.currency);
      const v = Math.max(0, Math.min(leftPool, unInExp));
      amt.value = v.toFixed(2); if (al) al.value = Math.min(c.reimb_unallocated, conv(v, me.currency, c.currency)).toFixed(2);
    }
    pick.checked = +amt.value > 0; refresh();
  };
  $$('[data-fill]', bg).forEach(b => b.onclick = () => fill(+b.dataset.fill));
  $$('[data-pick]', bg).forEach(c => c.onchange = () => { const a = $(`[data-amt="${c.dataset.pick}"]`, bg); if (c.checked && !a.value) fill(+c.dataset.pick); else { if (!c.checked) { a.value = ''; const al = $(`[data-alloc="${c.dataset.pick}"]`, bg); if (al) al.value = ''; } refresh(); } });
  $$('.rb-amt', bg).forEach(i => i.oninput = () => { const cid = i.dataset.amt || i.dataset.alloc; const c = byId[cid];
    if (i.dataset.amt) { const al = $(`[data-alloc="${cid}"]`, bg); if (al) al.value = i.value === '' ? '' : (side === 'reimb' ? conv(+i.value, c.currency, me.currency) : conv(+i.value, me.currency, c.currency)).toFixed(2); }
    $(`[data-pick="${cid}"]`, bg).checked = +($(`[data-amt="${cid}"]`, bg).value) > 0; refresh(); });
  $('#rbFillAll', bg).onclick = () => { const vis = cands.filter(c => $(`tr[data-c="${c.id}"]`, bg).style.display !== 'none'); const sel = vis.filter(c => $(`[data-pick="${c.id}"]`, bg).checked); (sel.length ? sel : vis).forEach(c => fill(c.id)); };
  $('#rbQ', bg).oninput = e => { const q = e.target.value.trim().toLowerCase(); $$('tr[data-c]', bg).forEach(tr => tr.style.display = !q || tr.textContent.toLowerCase().includes(q) ? '' : 'none'); };
  const reload = () => { bg.close(); rbLinkDialog({side, id, after}); after && after(); };
  rbBindLinkButtons($('.rb-mine', bg) || bg, reload);
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const items = cands.filter(c => $(`[data-pick="${c.id}"]`, bg).checked).map(c => {
      const it = {amount: +$(`[data-amt="${c.id}"]`, bg).value}; const al = $(`[data-alloc="${c.id}"]`, bg);
      if (al && al.value !== '') it.alloc_amount = +al.value;
      it[side === 'reimb' ? 'expense_id' : 'reimbursement_id'] = c.id; return it;
    });
    const r = await guard(() => api('/api/reimbursements/links', {method: 'POST', body: {[side === 'reimb' ? 'reimbursement_id' : 'expense_id']: id, items}}));
    bg.close();
    const closed = r.expenses.filter(x => x.reimb_state === 'closed').length;
    toast(t('Linked {n} · {c} closed', {n: r.links.length, c: closed}));
    after && after();
  };
  refresh();
  return bg;
}

/* ---------- links box in the transaction dialog ---------- */
/* a reimbursement with money left over that will never be linked: complete it, so it stops showing when linking */
async function rbComplete(r, after, undo) {
  if (!undo && !await confirmBox(t('Mark this reimbursement complete? The unallocated {amt} is left as is and it is no longer offered when you link expenses (you can reopen it).', {amt: money(r.reimb_unallocated, r.currency)}))) return;
  await guard(() => api(`/api/reimbursements/receipts/${r.id}/complete`, {method: 'POST', body: undo ? {undo: true} : {}}));
  toast(undo ? t('Reopened') : t('Marked complete')); after && after();
}
async function rbLinkBox(box, id, after) {
  const t0 = await api('/api/transactions/' + id);
  const isR = t0.kind === 'reimbursement';
  if (!isR && !(t0.kind === 'expense' && t0.reimbursable)) { box.innerHTML = ''; box.hidden = true; return; }
  box.hidden = false;
  const links = t0.reimb_links || [];
  const sum = isR
    ? `${t('Allocated')} <b>${money(t0.reimb_allocated, t0.currency)}</b> · ${t('Unallocated')} <b class="${t0.reimb_unallocated > 0.005 ? 'warn-t' : ''}">${money(t0.reimb_unallocated, t0.currency)}</b>${t0.reimb_done ? ` · ✓ ${t('Complete')}${t0.reimb_leftover > 0.005 ? ' · ' + esc(t('{amt} left as is', {amt: money(t0.reimb_leftover, t0.currency)})) : ''}` : ''}`
    : `${rbBadge(t0)} ${t('Reimbursed')} <b>${money(t0.reimb_settled, t0.currency)}</b> · ${t('Outstanding')} <b>${money(t0.reimb_outstanding, t0.currency)}</b>${t0.reimb_writeoff_amount ? ` · ${t('written off')} ${money(t0.reimb_writeoff_amount, t0.currency)}` : ''}`;
  const canLink = isR ? t0.reimb_unallocated > 0.005 : t0.reimb_state !== 'closed';
  box.innerHTML = `<div class="row" style="flex-wrap:wrap;gap:6px"><b>🔗 ${isR ? t('Pays back') : t('Paid back by')}</b><span class="small">${sum}</span><span class="sp"></span>
    ${canLink ? `<button type="button" class="btn sm pri" id="rbBoxLink">${isR ? t('Link expenses') : t('Link reimbursements')}…</button>` : ''}
    ${!isR && t0.reimb_state !== 'closed' ? `<button type="button" class="btn sm" id="rbBoxClose" title="Close it: the outstanding rest won't be reimbursed and counts as a normal expense">${t('Close &amp; write off')}</button>` : ''}
    ${!isR && t0.reimb_closed_by === 'written_off' ? `<button type="button" class="btn sm" id="rbBoxReopen">${t('Reopen')}</button>` : ''}
    ${isR && canLink ? `<button type="button" class="btn sm" id="rbBoxDone">✓ ${t('Mark complete')}</button>` : ''}${isR && t0.reimb_done ? `<button type="button" class="btn sm" id="rbBoxUndone">${t('Reopen')}</button>` : ''}</div>
    <div class="rb-links">${links.map(l => rbLinkLine(l, isR ? 'expense' : 'reimb')).join('') || `<span class="small muted">${isR ? t('Not linked to any expense yet.') : t('No reimbursement linked yet.')}</span>`}</div>`;
  const re = () => { rbLinkBox(box, id, after); after && after(); };
  rbBindLinkButtons(box, re);
  bind('#rbBoxLink', 'click', () => rbLinkDialog({side: isR ? 'reimb' : 'expense', id, after: re}), box);
  bind('#rbBoxClose', 'click', async () => {
    if (!await confirmBox(t('Close this expense? The outstanding {amt} won\'t be reimbursed and counts as a normal expense (you can reopen it).', {amt: money(t0.reimb_outstanding, t0.currency)}))) return;
    await guard(() => api(`/api/reimbursements/${id}/close`, {method: 'POST'})); toast(t('Closed')); re();
  }, box);
  bind('#rbBoxDone', 'click', () => rbComplete(t0, re), box);
  bind('#rbBoxUndone', 'click', () => rbComplete(t0, re, true), box);
  bind('#rbBoxReopen', 'click', async () => { await guard(() => api(`/api/reimbursements/${id}/reopen`, {method: 'POST'})); toast(t('Reopened')); re(); }, box);
}

function recordDialog(list) {
  const first = list[0];
  const payers = [...new Set(list.map(x => x.reimb_payer).filter(Boolean))];
  const curs = [...new Set(list.map(x => x.currency))];
  const bg = modal(t('Record reimbursement'), `<form id="rbF" class="form" autocomplete="off">
    <label class="f"><span>Received into</span><select name="account_id">${acctOptions(first.account_id)}</select></label>
    <label class="f"><span>Date</span><input type="date" name="date" value="${today()}"></label>
    <label class="f"><span>Amount received <small class="muted" id="rbCur"></small></span><input name="amount" type="number" step="0.01" inputmode="decimal" placeholder="default: the sum below"></label>
    <label class="f"><span>Reimburse from</span><input name="payer" list="dlPayer" value="${esc(payers.length === 1 ? payers[0] : '')}"></label>
    <label class="f full"><span>Note</span><input name="note" placeholder="e.g. expense report #12"></label>
    <div class="full"><div class="small muted" style="margin-bottom:4px">Amount settled per expense (in the expense's currency; lower it for a partial reimbursement):</div>
      <table class="t"><tbody>${list.map(x => `<tr><td class="small nowrap">${esc(x.date.slice(0, 10))}</td><td class="small">${esc(x.merchant_name || x.category_label || x.note || '')}</td>
        <td class="right nowrap small muted">${t('of')} ${money(x.reimb_outstanding, x.currency)}</td><td style="width:130px"><input type="number" step="0.01" data-amt="${x.id}" value="${x.reimb_outstanding}" max="${x.reimb_outstanding}" style="text-align:right" aria-label="Amount"></td><td class="small">${x.currency}</td></tr>`).join('')}</tbody></table>
      ${curs.length > 1 ? '<div class="small muted">Several currencies: the default amount received is converted with Settings → Currencies rates; enter the real amount if it differs.</div>' : ''}
      <div class="small muted">${t('Received more than this? The rest stays unallocated and can be linked to other expenses later.')}</div></div>
  </form>`, {wide: true, buttons: '<button class="btn" data-x2>Cancel</button><button class="btn pri" data-ok>Save</button>'});
  const f = $('#rbF', bg);
  const cur = () => { const a = acctById(f.account_id.value); $('#rbCur', bg).textContent = a ? '(' + a.currency + ')' : ''; };
  f.account_id.onchange = cur; cur();
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const items = $$('[data-amt]', bg).map(i => ({expense_id: +i.dataset.amt, amount: i.value === '' ? null : +i.value})).filter(x => x.amount === null || x.amount > 0);
    const r = await guard(() => api('/api/reimbursements/receipts', {method: 'POST', body: {account_id: +f.account_id.value, date: f.date.value, amount: f.amount.value || null, payer: f.payer.value || null, note: f.note.value || null, items}}));
    bg.close(); toast(t('Recorded {amt} · {c}/{n} closed', {amt: money(r.receipt.amount, r.receipt.currency), c: r.settled.filter(x => x.reimb_state === 'closed').length, n: r.settled.length})); await loadMeta(); route();
  };
}

async function flagDialog() {
  const d = await guard(() => api('/api/transactions?kind=expense&limit=100'));
  const rows = d.items.filter(x => !x.reimbursable);
  const bg = modal(t('Flag expenses as reimbursable'), `<div class="row" style="margin-bottom:8px"><label class="f" style="flex:1"><span>Reimburse from (optional)</span><input id="flPayer" list="dlPayer"></label></div>
    ${rows.length ? `<div class="scroll-x" style="max-height:50vh;overflow:auto"><table class="t"><tbody>${rows.map(x => `<tr><td><input type="checkbox" data-fl="${x.id}" aria-label="Select"></td><td class="small nowrap">${esc(x.date.slice(0, 10))}</td><td>${txDesc(x)}</td><td class="right nowrap">${money(x.amount, x.currency)}</td></tr>`).join('')}</tbody></table></div>` : '<div class="empty">No recent expenses</div>'}
    <div class="small muted" style="margin-top:6px">${t('The 100 most recent expenses. Older ones: open the transaction and tick Reimbursable.')}</div>`,
    {wide: true, buttons: '<button class="btn" data-x2>Cancel</button><button class="btn pri" data-ok>Flag selected</button>'});
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const ids = $$('[data-fl]:checked', bg).map(c => +c.dataset.fl); if (!ids.length) return;
    const r = await guard(() => api('/api/reimbursements/mark', {method: 'POST', body: {ids, reimbursable: true, payer: $('#flPayer', bg).value || null}}));
    bg.close(); toast(t('Flagged {n} as reimbursable', {n: r.updated})); route();
  };
}
