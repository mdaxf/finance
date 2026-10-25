/* ---------- Reserves 押金/预授权 (#/reserves) ----------
   Card holds (memo only: never a transaction, the card shows "On hold" and less available credit) and refundable
   deposits (a transfer into the system "Deposits 押金" account). Check out in one step with a preview (dry run):
   final bill / release for a hold; refund, applied to the bill, kept by the merchant and extra charges for a deposit. */
S.rs = S.rs || {states: ['open', 'partial'], method: ''};
const RS_STATE = {open: 'Open', partial: 'Partly settled', settled: 'Settled up', released: 'Released', cancelled: 'Cancelled'};
const RS_METHOD = {hold: 'Card hold', deposit: 'Deposit'};
const RS_ROLE = {deposit: ['➡️', 'Deposit paid'], charge: ['🧾', 'Charge'], refund: ['↩️', 'Refund received'], apply: ['🧾', 'Applied to the bill'],
  forfeit: ['✂️', 'Kept by the merchant'], fx: ['⚖️', 'FX adjustment']};
const rsBadge = x => `<span class="rs-st rs-${esc(x.state)}">${t(RS_STATE[x.state] || x.state)}</span>`;
const rsMethod = x => `<span class="pill rs-m rs-m-${esc(x.method)}">${x.method === 'hold' ? '🔒' : '💰'} ${t(RS_METHOD[x.method])}</span>`;
const rsTotals = o => o.by_currency.length ? o.by_currency.map(c => money(c.amount, c.currency)).join('<br>') + (o.by_currency.length > 1 ? `<div class="small muted">≈ ${money(o.total_base, o.base_currency)}</div>` : '') : money(0);
function rsDue(x) {
  if (x.closed) return '';
  if (!x.remind) return `<div class="small muted">🔕 ${t('No reminder')}</div>`;
  if (x.overdue_days > 0) return `<div class="small neg">⏰ ${esc(t('{n} days past the reminder date', {n: x.overdue_days}))}</div>`;
  return `<div class="small muted">⏰ ${esc(t('Reminder {date}', {date: x.release_due}))}</div>`;
}
const rsOpenAmt = x => x.method === 'hold' ? x.on_hold : x.outstanding;

