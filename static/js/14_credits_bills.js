/* ---------- Credits 抵用金 (#/credits) and Bills 账单待付 (#/bills) ----------
   Credits: refund / cancellation / bonus credits with an expiry date (a note only: never a transaction). Use it all or
   part of it; a reminder shows N months (setting, default 6) before it expires, and expired credits are flagged.
   Bills: payment tasks created from bill / payment-request emails (or by hand), reminded N days (default 3) before the
   due date; payment-confirmation emails mark them paid, unmatched / ambiguous ones wait under "Payments to review". */
S.cr = S.cr || {states: ['active', 'partial', 'expired']};
const CR_STATE = {active: 'Active', partial: 'Partly used', expired: 'Expired', used: 'Used', void: 'Closed'};
const CR_SOURCE = {refund: 'Refund', cancel: 'Cancellation', bonus: 'Bonus'};
const crBadge = c => `<span class="pill${c.state === 'expired' ? ' cx-bad' : c.expiring_soon ? ' warn' : ''}">${t(CR_STATE[c.state] || c.state)}</span>`;
function crWhen(c) {
  if (c.closed || c.days_left == null) return '';
  if (c.expired) return `<div class="small neg">⚠️ ${esc(t('Expired {n} days ago', {n: -c.days_left}))}</div>`;
  return `<div class="small ${c.expiring_soon ? 'warn-t' : 'muted'}">${esc(c.days_left === 0 ? t('Expires today') : t('{n} days left', {n: c.days_left}))}</div>`;
}