VIEWS.reserves = async (v) => {
  const st = S.rs;
  const q = new URLSearchParams({state: st.states.length ? st.states.join(',') : 'none'});
  if (st.method) q.set('method', st.method);
  const [d, rv] = await Promise.all([api('/api/reserves?' + q), api('/api/reserves?review=1&state=all')]);
  const set = S.meta.settings;
  const sc = d.state_counts || {};
  v.innerHTML = `${demoBanner()}<div class="card"><div class="page-h"><h2>🔒 ${t('Reserves')}</h2><div class="row"><a class="btn sm" href="/api/reserves/export.csv">⬇ ${t('Export CSV')}</a><button class="btn pri" id="rsNew">＋ ${t('New reserve')}</button></div></div>
    <p class="small muted">${t('Hotel and rental deposits and card holds. A card hold is a note only: it never changes a balance or your spending. A deposit moves money to the Deposits account until it comes back.')}</p>
    <div class="rb-cards">
      <div class="ic"><div class="lbl">${t('On hold')}</div><div class="val">${rsTotals(d.on_hold)}</div><div class="small muted">${t('Card holds not released yet')}</div></div>
      <div class="ic"><div class="lbl">${t('Deposits outstanding')}</div><div class="val">${rsTotals(d.deposits)}</div><div class="small muted">${t('Money to get back')}</div></div>
      <div class="ic"><div class="lbl">${t('Overdue')}</div><div class="val">${d.items.filter(x => !x.closed && x.overdue_days > 0).length}</div><div class="small muted">${t('Past the reminder date')}</div></div>
    </div>
    ${rv.items.length ? `<div class="rs-review"><h3>📥 ${t('Holds found in emails')} (${rv.items.length})</h3>${rv.items.map(x => rsReviewRow(x)).join('')}</div>` : ''}
    <form class="rb-flt" id="rsFlt" autocomplete="off">
      <span class="chips" role="group" aria-label="${t('Status')}">${Object.entries(RS_STATE).map(([k, l]) => `<button type="button" class="chip${st.states.includes(k) ? ' on' : ''}" data-st="${k}" aria-pressed="${st.states.includes(k)}">${t(l)} <span class="cnt">${sc[k] || 0}</span></button>`).join('')}</span>
      <select name="method"><option value="">${t('Holds and deposits')}</option>${Object.entries(RS_METHOD).map(([k, l]) => `<option value="${k}" ${st.method === k ? 'selected' : ''}>${t(l)}</option>`).join('')}</select></form>
    ${d.items.filter(x => !x.needs_review).length ? `<div class="scroll-x"><table class="t rs-tbl"><thead><tr><th>${t('Stay')}</th><th>${t('Reserve')}</th><th class="right">${t('Amount')}</th><th class="right">${t('Open amount')}</th><th>${t('Status')}</th><th></th></tr></thead><tbody>
      ${d.items.filter(x => !x.needs_review).map(x => `<tr data-rid="${x.id}"><td class="nowrap small">${esc(x.start_date)}${x.end_date ? ` → ${esc(x.end_date)}` : ''}</td>
        <td class="rs-open"><div>${isDemoName(x.title) ? '<span class="pill demo">DEMO</span> ' : ''}${rsMethod(x)} <b>${esc(x.label)}</b></div><div class="small muted">${esc(x.account_name || '')}${x.ref ? ' · #' + esc(x.ref) : ''}${x.link_count ? ' · ' + esc(t('{n} linked', {n: x.link_count})) : ''}</div></td>
        <td class="right nowrap">${money(x.amount, x.currency)}${x.over_hold > 0.005 ? `<div class="small warn-t">${esc(t('Over hold {amt}', {amt: money(x.over_hold, x.currency)}))}</div>` : ''}</td>
        <td class="right nowrap"><b class="${rsOpenAmt(x) > 0.005 ? 'warn-t' : 'muted'}">${money(rsOpenAmt(x), x.currency)}</b></td>
        <td>${rsBadge(x)}${rsDue(x)}</td>
        <td class="rb-act nowrap">${x.closed ? '' : `<button class="btn sm pri" data-co="${x.id}">🧳 ${t('Check out')}</button>`} <button class="btn sm" data-op="${x.id}">${t('Details')}</button></td></tr>`).join('')}</tbody></table></div>`
      : `<div class="empty">${st.states.join() === 'open,partial' ? t('No open reserves. Add one when a hotel or rental company holds money on your card or takes a deposit.') : t('No matching reserves')}</div>`}
    <div class="small muted rs-set">${t('Remind me')} <input id="rsRel" type="number" min="0" max="365" value="${esc(set.reserve_release_days || '7')}"> ${t('days after check-out if a hold is not released or a deposit not refunded')} · ${t('without a check-out date')} <input id="rsHold" type="number" min="0" max="365" value="${esc(set.reserve_hold_days || '30')}"> ${t('days after check-in')} <button class="btn sm" id="rsSave">${t('Save')}</button>
      <div>${t('Reminders show in Reminders and go out by email and Telegram like the others.')}</div></div></div>`;
  const f = $('#rsFlt', v);
  f.onchange = () => { st.method = f.method.value; route(); };
  $$('[data-st]', f).forEach(b => b.onclick = () => { const k = b.dataset.st; st.states = st.states.includes(k) ? st.states.filter(x => x !== k) : [...st.states, k]; route(); });
  $('#rsNew', v).onclick = () => reserveForm({after: route});
  $$('[data-co]', v).forEach(b => b.onclick = () => reserveCheckout(+b.dataset.co, route));
  $$('[data-op]', v).forEach(b => b.onclick = () => openReserve(+b.dataset.op, route));
  $$('td.rs-open', v).forEach(td => { td.style.cursor = 'pointer'; td.onclick = () => openReserve(+td.parentElement.dataset.rid, route); });
  rsBindReview(v, async () => { await loadMeta(); route(); });
  $('#rsSave', v).onclick = async () => { await guard(() => api('/api/settings', {method: 'PUT', body: {reserve_release_days: $('#rsRel').value, reserve_hold_days: $('#rsHold').value}})); await loadMeta(); toast(t('Saved')); route(); };
};
/* a hold detected from an email: Confirm (it's a hold) or It's a real charge (becomes an expense to review) */
function rsReviewRow(x) {
  return `<div class="rs-rv-row" data-rvid="${x.id}"><span>🔒 <b>${esc(x.label)}</b> ${money(x.amount, x.currency)} <span class="small muted">${esc(x.account_name || '')} · ${esc(x.start_date)}${x.note ? ' · ' + esc(x.note.slice(0, 80)) : ''}</span></span>
    <span class="sp"></span><button class="btn sm pri" data-rvok="${x.id}">✓ ${t('Confirm hold')}</button> <button class="btn sm" data-rvtx="${x.id}" title="${t('Not a hold: it becomes an expense in the review queue')}">🧾 ${t('It\'s a real charge')}</button> <button class="btn sm" data-rved="${x.id}">✎</button> <button class="btn sm danger" data-rvdel="${x.id}">✕</button></div>`;
}
function rsBindReview(root, after) {
  $$('[data-rvok]', root).forEach(b => b.onclick = async () => { await guard(() => api(`/api/reserves/${b.dataset.rvok}/confirm`, {method: 'POST'})); toast(t('Hold confirmed')); after(); });
  $$('[data-rvtx]', root).forEach(b => b.onclick = async () => { await guard(() => api(`/api/reserves/${b.dataset.rvtx}/to-transaction`, {method: 'POST'})); toast(t('Moved to the review queue as an expense')); after(); });
  $$('[data-rved]', root).forEach(b => b.onclick = () => openReserve(+b.dataset.rved, after));
  $$('[data-rvdel]', root).forEach(b => b.onclick = async () => { if (!await confirmBox(t('Delete this hold?'))) return; await guard(() => api(`/api/reserves/${b.dataset.rvdel}`, {method: 'DELETE'})); after(); });
}