/* Credits page tabs: 🎟 Credits (#/credits) | ✈️ Miles & points (#/credits/rewards), each with its attention count */
function crTabs(tab) {
  const m = S.meta || {};
  const n = {credits: (m.credit_expiring_count || 0) + (m.credit_expired_count || 0), rewards: (m.reward_expiring_count || 0) + (m.reward_expired_count || 0)};
  return `<div class="tabs">${[['credits', '🎟 ' + t('Credits'), '#/credits'], ['rewards', '✈️ ' + t('Miles & points'), '#/credits/rewards']].map(([k, l, h]) =>
    `<a href="${h}" class="${k === tab ? 'active' : ''}">${l}${n[k] ? ` <span class="badge">${n[k]}</span>` : ''}</a>`).join('')}</div>`;
}
VIEWS.credits = async (v, rest) => {
  if (rest && rest[0] === 'rewards') return rewardsView(v);
  const st = S.cr;
  const d = await api('/api/credits?state=' + (st.states.length ? st.states.join(',') : 'none'));
  const sc = d.state_counts || {};
  const set = S.meta.settings;
  v.innerHTML = `${crTabs('credits')}<div class="card"><div class="page-h"><h2>🎟 ${t('Credits')}</h2><div class="row"><button class="btn pri" id="crNew">＋ ${t('New credit')}</button></div></div>
    <p class="small muted">${t('Credits from refunds, cancellations and bonuses that expire (airline and travel credits, store credit, promo bonuses). A credit is a note only: it never changes a balance.')}</p>
    <div class="rb-cards">
      <div class="ic"><div class="lbl">${t('Available')}</div><div class="val">${d.available.length ? d.available.map(x => money(x.amount, x.currency)).join('<br>') : money(0)}</div><div class="small muted">${t('Not expired, not used')}</div></div>
      <div class="ic"><div class="lbl">${t('Expiring soon')}</div><div class="val">${d.expiring_count}</div><div class="small muted">${esc(t('Within {n} months', {n: d.lead_months}))}</div></div>
      <div class="ic"><div class="lbl">${t('Expired')}</div><div class="val ${d.expired_count ? 'neg' : ''}">${d.expired_count}</div><div class="small muted">${t('With money left')}</div></div>
    </div>
    <div class="rb-flt"><span class="chips" role="group">${Object.entries(CR_STATE).map(([k, l]) => `<button type="button" class="chip${st.states.includes(k) ? ' on' : ''}" data-st="${k}" aria-pressed="${st.states.includes(k)}">${t(l)} <span class="cnt">${sc[k] || 0}</span></button>`).join('')}</span></div>
    ${d.items.length ? `<div class="scroll-x"><table class="t"><thead><tr><th>${t('Expires')}</th><th>${t('Credit')}</th><th class="right">${t('Amount')}</th><th class="right">${t('Remaining')}</th><th>${t('Status')}</th><th></th></tr></thead><tbody>
      ${d.items.map(c => `<tr data-cid="${c.id}"><td class="nowrap">${esc(c.expiry_date || '')}${crWhen(c)}</td>
        <td class="cr-open" style="cursor:pointer"><b>${esc(c.label)}</b><div class="small muted">${[t(CR_SOURCE[c.source] || c.source), c.account_name, t('issued {date}', {date: c.issued_date})].filter(Boolean).map(esc).join(' · ')}${c.reference ? ' · #' + esc(c.reference) : ''}</div></td>
        <td class="right nowrap">${money(c.amount, c.currency)}</td>
        <td class="right nowrap"><b class="${c.expired ? 'neg' : c.closed ? 'muted' : ''}">${money(c.remaining, c.currency)}</b></td>
        <td>${crBadge(c)}</td>
        <td class="right nowrap">${c.closed ? '' : `<button class="btn sm pri" data-use="${c.id}">${t('Use…')}</button>`} <button class="btn sm" data-op="${c.id}">${t('Details')}</button></td></tr>`).join('')}</tbody></table></div>`
      : `<div class="empty">${t('No credits here. Add one when a merchant gives you a credit instead of cash.')}</div>`}
    <div class="small muted">${t('Remind me')} <input id="crLead" type="number" min="0" max="60" style="width:60px;display:inline-block" value="${esc(set.credit_expiry_lead_months || '6')}"> ${t('months before a credit expires')} <button class="btn sm" id="crSave">${t('Save')}</button>
      <div>${t('Reminders show in Reminders and go out by email and Telegram like the others.')}</div></div></div>`;
  const after = async () => { await loadMeta(); route(); };
  $$('[data-st]', v).forEach(b => b.onclick = () => { const k = b.dataset.st; st.states = st.states.includes(k) ? st.states.filter(x => x !== k) : [...st.states, k]; route(); });
  $('#crNew', v).onclick = () => creditForm(null, after);
  $$('[data-use]', v).forEach(b => b.onclick = () => creditUse(d.items.find(c => c.id == b.dataset.use), after));
  $$('[data-op]', v).forEach(b => b.onclick = () => openCredit(+b.dataset.op, after));
  $$('td.cr-open', v).forEach(td => td.onclick = () => openCredit(+td.parentElement.dataset.cid, after));
  $('#crSave', v).onclick = async () => { await guard(() => api('/api/settings', {method: 'PUT', body: {credit_expiry_lead_months: $('#crLead').value}})); toast(t('Saved')); after(); };
};
function creditForm(c, after) {
  const bg = modal(c ? t('Edit credit') : t('New credit'), `<form class="form" id="crF" autocomplete="off">
    <label class="f"><span>${t('Account')} <span class="muted">(${t('optional')})</span></span><select name="account_id">${acctOptions(c ? c.account_id : null, {blank: t('— none: issuer only —')})}</select></label>
    <label class="f"><span>${t('Source')}</span><select name="source">${Object.entries(CR_SOURCE).map(([k, l]) => `<option value="${k}" ${c && c.source === k ? 'selected' : ''}>${t(l)}</option>`).join('')}</select></label>
    <label class="f full"><span>${t('Issuer / title')}</span><input name="title" value="${esc(c ? c.title || '' : '')}" placeholder="${t('e.g. Delta travel credit for a cancelled flight')}"></label>
    <label class="f"><span>${t('Amount')}</span><input name="amount" type="number" step="0.01" min="0" required value="${c ? c.amount : ''}"></label>
    <label class="f"><span>${t('Remaining')}</span><input name="remaining" type="number" step="0.01" min="0" value="${c ? c.remaining : ''}" placeholder="${t('= amount')}"></label>
    <label class="f"><span>${t('Currency')}</span><input name="currency" maxlength="3" value="${esc(c ? c.currency : '')}" placeholder="${t('account currency, else base')}"></label>
    <label class="f"><span>${t('Issued')}</span><input name="issued_date" type="date" value="${esc(c ? c.issued_date : today())}"></label>
    <label class="f"><span>${t('Expires')}</span><input name="expiry_date" type="date" required value="${esc(c ? c.expiry_date || '' : '')}"></label>
    <label class="f"><span>${t('Reference')}</span><input name="reference" value="${esc(c ? c.reference || '' : '')}"></label>
    <label class="f full"><span>${t('Note')}</span><input name="note" value="${esc(c ? c.note || '' : '')}"></label></form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${t('Save')}</button>`});
  const f = $('#crF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const body = Object.fromEntries(new FormData(f));
    if (body.remaining === '') delete body.remaining;
    await guard(() => api(c ? `/api/credits/${c.id}` : '/api/credits', {method: c ? 'PUT' : 'POST', body}));
    bg.close(); toast(t('Saved')); after();
  };
}
function creditUse(c, after) {
  const bg = modal(t('Use credit'), `<form class="form" id="cuF" autocomplete="off">
    <p class="small muted full">${esc(t('{title}: {amt} left, expires {date}', {title: c.label, amt: money(c.remaining, c.currency), date: c.expiry_date}))}</p>
    ${c.expired ? `<p class="small neg full">⚠️ ${t('This credit has expired. Record a use only if the merchant still honoured it.')}</p>` : ''}
    <label class="f"><span>${t('Amount used')} (${esc(c.currency)})</span><input name="amount" type="number" step="0.01" min="0" value="${c.remaining}"></label>
    <label class="f"><span>${t('Date')}</span><input name="date" type="date" value="${today()}"></label>
    <label class="f full"><span>${t('Note')}</span><input name="note" placeholder="${t('What it was used for')}"></label></form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${t('Save')}</button>`});
  const f = $('#cuF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const r = await guard(() => api(`/api/credits/${c.id}/use`, {method: 'POST', body: {amount: f.amount.value, date: f.date.value, note: f.note.value, force: !!c.expired}}));
    bg.close(); toast(r.state === 'used' ? t('Credit used up') : t('Partly used: {amt} left', {amt: money(r.remaining, r.currency)})); after();
  };
}
async function openCredit(id, after) {
  const c = await api('/api/credits/' + id);
  const bg = modal('🎟 ' + esc(c.label), `<div class="small">${crBadge(c)} ${[t(CR_SOURCE[c.source] || c.source), c.account_name].filter(Boolean).map(esc).join(' · ')}${c.reference ? ' · #' + esc(c.reference) : ''}</div>
    <div class="rb-cards"><div class="ic"><div class="lbl">${t('Amount')}</div><div class="val">${money(c.amount, c.currency)}</div></div>
      <div class="ic"><div class="lbl">${t('Used')}</div><div class="val">${money(c.used_amount, c.currency)}</div></div>
      <div class="ic"><div class="lbl">${t('Remaining')}</div><div class="val ${c.expired ? 'neg' : ''}">${money(c.remaining, c.currency)}</div></div></div>
    <div class="small">${esc(t('issued {date}', {date: c.issued_date}))} · ${esc(t('Expires'))} <b>${esc(c.expiry_date || '—')}</b>${crWhen(c)}</div>
    ${c.note ? `<div class="small muted">${esc(c.note)}</div>` : ''}
    <h3>${t('Uses')}</h3>${c.uses.length ? `<table class="t">${c.uses.map(u => `<tr><td>${esc(u.date)}</td><td>${esc(u.note || '')}</td><td class="right">${money(u.amount, c.currency)}</td><td class="right"><button class="btn sm" data-undo="${u.id}" title="${t('Undo')}">↶</button></td></tr>`).join('')}</table>` : `<div class="small muted">${t('Not used yet')}</div>`}`,
    {wide: true, buttons: `<button class="btn danger" data-del>${t('Delete')}</button><span class="sp"></span>${c.state === 'void' || c.state === 'used' && c.status !== 'open' ? `<button class="btn" data-reo>${t('Reopen')}</button>` : `<button class="btn" data-void>${t('Close (forfeit the rest)')}</button>`}<button class="btn" data-ed>✎ ${t('Edit')}</button>${c.closed ? '' : `<button class="btn pri" data-use>${t('Use…')}</button>`}`});
  const done = () => { bg.close(); after(); };
  $$('[data-undo]', bg).forEach(b => b.onclick = async () => { await guard(() => api(`/api/credits/${c.id}/uses/${b.dataset.undo}`, {method: 'DELETE'})); done(); });
  $('[data-del]', bg).onclick = async () => { if (!await confirmBox(t('Delete this credit?'))) return; await guard(() => api('/api/credits/' + c.id, {method: 'DELETE'})); done(); };
  const vd = $('[data-void]', bg); if (vd) vd.onclick = async () => { if (!await confirmBox(t('Close this credit? What is left will no longer be tracked or reminded.'))) return; await guard(() => api(`/api/credits/${c.id}/void`, {method: 'POST'})); done(); };
  const ro = $('[data-reo]', bg); if (ro) ro.onclick = async () => { await guard(() => api(`/api/credits/${c.id}/reopen`, {method: 'POST'})); done(); };
  $('[data-ed]', bg).onclick = () => { bg.close(); creditForm(c, after); };
  const us = $('[data-use]', bg); if (us) us.onclick = () => { bg.close(); creditUse(c, after); };
}
function cbCreditReminderActions(r) {
  return `<button class="btn sm pri" data-cruse="${r.credit_id}">${t('Use…')}</button> <button class="btn sm" data-crop="${r.credit_id}">${t('Open credit')}</button> <button class="btn sm" data-done="${r.id}">✓</button>`;
}
function cbBindReminders(root, after) {
  $$('[data-cruse]', root).forEach(b => b.onclick = async () => creditUse(await api('/api/credits/' + b.dataset.cruse), after));
  $$('[data-crop]', root).forEach(b => b.onclick = () => openCredit(+b.dataset.crop, after));
  $$('[data-rwbal]', root).forEach(b => b.onclick = async () => rewardBalance(await api('/api/rewards/' + b.dataset.rwbal), after));
  $$('[data-rwop]', root).forEach(b => b.onclick = () => openReward(+b.dataset.rwop, after));
}