/* detail: amounts + timeline + actions */
function rsTimeline(x) {
  const ev = [];
  ev.push({d: x.start_date, k: 'in', html: `🛎 <b>${t('Check-in')}</b>${x.method === 'hold' ? ' · ' + esc(t('card hold placed {amt}', {amt: money(x.amount, x.currency)})) : ''}`});
  if (x.end_date) ev.push({d: x.end_date, k: 'out', html: `🧳 <b>${t('Check-out')}</b>`});
  for (const l of x.links) {
    const [ic, lb] = RS_ROLE[l.role] || ['•', l.role];
    const lbl = l.role === 'charge' && l.final ? t('Final bill') : t(lb);
    const other = l.tx_currency !== x.currency ? ` <span class="muted">(${money(l.amount, l.tx_currency)})</span>` : '';
    const acc = l.tx_kind === 'transfer' ? `${esc(l.tx_account_name || '')} → ${esc(l.tx_to_account_name || '')}` : esc(l.tx_account_name || '');
    ev.push({d: (l.tx_date || '').slice(0, 10), k: 'l', html: `${ic} <b>${esc(lbl)}</b> ${money(l.alloc_amount, x.currency)}${other} <span class="small muted">${acc}${l.tx_note ? ' · ' + esc(l.tx_note.slice(0, 60)) : ''}</span>
      <button type="button" class="lnk" data-otx="${l.transaction_id}" title="${t('Open the transaction')}">↗</button>${l.role === 'fx' ? '' : `<button type="button" class="lnk danger" data-lun="${l.id}" title="${t('Unlink')}" aria-label="${t('Unlink')}">✕</button>`}`});
  }
  if (!x.closed && x.remind) ev.push({d: x.release_due, k: 'due', html: `⏰ <b>${t('Reminder')}</b> <span class="small ${x.overdue_days > 0 ? 'neg' : 'muted'}">${x.overdue_days > 0 ? esc(t('{n} days past the reminder date', {n: x.overdue_days})) : t('if still open')}</span>`});
  else if (x.closed_at || x.state) {
    const end = x.method === 'hold' && x.released > 0.005 ? ' · ' + esc(t('{amt} released', {amt: money(x.released, x.currency)})) : '';
    const lastLink = x.links.map(l => (l.tx_date || '').slice(0, 10)).sort().pop();
    ev.push({d: (x.closed_at || '').slice(0, 10) || lastLink || x.end_date || x.start_date, k: 'closed', html: `✓ <b>${t(RS_STATE[x.state])}</b>${end}`});
  }
  const order = {in: 0, l: 1, out: 2, due: 3, closed: 4};
  ev.sort((a, b) => (a.d || '').localeCompare(b.d || '') || order[a.k] - order[b.k]);
  return `<ol class="rs-tl">${ev.map(e => `<li class="rs-tl-${e.k}"><span class="rs-tl-d small muted">${esc(e.d || '')}</span><span class="rs-tl-b">${e.html}</span></li>`).join('')}</ol>`;
}
function rsAmounts(x) {
  const c = x.currency, m = v => money(v, c);
  const rows = x.method === 'hold'
    ? [['Hold', m(x.amount)], ['Captured', m(x.captured)], ...(x.over_hold > 0.005 ? [['Over hold', `<span class="warn-t">${m(x.over_hold)}</span>`]] : []), [x.closed ? 'Released' : 'On hold', `<b>${m(x.closed ? x.released : x.on_hold)}</b>`]]
    : [['Deposited', m(x.deposited)], ['Refunded', m(x.refunded)], ['Applied to the bill', m(x.applied)], ['Kept by the merchant', m(x.forfeited)], ...(Math.abs(x.fx_adjusted) > 0.005 ? [['FX adjustment', m(x.fx_adjusted)]] : []), ...(x.charges > 0.005 ? [['Extra charges', m(x.charges)]] : []), ['Outstanding', `<b>${m(x.outstanding)}</b>`]];
  return `<div class="rs-amts">${rows.map(([l, v]) => `<div><div class="lbl small muted">${t(l)}</div><div>${v}</div></div>`).join('')}</div>`;
}
async function openReserve(id, after) {
  const x = await guard(() => api('/api/reserves/' + id));
  const done = () => { bg.close(); if (after) after(); };
  const bg = modal(`${x.method === 'hold' ? '🔒' : '💰'} ${esc(x.label)}`, `<div class="rs-head">${rsMethod(x)} ${rsBadge(x)} <span class="small muted">${esc(x.account_name || '')}${x.deposit_account_name ? ' → ' + esc(x.deposit_account_name) : ''}${x.ref ? ' · #' + esc(x.ref) : ''}${x.project_name ? ' · 📁 ' + esc(x.project_name) : ''}</span></div>
    ${x.needs_review ? `<div class="demo-banner">📥 ${t('Found in an email: confirm it is a hold, or turn it into a charge.')}</div>` : ''}
    ${rsAmounts(x)}
    <h3>${t('Timeline')}</h3>${rsTimeline(x)}
    ${x.note ? `<div class="small muted">📝 ${esc(x.note)}</div>` : ''}
    ${x.method === 'hold' ? `<div class="small muted">${t('A card hold is a note only: the card balance and your spending never include it. Link the final bill when it posts.')}</div>` : `<div class="small muted">${t('The deposit is a transfer to the Deposits account: your net worth does not change and it is not spending. Only the part kept or applied to the bill becomes an expense.')}</div>`}`,
    {wide: true, buttons: `${x.needs_review ? `<button class="btn pri" data-rvok="${x.id}">✓ ${t('Confirm hold')}</button><button class="btn" data-rvtx="${x.id}">🧾 ${t('It\'s a real charge')}</button>` : ''}
      ${x.closed ? `<button class="btn" data-a="reopen">${t('Reopen')}</button>` : `<button class="btn pri" data-a="checkout">🧳 ${t('Check out')}</button><button class="btn" data-a="link">🔗 ${t('Link transaction')}</button>${x.method === 'hold' ? `<button class="btn" data-a="release">${t('Release')}</button>${x.captured > 0.005 ? '' : `<button class="btn" data-a="cancel">${t('Booking cancelled')}</button>`}` : ''}`}
      <button class="btn" data-a="edit">✎ ${t('Edit')}</button><button class="btn danger" data-a="delete">${t('Delete')}</button>`});
  rsBindReview(bg, done);
  $$('[data-otx]', bg).forEach(b => b.onclick = () => openTx(+b.dataset.otx, () => { bg.close(); openReserve(id, after); }));
  $$('[data-lun]', bg).forEach(b => b.onclick = async () => { if (!await confirmBox(t('Unlink this transaction? It stays in your transactions.'))) return; await guard(() => api('/api/reserves/links/' + b.dataset.lun, {method: 'DELETE'})); bg.close(); openReserve(id, after); });
  const act = {
    checkout: () => { bg.close(); reserveCheckout(id, after); },
    link: () => { bg.close(); rsLinkDialog(id, () => openReserve(id, after)); },
    edit: () => { bg.close(); reserveForm({id, after}); },
    release: async () => { if (!await confirmBox(t('Release the hold? Nothing is recorded; the rest of the hold is freed on the card.'))) return; await guard(() => api(`/api/reserves/${id}/release`, {method: 'POST'})); toast(t('Released')); done(); },
    cancel: async () => { await guard(() => api(`/api/reserves/${id}/cancel`, {method: 'POST'})); toast(t('Cancelled')); done(); },
    reopen: async () => { await guard(() => api(`/api/reserves/${id}/reopen`, {method: 'POST'})); toast(t('Reopened')); bg.close(); openReserve(id, after); },
    delete: async () => {
      const n = x.link_count;
      if (!await confirmBox(n ? t('Delete this reserve? Its {n} linked transaction(s) stay in your transactions, only the links go.', {n}) : t('Delete this reserve?'))) return;
      await guard(() => api(`/api/reserves/${id}${n ? '?unlink=1' : ''}`, {method: 'DELETE'})); toast(t('Deleted')); done();
    },
  };
  $$('[data-a]', bg).forEach(b => b.onclick = act[b.dataset.a]);
}