/* ---------- Miles & points 里程/积分 (#/credits/rewards) ----------
   One row per loyalty program: balance in its own unit (miles / points), expiry date (reminded like credits), optional
   value per point for an estimated value. Update the balance in one tap; every update is kept. Never a transaction. */
S.rw = S.rw || {status: 'active'};
const RW_KIND = {airline: 'Airline', hotel: 'Hotel', card: 'Card points', other: 'Other'};
const RW_ICON = {airline: '✈️', hotel: '🏨', card: '💳', other: '⭐'};
const rwPts = (n, unit) => `${Number(n || 0).toLocaleString(i18nLocale(), {maximumFractionDigits: 2})} ${esc(t(unit || 'points'))}`;
function rwWhen(p) {
  if (!p.expiry_date) return `<div class="small muted">${t('No expiry')}</div>`;
  if (p.expired) return `<div class="small neg">⚠️ ${esc(t('Expired {n} days ago', {n: -p.days_left}))}</div>`;
  return `<div class="small ${p.expiring_soon ? 'warn-t' : 'muted'}">${esc(p.days_left === 0 ? t('Expires today') : t('{n} days left', {n: p.days_left}))}</div>`;
}
async function rewardsView(v) {
  const st = S.rw;
  const d = await api('/api/rewards?status=' + st.status);
  const sc = d.status_counts || {};
  v.innerHTML = `${crTabs('rewards')}<div class="card"><div class="page-h"><h2>✈️ ${t('Miles & points')}</h2><div class="row"><button class="btn pri" id="rwNew">＋ ${t('Add program')}</button></div></div>
    <p class="small muted">${t('Airline miles, hotel points and card points, with their expiry dates. A note only: never in balances or net worth.')}</p>
    <div class="rb-cards">
      <div class="ic"><div class="lbl">${t('Estimated value')}</div><div class="val">${d.est_value.length ? d.est_value.map(x => money(x.amount, x.currency)).join('<br>') : '—'}</div><div class="small muted">${t('Where a value per point is set')}</div></div>
      <div class="ic"><div class="lbl">${t('Expiring soon')}</div><div class="val ${d.expiring_count ? 'warn-t' : ''}">${d.expiring_count}</div><div class="small muted">${esc(t('Within {n} months', {n: d.lead_months}))}</div></div>
      <div class="ic"><div class="lbl">${t('Expired')}</div><div class="val ${d.expired_count ? 'neg' : ''}">${d.expired_count}</div><div class="small muted">${t('With points left')}</div></div>
    </div>
    <div class="rb-flt"><span class="chips" role="group">${[['active', 'Active'], ['closed', 'Closed']].map(([k, l]) => `<button type="button" class="chip${st.status === k ? ' on' : ''}" data-rs="${k}">${t(l)} <span class="cnt">${sc[k] || 0}</span></button>`).join('')}</span></div>
    ${d.items.length ? `<div class="scroll-x"><table class="t resp"><thead><tr><th>${t('Program')}</th><th class="right">${t('Balance')}</th><th>${t('Expires')}</th><th></th></tr></thead><tbody>
      ${d.items.map(p => `<tr><td data-rop="${p.id}" style="cursor:pointer">${RW_ICON[p.kind] || '⭐'} <b>${esc(p.name)}</b><div class="small muted">${[p.member_no ? '#' + p.member_no : '', t(RW_KIND[p.kind] || p.kind), p.account_name].filter(Boolean).map(esc).join(' · ')}</div></td>
        <td class="right nowrap"><b class="${p.expired ? 'neg' : ''}">${rwPts(p.balance, p.unit)}</b>${p.est_value != null ? `<div class="small muted">≈ ${money(p.est_value, p.value_currency)}</div>` : ''}<div class="small muted">${esc(t('as of {date}', {date: p.balance_date || '—'}))}</div></td>
        <td class="nowrap">${esc(p.expiry_date || '')}${rwWhen(p)}</td>
        <td class="right nowrap">${p.closed ? '' : `<button class="btn sm pri" data-rbal="${p.id}">${t('Update')}</button>`} <button class="btn sm" data-rop="${p.id}">${t('Details')}</button></td></tr>`).join('')}</tbody></table></div>`
      : `<div class="empty">${st.status === 'active' ? t('No programs yet. Add your airline, hotel or card rewards to keep an eye on their balance and expiry.') : t('Nothing here')}</div>`}
    <div class="small muted">${t('Expiry reminders use the same lead time as credits; change it on the Credits tab.')}</div></div>`;
  const after = async () => { await loadMeta(); route(); };
  const byId = Object.fromEntries(d.items.map(p => [p.id, p]));
  $('#rwNew', v).onclick = () => rewardForm(null, after);
  $$('[data-rs]', v).forEach(b => b.onclick = () => { st.status = b.dataset.rs; route(); });
  $$('[data-rbal]', v).forEach(b => b.onclick = () => rewardBalance(byId[b.dataset.rbal], after));
  $$('[data-rop]', v).forEach(b => b.onclick = () => openReward(+b.dataset.rop, after));
}
function rewardForm(p, after) {
  const bg = modal(p ? t('Edit program') : t('Add program'), `<form class="form" id="rwF" autocomplete="off">
    <label class="f full"><span>${t('Program')}</span><input name="name" required value="${esc(p ? p.name : '')}" placeholder="${t('e.g. United MileagePlus, Marriott Bonvoy')}"></label>
    <label class="f"><span>${t('Type')}</span><select name="kind">${Object.entries(RW_KIND).map(([k, l]) => `<option value="${k}" ${p && p.kind === k ? 'selected' : ''}>${RW_ICON[k]} ${t(l)}</option>`).join('')}</select></label>
    <label class="f"><span>${t('Unit')}</span><input name="unit" value="${esc(p ? p.unit : '')}" placeholder="${t('miles / points')}"></label>
    <label class="f"><span>${t('Member no.')}</span><input name="member_no" value="${esc(p ? p.member_no || '' : '')}"></label>
    <label class="f"><span>${t('Balance')}</span><input name="balance" inputmode="decimal" value="${p ? p.balance : ''}" placeholder="0"></label>
    <label class="f"><span>${t('As of')}</span><input name="balance_date" type="date" value="${esc(p ? p.balance_date || '' : today())}"></label>
    <label class="f"><span>${t('Expires')}</span><input name="expiry_date" type="date" value="${esc(p ? p.expiry_date || '' : '')}"></label>
    <label class="f"><span>${t('Value per point (¢)')} <span class="muted">(${t('optional')})</span></span><input name="value_cents" type="number" step="0.01" min="0" value="${p && p.value_per_point ? +(p.value_per_point * 100).toFixed(4) : ''}" placeholder="${t('e.g. 1.2')}"></label>
    <label class="f"><span>${t('Linked card')} <span class="muted">(${t('optional')})</span></span><select name="account_id">${acctOptions(p ? p.account_id : null, {blank: t('— none —')})}</select></label>
    <label class="f full"><span>${t('Note')}</span><input name="note" value="${esc(p ? p.note || '' : '')}"></label></form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${t('Save')}</button>`});
  const f = $('#rwF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    if (!f.reportValidity()) return;
    await guard(() => api(p ? `/api/rewards/${p.id}` : '/api/rewards', {method: p ? 'PUT' : 'POST', body: Object.fromEntries(new FormData(f))}));
    bg.close(); toast(t('Saved')); after();
  };
}
function rewardBalance(p, after) {
  const L = {total: 'New balance', earn: 'Earned', use: 'Used'};
  const bg = modal(t('Update balance'), `<form class="form" id="rbF" autocomplete="off">
    <p class="small muted full">${RW_ICON[p.kind] || '⭐'} ${esc(p.name)} · ${t('now')} ${rwPts(p.balance, p.unit)}${p.expiry_date ? ' · ' + esc(t('expires {date}', {date: p.expiry_date})) : ''}</p>
    <div class="f full"><span class="chips" role="group">${Object.entries(L).map(([k, l], i) => `<button type="button" class="chip${i ? '' : ' on'}" data-mode="${k}">${t(l)}</button>`).join('')}</span></div>
    <label class="f"><span id="rbAmtL">${t('New balance')} (${esc(t(p.unit))})</span><input name="amount" inputmode="decimal" required value="${p.balance}"></label>
    <label class="f"><span>${t('Date')}</span><input name="date" type="date" value="${today()}"></label>
    <label class="f"><span>${t('New expiry date')} <span class="muted">(${t('blank = keep')})</span></span><input name="expiry_date" type="date"></label>
    <label class="f full"><span>${t('Note')}</span><input name="note" placeholder="${t('e.g. statement, flight to Tokyo')}"></label></form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${t('Save')}</button>`});
  const f = $('#rbF', bg); $('[data-x2]', bg).onclick = bg.close;
  let mode = 'total';
  $$('[data-mode]', bg).forEach(b => b.onclick = () => {
    mode = b.dataset.mode;
    $$('[data-mode]', bg).forEach(x => x.classList.toggle('on', x === b));
    $('#rbAmtL', bg).textContent = `${t(L[mode])} (${t(p.unit)})`;
    f.amount.value = mode === 'total' ? p.balance : ''; f.amount.focus();
  });
  $('[data-ok]', bg).onclick = async () => {
    if (!f.reportValidity()) return;
    const n = parseFloat(String(f.amount.value).replace(/,/g, ''));
    const body = {date: f.date.value, expiry_date: f.expiry_date.value, note: f.note.value, ...(mode === 'total' ? {balance: n} : {change: mode === 'use' ? -n : n})};
    const r = await guard(() => api(`/api/rewards/${p.id}/balance`, {method: 'POST', body}));
    bg.close(); toast(`${t('Saved')} · ${rwPts(r.balance, r.unit)}`); after();
  };
}
async function openReward(id, after) {
  const p = await api('/api/rewards/' + id);
  const bg = modal(`${RW_ICON[p.kind] || '⭐'} ${esc(p.name)}`, `<div class="small muted">${[p.member_no ? '#' + p.member_no : '', t(RW_KIND[p.kind] || p.kind), p.account_name, p.closed ? t('Closed') : ''].filter(Boolean).map(esc).join(' · ')}</div>
    <div class="rb-cards"><div class="ic"><div class="lbl">${t('Balance')}</div><div class="val ${p.expired ? 'neg' : ''}">${rwPts(p.balance, p.unit)}</div><div class="small muted">${esc(t('as of {date}', {date: p.balance_date || '—'}))}</div></div>
      <div class="ic"><div class="lbl">${t('Expires')}</div><div class="val">${esc(p.expiry_date || '—')}</div>${rwWhen(p)}</div>
      <div class="ic"><div class="lbl">${t('Estimated value')}</div><div class="val">${p.est_value != null ? money(p.est_value, p.value_currency) : '—'}</div><div class="small muted">${p.value_per_point ? esc(t('{c}¢ per point', {c: +(p.value_per_point * 100).toFixed(4)})) : t('No value per point set')}</div></div></div>
    ${p.note ? `<div class="small muted">${esc(p.note)}</div>` : ''}
    <h3>${t('History')}</h3>${p.history.length ? `<div class="scroll-x"><table class="t">${p.history.map((h, i) => `<tr><td class="nowrap">${esc(h.date)}</td><td class="right nowrap">${h.change != null ? `<span class="${h.change < 0 ? 'neg' : 'pos'}">${h.change > 0 ? '+' : ''}${rwPts(h.change, p.unit)}</span>` : ''}</td><td class="right nowrap"><b>${rwPts(h.balance, p.unit)}</b></td><td class="small muted">${[h.note ? t(h.note) : '', h.expiry_date ? t('expires {date}', {date: h.expiry_date}) : ''].filter(Boolean).map(esc).join(' · ')}</td><td class="right">${i === 0 && p.history.length > 1 ? `<button class="btn sm" data-undo="${h.id}" title="${t('Undo')}">↶</button>` : ''}</td></tr>`).join('')}</table></div>` : `<div class="small muted">—</div>`}`,
    {wide: true, buttons: `<button class="btn danger" data-del>${t('Delete')}</button><span class="sp"></span>${p.closed ? `<button class="btn" data-reo>${t('Reopen')}</button>` : `<button class="btn" data-cls>${t('Close program')}</button>`}<button class="btn" data-ed>✎ ${t('Edit')}</button>${p.closed ? '' : `<button class="btn pri" data-bal>${t('Update balance')}</button>`}`});
  const done = () => { bg.close(); after(); };
  $$('[data-undo]', bg).forEach(b => b.onclick = async () => { await guard(() => api(`/api/rewards/${p.id}/history/${b.dataset.undo}`, {method: 'DELETE'})); done(); });
  $('[data-del]', bg).onclick = async () => { if (!await confirmBox(t('Delete this program and its history?'))) return; await guard(() => api('/api/rewards/' + p.id, {method: 'DELETE'})); done(); };
  const cl = $('[data-cls]', bg); if (cl) cl.onclick = async () => { if (!await confirmBox(t('Close this program? It will no longer be tracked or reminded.'))) return; await guard(() => api(`/api/rewards/${p.id}/close`, {method: 'POST'})); done(); };
  const ro = $('[data-reo]', bg); if (ro) ro.onclick = async () => { await guard(() => api(`/api/rewards/${p.id}/reopen`, {method: 'POST'})); done(); };
  $('[data-ed]', bg).onclick = () => { bg.close(); rewardForm(p, after); };
  const bl = $('[data-bal]', bg); if (bl) bl.onclick = () => { bg.close(); rewardBalance(p, after); };
}
function cbRewardReminderActions(r) {
  return `<button class="btn sm pri" data-rwbal="${r.reward_id}">${t('Update balance')}</button> <button class="btn sm" data-rwop="${r.reward_id}">${t('Open')}</button> <button class="btn sm" data-done="${r.id}">✓</button>`;
}

/* ---------- Bills ---------- */
S.bl = S.bl || {status: 'open'};
const BL_STATUS = {open: 'To pay', paid: 'Paid', cancelled: 'Cancelled', all: 'All'};
VIEWS.bills = async (v) => {
  const st = S.bl;
  const [d, pay] = await Promise.all([api('/api/bills?status=' + st.status), api('/api/bills/payments')]);
  const set = S.meta.settings;
  const sc = d.status_counts || {};
  v.innerHTML = `<div class="card"><div class="page-h"><h2>📬 ${t('Bills')}</h2><div class="row"><button class="btn" id="blTest">🔍 ${t('Test an email')}</button><button class="btn pri" id="blNew">＋ ${t('New bill')}</button></div></div>
    <p class="small muted">${t('Bills and payment requests found in your emails become payment tasks with a reminder before the due date. Payment confirmation emails mark the matching bill paid; anything unclear waits here for you.')}</p>
    ${pay.items.length ? `<div class="rs-review"><h3>📥 ${t('Payments to review')} (${pay.items.length})</h3>${pay.items.map(p => `<div class="rs-rv-row" data-pid="${p.id}">
      <span>💸 <b>${esc(p.payee || '?')}</b> ${p.amount != null ? money(p.amount, p.currency) : ''} <span class="small muted">${esc(p.paid_date || '')} · ${esc(p.status === 'ambiguous' ? t('Several bills fit') : t('No matching bill'))} · ${esc(p.subject || '')}</span></span>
      <span class="sp"></span><select data-pm="${p.id}"><option value="">${t('This pays…')}</option>${p.suggestions.map(b => `<option value="${b.id}">${esc(b.payee)} · ${b.amount != null ? money(b.amount, b.currency) : '—'} · ${esc(b.due_date || '')}</option>`).join('')}${d.items.filter(b => b.status === 'open' && !p.suggestions.some(s => s.id === b.id)).map(b => `<option value="${b.id}">${esc(b.payee)} · ${b.amount != null ? money(b.amount, b.currency) : '—'} · ${esc(b.due_date || '')}</option>`).join('')}</select>
      <button class="btn sm" data-pd="${p.id}">✕ ${t('Not a bill')}</button></div>`).join('')}</div>` : ''}
    <div class="rb-flt"><span class="chips" role="group">${Object.entries(BL_STATUS).map(([k, l]) => `<button type="button" class="chip${st.status === k ? ' on' : ''}" data-st="${k}">${t(l)}${k !== 'all' ? ` <span class="cnt">${sc[k] || 0}</span>` : ''}</button>`).join('')}</span></div>
    ${d.items.length ? `<div class="scroll-x"><table class="t"><thead><tr><th>${t('Due')}</th><th>${t('Payee')}</th><th class="right">${t('Amount')}</th><th>${t('Status')}</th><th></th></tr></thead><tbody>
      ${d.items.map(b => `<tr><td class="nowrap">${esc(b.due_date || '—')}${b.status === 'open' && b.days_left != null ? `<div class="small ${b.overdue ? 'neg' : b.days_left <= d.lead_days ? 'warn-t' : 'muted'}">${esc(b.overdue ? t('{n} days overdue', {n: -b.days_left}) : t('{n} days left', {n: b.days_left}))}</div>` : ''}</td>
        <td><b>${esc(b.label)}</b><div class="small muted">${b.source === 'email' ? '✉️ ' + esc(b.subject || '') : t('Added by hand')}${b.reference ? ' · #' + esc(b.reference) : ''}${b.account_name ? ' · ' + esc(b.account_name) : ''}</div>
          ${b.status === 'paid' ? `<div class="small">✓ ${esc(t('Paid {date}', {date: b.paid_at || ''}))}${b.matched === 'auto' ? ' · ' + esc(t('matched from email')) + (b.paid_subject ? ': ' + esc(b.paid_subject) : '') : ''}</div>` : ''}</td>
        <td class="right nowrap">${b.amount != null ? money(b.amount, b.currency) : '—'}</td>
        <td><span class="pill${b.overdue ? ' warn' : ''}">${t(BL_STATUS[b.status] || b.status)}</span></td>
        <td class="right nowrap">${b.status === 'open' ? `<button class="btn sm pri" data-paid="${b.id}">${t('Paid')}</button> <button class="btn sm" data-cn="${b.id}">${t('Cancel')}</button>` : `<button class="btn sm" data-ro="${b.id}">${t('Reopen')}</button>`} <button class="btn sm" data-ed="${b.id}">✎</button> <button class="btn sm danger" data-del="${b.id}">✕</button></td></tr>`).join('')}</tbody></table></div>`
      : `<div class="empty">${t('No bills here.')}</div>`}
    <div class="small muted">${t('Remind me')} <input id="blLead" type="number" min="0" max="365" style="width:60px;display:inline-block" value="${esc(set.bill_reminder_lead_days || '3')}"> ${t('days before the due date')} ·
      ${t('Match payments within')} <input id="blTol" type="number" min="0" step="0.01" style="width:70px;display:inline-block" value="${esc(set.bill_match_tolerance || '1.00')}"> ${t('(or 1%) of the bill amount, up to')} <input id="blDays" type="number" min="0" max="365" style="width:60px;display:inline-block" value="${esc(set.bill_match_days || '10')}"> ${t('days after the due date')} <button class="btn sm" id="blSave">${t('Save')}</button></div></div>`;
  const after = async () => { await loadMeta(); route(); };
  const post = async (p, body) => { await guard(() => api(p, {method: 'POST', body: body || {}})); after(); };
  const byId = id => d.items.find(b => b.id == id);
  $$('[data-st]', v).forEach(b => b.onclick = () => { st.status = b.dataset.st; route(); });
  $('#blNew', v).onclick = () => billForm(null, after);
  $('#blTest', v).onclick = () => billTest();
  $$('[data-paid]', v).forEach(b => b.onclick = () => billPaid(byId(b.dataset.paid), after));
  $$('[data-cn]', v).forEach(b => b.onclick = () => post(`/api/bills/${b.dataset.cn}/cancel`));
  $$('[data-ro]', v).forEach(b => b.onclick = () => post(`/api/bills/${b.dataset.ro}/reopen`));
  $$('[data-ed]', v).forEach(b => b.onclick = () => billForm(byId(b.dataset.ed), after));
  $$('[data-del]', v).forEach(b => b.onclick = async () => { if (!await confirmBox(t('Delete this bill?'))) return; await guard(() => api('/api/bills/' + b.dataset.del, {method: 'DELETE'})); after(); });
  $$('[data-pm]', v).forEach(sel => sel.onchange = () => sel.value && post(`/api/bills/payments/${sel.dataset.pm}/match`, {bill_id: +sel.value}));
  $$('[data-pd]', v).forEach(b => b.onclick = () => post(`/api/bills/payments/${b.dataset.pd}/dismiss`));
  $('#blSave', v).onclick = async () => { await guard(() => api('/api/settings', {method: 'PUT', body: {bill_reminder_lead_days: $('#blLead').value, bill_match_tolerance: $('#blTol').value, bill_match_days: $('#blDays').value}})); toast(t('Saved')); after(); };
};
function billForm(b, after) {
  const bg = modal(b ? t('Edit bill') : t('New bill'), `<form class="form" id="blF" autocomplete="off">
    <label class="f full"><span>${t('Payee')}</span><input name="payee" required value="${esc(b ? b.payee : '')}"></label>
    <label class="f"><span>${t('Amount')}</span><input name="amount" type="number" step="0.01" min="0" value="${b && b.amount != null ? b.amount : ''}"></label>
    <label class="f"><span>${t('Currency')}</span><input name="currency" maxlength="3" value="${esc(b ? b.currency || '' : '')}"></label>
    <label class="f"><span>${t('Due date')}</span><input name="due_date" type="date" value="${esc(b ? b.due_date || '' : '')}"></label>
    <label class="f"><span>${t('Remind days before')}</span><input name="lead_days" type="number" min="0" max="365" value="${b && b.lead_days != null ? b.lead_days : ''}" placeholder="${esc(S.meta.settings.bill_reminder_lead_days || '3')}"></label>
    <label class="f"><span>${t('Pay from')}</span><select name="account_id">${acctOptions(b ? b.account_id : null, {blank: t('— optional —')})}</select></label>
    <label class="f"><span>${t('Reference')}</span><input name="reference" value="${esc(b ? b.reference || '' : '')}"></label>
    <label class="f full"><span>${t('Note')}</span><input name="note" value="${esc(b ? b.note || '' : '')}"></label></form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${t('Save')}</button>`});
  const f = $('#blF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => { await guard(() => api(b ? `/api/bills/${b.id}` : '/api/bills', {method: b ? 'PUT' : 'POST', body: Object.fromEntries(new FormData(f))})); bg.close(); toast(t('Saved')); after(); };
}
function billPaid(b, after) {
  const bg = modal(t('Mark paid'), `<form class="form" id="bpF">
    <label class="f full"><span><input type="checkbox" name="create"> ${t('Also record the payment as an expense')}</span></label>
    <label class="f"><span>${t('From')}</span><select name="from">${acctOptions(b.account_id, {blank: t('— choose —')})}</select></label>
    <label class="f"><span>${t('Amount')} (${esc(b.currency || '')})</span><input name="amount" type="number" step="0.01" value="${b.amount ?? ''}"></label>
    <label class="f"><span>${t('Date')}</span><input name="date" type="date" value="${today()}"></label></form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${t('Save')}</button>`});
  const f = $('#bpF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    await guard(() => api(`/api/bills/${b.id}/paid`, {method: 'POST', body: {create_payment: f.create.checked, from_account_id: f.from.value ? +f.from.value : null, amount: f.amount.value, date: f.date.value}}));
    bg.close(); toast(t('Marked paid')); after();
  };
}
function billTest() {
  const bg = modal(t('Test an email'), `<form class="form" id="btF" autocomplete="off">
    <label class="f full"><span>${t('From')}</span><input name="from" placeholder="Utility Co <billing@utility.example>"></label>
    <label class="f full"><span>${t('Subject')}</span><input name="subject"></label>
    <label class="f full"><span>${t('Body')}</span><textarea name="body" rows="6"></textarea></label></form><pre class="small" id="btOut"></pre>`,
    {wide: true, buttons: `<button class="btn" data-x2>${t('Close')}</button><button class="btn pri" data-ok>${t('Parse')}</button>`});
  const f = $('#btF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => { const r = await guard(() => api('/api/bills/parse', {method: 'POST', body: Object.fromEntries(new FormData(f))})); $('#btOut', bg).textContent = JSON.stringify(r, null, 2); };
}