/* new / edit; opts: {id, preset, fromReview: review item id, fromTx: posted expense id (converted in place), after} */
async function reserveForm(opts = {}) {
  await (S.meta || loadMeta());
  const m = S.meta;
  const x = opts.id ? await guard(() => api('/api/reserves/' + opts.id)) : Object.assign({method: 'hold', start_date: today(), remind: 1}, opts.preset || {});
  const edit = !!opts.id, locked = edit && x.method === 'deposit';
  const accts = a => !a.archived && a.system_role !== 'deposits' || a.id == x.account_id;
  const bg = modal(edit ? `✎ ${esc(x.label || t('Reserve'))}` : opts.fromReview || opts.fromTx ? `🔒 ${t('Convert to a reserve')}` : `🔒 ${t('New reserve')}`, `<form class="form" id="rsF" autocomplete="off">
    <div class="full kind-tabs rs-kinds">${Object.entries(RS_METHOD).map(([k, l]) => `<button type="button" data-m="${k}" class="${x.method === k ? 'on' : ''}" ${edit ? 'disabled' : ''}>${k === 'hold' ? '🔒' : '💰'} ${t(l)}</button>`).join('')}</div>
    <div class="full small muted" data-for="hold">${t('A hold (pre-authorization) only reserves part of your credit: no transaction is recorded. Check out with the final bill.')}</div>
    <div class="full small muted" data-for="deposit">${t('A deposit is money that left your account and should come back: it is recorded as a transfer to the Deposits account, not as spending.')}</div>
    <label class="f"><span>${t('Card or account')}</span><select name="account_id" ${locked ? 'disabled' : ''}>${acctOptions(x.account_id, {filter: accts})}</select></label>
    <label class="f"><span>${t('Amount held or deposited')}</span><input name="amount" type="number" step="0.01" min="0.01" inputmode="decimal" value="${x.amount ?? ''}" ${locked ? 'readonly' : ''}></label>
    <label class="f"><span>${t('Currency')}</span><select name="currency" ${locked ? 'disabled' : ''}>${curOptions(x.currency || (acctById(x.account_id) || {}).currency || m.settings.base_currency)}</select></label>
    <label class="f" data-for="deposit" id="rsPaid" hidden><span>${t('Amount charged to the account')} <small class="muted">${t('(optional, its currency)')}</small></span><input name="paid_amount" type="number" step="0.01" inputmode="decimal" ${locked ? 'disabled' : ''}></label>
    <label class="f"><span>${t('Hotel / merchant')}</span><input name="merchant" list="dlMerch" value="${esc(x.merchant_name || x.merchant || '')}"></label>
    <label class="f"><span>${t('Title')}</span><input name="title" value="${esc(x.title || '')}" placeholder="${t('e.g. Hilton Chicago, 3 nights')}"></label>
    <label class="f"><span>${t('Check-in')}</span><input name="start_date" type="date" value="${esc(x.start_date || today())}"></label>
    <label class="f"><span>${t('Check-out')} <small class="muted">${t('(optional)')}</small></span><input name="end_date" type="date" value="${esc(x.end_date || '')}"></label>
    <label class="f"><span>${t('Category for the bill')}</span><select name="category_id">${catOptions('expense', x.category_id)}</select></label>
    <label class="f"><span>${t('Project')}</span><select name="project_id">${listOptions(m.projects.filter(p => !p.archived || p.id == x.project_id), x.project_id, '—')}</select></label>
    <label class="f"><span>${t('Confirmation #')}</span><input name="ref" value="${esc(x.ref || '')}"></label>
    <label class="f full"><span>${t('Note')}</span><input name="note" value="${esc(x.note || '')}"></label>
    <label class="chk full"><input type="checkbox" name="remind" ${x.remind ? 'checked' : ''}> ${t('Remind me if it is still open after check-out')}</label>
    ${locked ? `<div class="full small muted">${t('The amount, currency and account come from the deposit transfer: edit that transaction to change them.')}</div>` : ''}
    ${opts.fromTx ? `<div class="full small muted" data-for="hold">${t('The expense is removed: a hold is not spending.')}</div><div class="full small muted" data-for="deposit">${t('The expense becomes the transfer to the Deposits account (same transaction, receipts kept).')}</div>` : ''}</form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${edit ? t('Save') : t('Create')}</button>`});
  const f = $('#rsF', bg); let method = x.method;
  const sync = () => {
    $$('[data-m]', bg).forEach(b => b.classList.toggle('on', b.dataset.m === method));
    $$('[data-for]', bg).forEach(el => el.hidden = el.dataset.for !== method);
    const a = acctById(f.account_id.value);
    $('#rsPaid', bg).hidden = method !== 'deposit' || !a || a.currency === f.currency.value;
  };
  $$('[data-m]', bg).forEach(b => b.onclick = () => { method = b.dataset.m; sync(); });
  f.account_id.onchange = () => { const a = acctById(f.account_id.value); if (a && !edit) f.currency.value = a.currency; sync(); };
  f.currency.onchange = sync;
  sync();
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const body = {title: f.title.value, merchant: f.merchant.value, start_date: f.start_date.value, end_date: f.end_date.value,
      category_id: f.category_id.value || null, project_id: f.project_id.value || null, ref: f.ref.value, note: f.note.value, remind: f.remind.checked};
    if (!locked) Object.assign(body, {account_id: +f.account_id.value || null, amount: f.amount.value, currency: f.currency.value});
    if (!edit) { body.method = method; if (!$('#rsPaid', bg).hidden && f.paid_amount.value) body.paid_amount = f.paid_amount.value; }
    const r = await guard(() => opts.fromTx ? api(`/api/transactions/${opts.fromTx}/to-reserve`, {method: 'POST', body})
      : opts.fromReview ? api(`/api/review/${opts.fromReview}/to-reserve`, {method: 'POST', body})
      : edit ? api('/api/reserves/' + opts.id, {method: 'PUT', body}) : api('/api/reserves', {method: 'POST', body}));
    bg.close(); toast(edit ? t('Saved') : method === 'hold' ? t('Card hold saved — no transaction recorded') : t('Deposit saved — moved to the Deposits account'));
    await loadMeta();
    if (opts.after) opts.after(r); else if (!edit) location.hash = '#/reserves';
  };
}

/* check-out: one step, previewed with dry_run */
async function reserveCheckout(id, after) {
  const x = await guard(() => api('/api/reserves/' + id));
  const sug = x.method === 'hold' ? await api('/api/reserves/suggest?reserve_id=' + id).catch(() => ({items: []})) : {items: []};
  const charges = sug.items.filter(s => s.kind === 'expense');
  const hold = x.method === 'hold';
  const cur = x.currency;
  const body = hold ? `<div class="full small muted">${esc(t('Hold {amt} on {account}. Enter the final bill: if it is lower, the rest is released; if higher, the difference shows as over hold.', {amt: money(x.on_hold, cur), account: x.account_name || ''}))}</div>
      <div class="full chips rs-co-mode">${[['bill', t('Final bill')], ...(charges.length ? [['link', t('Link the posted charge')]] : []), ['release', t('Released with no charge')]].map(([k, l], i) => `<label class="chip"><input type="radio" name="mode" value="${k}" ${i === 0 ? 'checked' : ''}> ${l}</label>`).join('')}</div>
      <label class="f" data-mode="bill"><span>${t('Final bill amount')} (${esc(cur)})</span><input name="bill" type="number" step="0.01" min="0.01" inputmode="decimal" value="${x.amount}"></label>
      <label class="f" data-mode="bill"><span>${t('Paid with')}</span><select name="bill_acct">${acctOptions(x.account_id, {filter: a => !a.archived && a.system_role !== 'deposits'})}</select></label>
      <label class="f" data-mode="bill"><span>${t('Category')}</span><select name="bill_cat">${catOptions('expense', x.category_id)}</select></label>
      ${charges.length ? `<label class="f full" data-mode="link" hidden><span>${t('Charge')}</span><select name="tx">${charges.map(s => `<option value="${s.id}">${esc(s.date.slice(0, 10))} · ${money(s.amount, s.currency)} · ${esc(s.merchant_name || s.note || s.account_name || '')}</option>`).join('')}</select></label>` : ''}
      <label class="f"><span>${t('Date')}</span><input name="date" type="date" value="${esc(x.end_date && x.end_date <= today() ? x.end_date : today())}"></label>`
    : `<div class="full small muted">${esc(t('{amt} outstanding. Split it into what came back, what was applied to the bill and what the merchant kept.', {amt: money(x.outstanding, cur)}))}</div>
      <fieldset class="full rs-co-box"><legend>↩️ ${t('Refund received')}</legend>
        <label class="f"><span>${t('Into account')}</span><select name="rf_acct">${acctOptions(x.account_id, {filter: a => !a.archived && a.id !== x.deposit_account_id})}</select></label>
        <label class="f"><span>${t('Deposit returned')} (${esc(cur)}) <small class="muted">${t('(blank = the rest)')}</small></span><input name="rf_amt" type="number" step="0.01" min="0" inputmode="decimal"></label>
        <label class="f" id="rsRecv" hidden><span>${t('Amount that arrived')} <small class="muted" id="rsRecvCur"></small></span><input name="rf_recv" type="number" step="0.01" min="0" inputmode="decimal" placeholder="${t('optional: a different rate posts an FX adjustment')}"></label>
        <label class="chk"><input type="checkbox" name="rf_on" checked> ${t('Record the refund')}</label></fieldset>
      <fieldset class="full rs-co-box"><legend>🧾 ${t('Applied to the bill')}</legend><label class="f"><span>${t('Amount')} (${esc(cur)})</span><input name="ap_amt" type="number" step="0.01" min="0" inputmode="decimal"></label></fieldset>
      <fieldset class="full rs-co-box"><legend>✂️ ${t('Kept by the merchant')}</legend><label class="f"><span>${t('Amount')} (${esc(cur)})</span><input name="fo_amt" type="number" step="0.01" min="0" inputmode="decimal"></label><label class="f"><span>${t('Why it was kept')}</span><input name="fo_note" placeholder="${t('e.g. damage, cleaning fee')}"></label></fieldset>
      <fieldset class="full rs-co-box"><legend>➕ ${t('Extra charge on the card')}</legend><label class="f"><span>${t('Amount')}</span><input name="ch_amt" type="number" step="0.01" min="0" inputmode="decimal"></label><label class="f"><span>${t('Paid with')}</span><select name="ch_acct">${acctOptions(x.account_id, {filter: a => !a.archived && a.id !== x.deposit_account_id})}</select></label></fieldset>
      <label class="f"><span>${t('Category')}</span><select name="cat">${catOptions('expense', x.category_id)}</select></label>
      <label class="f"><span>${t('Date')}</span><input name="date" type="date" value="${esc(today())}"></label>`;
  const bg = modal(`🧳 ${t('Check out')} · ${esc(x.label)}`, `<form class="form" id="rsCo" autocomplete="off">${body}</form><div class="rs-prev" id="rsPrev"></div>`,
    {wide: true, buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>✓ ${t('Check out')}</button>`});
  const f = $('#rsCo', bg);
  const payload = () => {
    const b = {date: f.date.value};
    if (hold) {
      const mode = (f.querySelector('[name=mode]:checked') || {}).value;
      if (mode === 'release') b.release = true;
      else if (mode === 'link') b.final = {transaction_id: +f.tx.value};
      else b.final = {amount: f.bill.value, account_id: +f.bill_acct.value, category_id: f.bill_cat.value || null, date: f.date.value};
      return b;
    }
    const cat = f.cat.value || null;
    if (f.rf_on.checked) { b.refund = {account_id: +f.rf_acct.value, date: f.date.value}; if (f.rf_amt.value) b.refund.amount = f.rf_amt.value; if (!$('#rsRecv', bg).hidden && f.rf_recv.value) b.refund.received = f.rf_recv.value; }
    if (+f.ap_amt.value) b.apply = {amount: f.ap_amt.value, category_id: cat};
    if (+f.fo_amt.value) b.forfeit = {amount: f.fo_amt.value, category_id: cat, note: f.fo_note.value || undefined};
    if (+f.ch_amt.value) b.charge = {amount: f.ch_amt.value, account_id: +f.ch_acct.value, category_id: cat};
    return b;
  };
  const planLine = p => {
    const a = acctById(p.account_id);
    if (p.action === 'release') return `✓ ${t('The hold is released: nothing is recorded')}`;
    if (p.action === 'link') return `🔗 ${t('Link the posted charge')}${p.final ? ' · ' + t('Final bill') : ''}`;
    const [ic, lb] = RS_ROLE[p.role] || ['•', p.role];
    const what = p.role === 'charge' ? (p.final ? t('Final bill') : t('Extra charge')) : t(lb);
    const kind = p.kind === 'transfer' ? t('transfer') : t('expense');
    return `${ic} ${esc(what)} <b>${money(p.amount, p.role === 'charge' && a ? a.currency : cur)}</b> <span class="muted">${esc(kind)}${a ? ' · ' + esc(a.name) : ''}${p.received != null && a && a.currency !== cur ? ' · ' + esc(t('arrives as {amt}', {amt: money(p.received, a.currency)})) : ''}</span>`;
  };
  let h;
  const preview = () => { clearTimeout(h); h = setTimeout(async () => {
    const el = $('#rsPrev', bg); if (!el) return;
    try {
      const r = await api(`/api/reserves/${id}/settle`, {method: 'POST', body: {...payload(), dry_run: true}});
      const y = r.result;
      const tail = hold ? (y.over_hold > 0.005 ? t('Over hold by {amt}', {amt: money(y.over_hold, cur)}) : y.released > 0.005 ? t('{amt} released', {amt: money(y.released, cur)}) : '')
        : y.fx_adjusted && Math.abs(y.fx_adjusted) > 0.005 ? t('FX adjustment {amt} (not income or spending)', {amt: money(y.fx_adjusted, cur)}) : '';
      el.innerHTML = `<h3>${t('Preview')}</h3><ul class="rs-plan">${r.plan.map(p => `<li>${planLine(p)}</li>`).join('')}</ul>
        <div>${t('After check-out')}: ${rsBadge(y)} ${!hold ? esc(t('{amt} outstanding', {amt: money(y.outstanding, cur)})) : ''} ${tail ? `<span class="small muted">· ${esc(tail)}</span>` : ''}</div>
        <div class="small muted">${t('Nothing is saved until you press Check out.')}</div>`;
      $('[data-ok]', bg).disabled = false;
    } catch (e) { el.innerHTML = `<div class="neg">⚠ ${esc(errText(e))}</div>`; $('[data-ok]', bg).disabled = true; }
  }, 250); };
  const syncMode = () => {
    if (hold) { const mode = (f.querySelector('[name=mode]:checked') || {}).value; $$('[data-mode]', f).forEach(el => el.hidden = el.dataset.mode !== mode); }
    else { const a = acctById(f.rf_acct.value); $('#rsRecv', bg).hidden = !a || a.currency === cur; $('#rsRecvCur', bg).textContent = a ? '(' + a.currency + ')' : ''; }
  };
  f.addEventListener('input', () => { syncMode(); preview(); });
  f.addEventListener('change', () => { syncMode(); preview(); });
  syncMode(); preview();
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const r = await guard(() => api(`/api/reserves/${id}/settle`, {method: 'POST', body: payload()}));
    bg.close(); toast(r.reserve.closed ? t('Checked out') : t('Saved — part of the deposit is still outstanding'));
    await loadMeta(); if (after) after(); else route();
  };
}

/* link existing transactions (suggestions: same merchant / card / Deposits account around the stay) */
async function rsLinkDialog(id, after) {
  const d = await guard(() => api('/api/reserves/suggest?reserve_id=' + id));
  const x = d.reserve;
  const roleOpts = sel => (x.method === 'hold' ? ['charge'] : ['charge', 'deposit', 'refund', 'apply', 'forfeit']).map(r => `<option value="${r}" ${r === sel ? 'selected' : ''}>${t(RS_ROLE[r][1])}</option>`).join('');
  const bg = modal(`🔗 ${t('Link transaction')} · ${esc(x.label)}`, d.items.length ? `<div class="scroll-x"><table class="t"><thead><tr><th></th><th>${t('Date')}</th><th>${t('Transaction')}</th><th class="right">${t('Amount')}</th><th>${t('As')}</th>${x.method === 'hold' ? `<th>${t('Final bill')}</th>` : ''}</tr></thead><tbody>
    ${d.items.map(s => `<tr><td><input type="checkbox" data-pick="${s.id}" aria-label="${t('Select')}"></td><td class="small nowrap">${esc(s.date.slice(0, 10))}</td><td>${txDesc(s)}</td><td class="right nowrap">${txAmount(s)}</td>
      <td><select data-role="${s.id}">${roleOpts(s.role)}</select></td>${x.method === 'hold' ? `<td><input type="checkbox" data-fin="${s.id}" aria-label="${t('Final bill')}"></td>` : ''}</tr>`).join('')}</tbody></table></div>
    <div class="small muted">${t('Transactions with the same merchant, card or Deposits account around the stay. A refund to your card is turned into a transfer from the Deposits account, so it is not counted as income.')}</div>`
    : `<div class="empty">${t('No matching transactions yet. Record the bill or refund first, then link it here (or use Check out).')}</div>`,
    {wide: true, buttons: `<button class="btn" data-x2>${t('Cancel')}</button>${d.items.length ? `<button class="btn pri" data-ok>🔗 ${t('Link')}</button>` : ''}`});
  $('[data-x2]', bg).onclick = bg.close;
  const ok = $('[data-ok]', bg);
  if (ok) ok.onclick = async () => {
    const items = $$('[data-pick]', bg).filter(c => c.checked).map(c => { const i = c.dataset.pick; const fin = $(`[data-fin="${i}"]`, bg); return {transaction_id: +i, role: $(`[data-role="${i}"]`, bg).value, final: fin ? fin.checked : false}; });
    if (!items.length) { toast(t('Select at least one transaction'), true); return; }
    await guard(() => api(`/api/reserves/${id}/links`, {method: 'POST', body: {items}})); bg.close(); toast(t('Linked')); if (after) after();
  };
}

/* transaction dialog: the reserves a transaction belongs to, and "link to a reserve" */
async function rsTxBox(box, tid, after) {
  const [tx, sg] = await Promise.all([api('/api/transactions/' + tid), api('/api/reserves/suggest?transaction_id=' + tid).catch(() => ({items: []}))]);
  const mine = tx.reserves || [];
  const canConvert = tx.status === 'posted' && tx.kind === 'expense' && !mine.length;  // e.g. a hotel deposit auto-posted from an email
  if (!mine.length && !sg.items.length && !canConvert) return;
  box.hidden = false;
  box.innerHTML = `<div class="small"><b>🔒 ${t('Reserves')}</b> ${mine.map(r => `<a href="javascript:void 0" class="pill rs-m" data-ors="${r.id}">${esc(r.title || '#' + r.id)} · ${t(RS_ROLE[r.role] ? RS_ROLE[r.role][1] : r.role)}</a>`).join(' ')}
    ${canConvert ? `<button type="button" class="btn sm" data-rscv title="${t('Hotel / rental deposit or card hold')}">🔒 ${t('Convert to a reserve')}</button>` : ''}
    ${sg.items.length ? `<select id="rsPick" class="rs-pick"><option value="">${t('Link to a reserve…')}</option>${sg.items.map(r => `<option value="${r.id}" data-role="${r.role}">${esc(r.label)} · ${money(rsOpenAmt(r), r.currency)}</option>`).join('')}</select>` : ''}</div>`;
  $$('[data-ors]', box).forEach(a => a.onclick = () => openReserve(+a.dataset.ors, after));
  const cv = $('[data-rscv]', box);
  if (cv) cv.onclick = () => reserveForm({fromTx: tid, after, preset: {account_id: tx.account_id, amount: tx.orig_currency ? tx.orig_amount : tx.amount,
    currency: tx.orig_currency || tx.currency, merchant: tx.merchant_name || '', start_date: (tx.date || '').slice(0, 10), category_id: tx.category_id,
    project_id: tx.project_id, note: tx.note || ''}});
  const p = $('#rsPick', box);
  if (p) p.onchange = async () => { if (!p.value) return; await guard(() => api(`/api/reserves/${p.value}/links`, {method: 'POST', body: {transaction_id: tid}})); toast(t('Linked')); rsTxBox(box, tid, after); };
}
/* list badge */
const txReservePill = x => (x.reserves || []).map(r => `<span class="pill rs-m" title="${t('Linked to a reserve')}">🔒 ${esc((r.title || '').replace(/^\[DEMO\]\s*/, '') || t('Reserve'))}</span> `).join('');
/* reminders page: open the reserve / check out */
function rsReminderActions(r) {
  return `<button class="btn sm pri" data-rsco="${r.reserve_id}">🧳 ${t('Check out')}</button> <button class="btn sm" data-rsop="${r.reserve_id}">${t('Open reserve')}</button>`;
}
function rsBindReminders(root, after) {
  $$('[data-rsco]', root).forEach(b => b.onclick = () => reserveCheckout(+b.dataset.rsco, after));
  $$('[data-rsop]', root).forEach(b => b.onclick = () => openReserve(+b.dataset.rsop, after));
}
