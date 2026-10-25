/* Reminder Center 提醒中心: card statements, card expiry + remind-only schedules */
const REM_STATE = {upcoming: 'Upcoming', overdue: 'Overdue', snoozed: 'Snoozed', later: 'Later', done: 'Done'};
function remWhen(r) {
  if (r.state === 'done') return `<span class="pill">${esc(r.done_reason || 'done')}</span>`;
  if (r.state === 'overdue') return `<span class="pill warn">逾期 ${-r.days_left}d overdue</span>`;
  return r.days_left === 0 ? '<span class="pill warn">today</span>' : `<span class="pill">${r.days_left}d</span>`;
}
/* card expiry 信用卡到期: the card, the scheduled payments charged to it and what is still to do */
function remExpBody(r) {
  const sch = r.card_schedules || [];
  const when = r.expired ? t('Card expired {date}', {date: r.expiry_label}) : t('Card expires {date}', {date: r.expiry_label});
  const cardSt = r.card_state_eff === 'renewed' ? t('Renewed · new expiry {date}', {date: r.new_expiry_label || '—'})
    : r.card_state_eff === 'closed' ? t('Card archived') : t('Card confirmed');
  const cardLine = r.card_confirmed
    ? `<span class="pill cx-ok">✓ ${esc(cardSt)}</span>${r.card_state === 'acknowledged' && r.state !== 'done' ? ` <button class="btn sm" data-cxcu="${r.id}">${t('Undo')}</button>` : ''}`
    : (r.state === 'done' ? `<span class="muted">${t('Not confirmed')}</span>` : `<button class="btn sm pri" data-cxren="${r.id}">📅 ${t('New expiry date…')}</button> <button class="btn sm" data-cxack="${r.id}">${t('Confirm card (closed / nothing to change)')}</button>`);
  const moveOpts = acctOptions(null, {blank: t('Move to account…'), filter: a => a.id !== r.account_id});
  const li = s => `<li class="cx-${s.status}"><a href="javascript:void 0" data-cxe="${s.schedule_id}" title="${t('Edit schedule')}">✎ ${esc(s.name)}</a>
     <span class="small muted">${s.post_mode === 'remind' ? '⏰ ' + t('Remind only') + ' · ' : ''}${s.next_date ? t('next {date}', {date: esc(s.next_date.slice(0, 10))}) : ''}${s.amount != null ? ' · ' + money(s.amount, s.currency) : ''}</span>
     ${s.status === 'pending' ? (r.state === 'done' ? `<span class="pill warn">${t('Not confirmed')}</span>` : `<span class="cx-acts"><button class="btn sm" data-cxok="${s.schedule_id}">✓ ${t('Card updated')}</button><select class="cx-mv" data-cxmv="${s.schedule_id}">${moveOpts}</select></span>`)
      : s.status === 'confirmed' ? `<span class="pill cx-ok">✓ ${t('Card updated')}</span>${r.state !== 'done' ? ` <button class="btn sm" data-cxun="${s.schedule_id}">${t('Undo')}</button>` : ''}`
      : `<span class="pill cx-ok">→ ${esc(t('Moved to {account}', {account: s.to_account_name || ''}))}</span>`}</li>`;
  return `<div class="cx-head">💳 <b>${esc(r.title || '')}</b>${r.account_last4 ? ` <span class="small muted">…${esc(r.account_last4)}</span>` : ''} · <b class="${r.expired ? 'neg' : 'warn-t'}">${esc(when)}</b></div>
    <div class="cx-card small"><span class="muted">${t('Card')}:</span> ${cardLine}</div>
    ${sch.length ? `<div class="small cx-sub">${r.pending_count ? `<b>${t('Update the payment card for:')}</b>` : t('Scheduled payments on this card')}</div><ul class="cx-list">${sch.map(li).join('')}</ul>`
      : `<div class="small muted cx-sub">${t('No active scheduled payments use this card.')}</div>`}
    ${r.state !== 'done' ? `<div class="small muted">${t('This reminder stops once the card is confirmed and every scheduled payment is updated or moved.')}</div>` : ''}`;
}
function remLine(r) {
  if (r.kind === 'loan') return `<div class="row" style="justify-content:space-between;padding:4px 0"><span>🤝 <b>${esc(r.title || '')}</b> <span class="small muted">${esc(r.due_date)}</span> ${remWhen(r)}</span><b>${r.statement_amount ? money(r.statement_amount, r.currency) : ''}</b></div>`;
  if (r.kind === 'reward') return `<div class="row" style="justify-content:space-between;padding:4px 0"><span>${r.reward_icon || '⭐'} <b>${esc(r.title || '')}</b> <span class="${r.expired ? 'neg' : 'warn-t'}">${esc(t(r.expired ? 'Points expired {date}' : 'Points expire {date}', {date: r.due_date}))}</span> ${r.state === 'overdue' ? `<span class="pill cx-bad">${t('Expired')}</span>` : remWhen(r)}</span><b>${rwPts(r.reward_balance, r.reward_unit)}</b></div>`;
  if (r.kind === 'credit') return `<div class="row" style="justify-content:space-between;padding:4px 0"><span>🎟 <b>${esc(r.title || '')}</b> <span class="${r.expired ? 'neg' : 'warn-t'}">${esc(r.expired ? t('Credit expired {date}', {date: r.due_date}) : t('Credit expires {date}', {date: r.due_date}))}</span> ${r.state === 'overdue' ? `<span class="pill cx-bad">${t('Expired')}</span>` : remWhen(r)}</span><b>${r.remaining != null ? money(r.remaining, r.currency) : ''}</b></div>`;
  if (r.kind === 'bill') return `<div class="row" style="justify-content:space-between;padding:4px 0"><span>🧾 <b>${esc(r.title || '')}</b> <span class="small muted">${esc(r.bill_has_due ? t('Bill due {date}', {date: r.due_date}) : t('Payment requested'))}</span> ${remWhen(r)}</span><b>${r.statement_amount != null ? money(r.statement_amount, r.currency) : ''}</b></div>`;
  if (r.kind === 'reserve') return `<div class="row" style="justify-content:space-between;padding:4px 0"><span>🔒 <b>${esc(r.title || '')}</b> <span class="small muted">${esc(r.reserve_method === 'hold' ? t('Card hold not released') : t('Deposit not refunded'))}</span> ${remWhen(r)}</span><b>${r.remaining != null ? money(r.remaining, r.currency) : ''}</b></div>`;
  if (r.kind === 'card_expiry') return `<div class="row" style="justify-content:space-between;padding:4px 0"><span>💳 <b>${esc(r.title || '')}</b> <span class="${r.expired ? 'neg' : 'warn-t'}">${esc(r.expired ? t('Card expired {date}', {date: r.expiry_label}) : t('Card expires {date}', {date: r.expiry_label}))}</span> ${r.state === 'overdue' ? `<span class="pill cx-bad">${t('Expired')}</span>` : remWhen(r)}</span><span class="small muted">${r.pending_count ? esc(t('{n} payment(s) to update', {n: r.pending_count})) : ''}</span></div>`;
  const amt = r.remaining ?? r.statement_amount;
  return `<div class="row" style="justify-content:space-between;padding:4px 0"><span>${r.kind === 'card' ? '💳' : '🔁'} <b>${esc(r.title || '')}</b> <span class="small muted">${esc(r.due_date)}</span> ${remWhen(r)}</span><b>${amt ? money(amt, r.currency) : ''}</b></div>`;
}
VIEWS.reminders = async (v) => {
  const view = S.remView || 'open';
  const d = await api('/api/reminders?view=' + view);
  const tabs = [['open', 'Active'], ['all_upcoming', 'All upcoming'], ['overdue', REM_STATE.overdue], ['snoozed', REM_STATE.snoozed], ['done', REM_STATE.done], ['all', 'All']];
  const st = S.meta.settings;
  v.innerHTML = `<div class="page-h"><h2>⏰ Reminders</h2><div class="row"><button class="btn" id="rmRun">Refresh</button></div></div>
   <div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:8px">${tabs.map(([k, l]) => `<button class="btn sm ${k === view ? 'pri' : ''}" data-v="${k}">${l}${d.counts[k] ? ` (${d.counts[k]})` : ''}</button>`).join('')}</div>
   <div class="card"><table class="t"><thead><tr><th>Due</th><th>Item</th><th class="right">Statement</th><th class="right">Remaining</th><th></th></tr></thead><tbody>
   ${d.items.map(r => r.kind === 'card_expiry' ? `<tr class="cx-row${r.state === 'overdue' ? ' cx-late' : ''}" data-rid="${r.id}"><td class="nowrap">${esc(r.due_date)}<div>${r.state === 'overdue' ? `<span class="pill cx-bad">${t('Expired')}</span>` : remWhen(r)}</div></td>
     <td colspan="3">${remExpBody(r)}</td>
     <td class="right nowrap">${r.state === 'done' ? `<button class="btn sm" data-reopen="${r.id}">Reopen</button>` : `<button class="btn sm" data-cxdone="${r.id}">✓ Done</button> <button class="btn sm" data-snz="${r.id}">Snooze</button> <button class="btn sm" data-dis="${r.id}">✕</button>`}</td></tr>` : `<tr><td class="nowrap">${esc(r.due_date)}<div>${remWhen(r)}</div></td>
     <td>${r.kind === 'card' ? '💳' : r.kind === 'reserve' ? '🔒' : r.kind === 'credit' ? '🎟' : r.kind === 'reward' ? (r.reward_icon || '⭐') : r.kind === 'bill' ? '🧾' : r.kind === 'loan' ? '🤝' : '🔁'} <b>${esc(r.title || '')}</b>${r.account_last4 ? ` <span class="small muted">…${esc(r.account_last4)}</span>` : ''}
       <div class="small muted">${r.kind === 'reward' ? `<span class="${r.expired ? 'neg' : ''}">${rwPts(r.reward_balance, r.reward_unit)} · ${esc(t(r.expired ? 'Points expired {date}' : 'Points expire {date}', {date: r.due_date}))}</span>` : r.kind === 'credit' ? `<span class="${r.expired ? 'neg' : ''}">${esc((r.expired ? t('Credit expired {date}', {date: r.due_date}) : t('Credit expires {date}', {date: r.due_date})) + (r.account_name ? ' · ' + r.account_name : ''))}</span>` : r.kind === 'bill' ? esc(r.bill_has_due ? t('Bill from email · due {date}', {date: r.due_date}) : t('Payment requested · no due date found')) : r.kind === 'reserve' ? esc((r.reserve_method === 'hold' ? t('Card hold not released') : t('Deposit not refunded')) + (r.reserve_end ? ' · ' + t('check-out {date}', {date: r.reserve_end}) : '')) : r.kind === 'card' ? `Statement ${esc(r.statement_date)}` : r.kind === 'loan' ? esc(t(r.loan_direction === 'lent' ? 'Repayment due to you' : 'Loan payment due')) : r.loan_id ? esc(t('Loan payment due')) : 'Scheduled payment'}${r.autopay ? ' · autopay' : ''}${r.reference ? ' · #' + esc(r.reference) : ''}${r.minimum ? ' · min ' + money(r.minimum, r.currency) : ''}${r.note ? ' · ' + esc(r.note) : ''}</div></td>
     <td class="right">${r.statement_amount != null ? money(r.statement_amount, r.currency) : '—'}${r.amount_override != null ? ' <span class="small muted">(edited)</span>' : ''}</td>
     <td class="right">${r.remaining != null ? money(r.remaining, r.currency) : ''}${r.paid ? `<div class="small muted">paid ${money(r.paid, r.currency)}</div>` : ''}</td>
     <td class="right nowrap">${r.state === 'done' ? `<button class="btn sm" data-reopen="${r.id}">Reopen</button>` : `${r.kind === 'reserve' ? rsReminderActions(r) : r.kind === 'credit' ? cbCreditReminderActions(r) : r.kind === 'reward' ? cbRewardReminderActions(r) : r.loan_id ? lnReminderActions(r) + ` <button class="btn sm" data-done="${r.id}">✓</button>` : `<button class="btn sm pri" data-paid="${r.id}">Paid</button> <button class="btn sm" data-done="${r.id}">✓</button>`} <button class="btn sm" data-snz="${r.id}">Snooze</button> <button class="btn sm" data-dis="${r.id}">✕</button>`} <button class="btn sm" data-ed="${r.id}">✎</button></td></tr>`).join('') || '<tr><td colspan="5" class="muted">Nothing here. Set a statement/due day on a credit card, or a schedule to “Remind only”.</td></tr>'}
   </tbody></table></div>
   <div class="card small"><b>Settings</b> · Show reminders <input id="rmLead" type="number" min="0" max="365" style="width:70px;display:inline-block" value="${esc(st.reminder_lead_days || '10')}"> <span>days before due (default; cards/schedules can override)</span> ·
     <span>Notify</span> <select id="rmNtf" style="width:auto;display:inline-block"><option value="off">Off</option><option value="telegram" ${st.reminder_notify === 'telegram' ? 'selected' : ''}>Telegram</option></select> ·
     <span>Card expiry: remind</span> <input id="rmCx" type="number" min="0" max="365" style="width:70px;display:inline-block" value="${esc(st.card_expiry_lead_days || '60')}"> <span>days before the card expires</span> ·
     <span>${t('Credits: remind')}</span> <input id="rmCr" type="number" min="0" max="60" style="width:60px;display:inline-block" value="${esc(st.credit_expiry_lead_months || '6')}"> <span>${t('months before a credit expires')}</span> ·
     <span>${t('Bills: remind')}</span> <input id="rmBl" type="number" min="0" max="365" style="width:60px;display:inline-block" value="${esc(st.bill_reminder_lead_days || '3')}"> <span>${t('days before the due date')}</span> <button class="btn sm" id="rmSave">Save</button>
     <div class="muted">iPhone: Shortcut → GET /api/reminders/notify (see README).</div></div>`;
  $$('[data-v]', v).forEach(b => b.onclick = () => { S.remView = b.dataset.v; route(); });
  const act = async (p, body) => { await guard(() => api('/api/reminders/' + p, {method: 'POST', body: body || {}})); await loadMeta(); route(); };
  $('#rmRun').onclick = async () => { const r = await guard(() => api('/api/reminders/run', {method: 'POST'})); toast(`${r.created} new · ${r.auto_completed} auto-completed`); await loadMeta(); route(); };
  $('#rmSave').onclick = async () => { await guard(() => api('/api/settings', {method: 'PUT', body: {reminder_lead_days: $('#rmLead').value, reminder_notify: $('#rmNtf').value, card_expiry_lead_days: $('#rmCx').value, credit_expiry_lead_months: $('#rmCr').value, bill_reminder_lead_days: $('#rmBl').value}})); await loadMeta(); toast('Saved'); route(); };
  $$('[data-done]', v).forEach(b => b.onclick = () => act(b.dataset.done + '/done'));
  $$('[data-dis]', v).forEach(b => b.onclick = () => act(b.dataset.dis + '/dismiss'));
  $$('[data-reopen]', v).forEach(b => b.onclick = () => act(b.dataset.reopen + '/reopen'));
  $$('[data-snz]', v).forEach(b => b.onclick = async () => { const n = prompt('Snooze for how many days?', '3'); if (n) act(b.dataset.snz + '/snooze', {days: +n}); });
  const byId = id => d.items.find(r => r.id == id);
  $$('[data-paid]', v).forEach(b => b.onclick = () => remPaid(byId(b.dataset.paid)));
  $$('[data-ed]', v).forEach(b => b.onclick = () => remEdit(byId(b.dataset.ed)));
  remExpBind(v, byId, async () => { await loadMeta(); route(); });
  rsBindReminders(v, async () => { await loadMeta(); route(); });
  lnBindReminders(v, async () => { await loadMeta(); route(); });
  cbBindReminders(v, async () => { await loadMeta(); route(); });
};
function remExpBind(v, byId, after) {
  const rowOf = el => byId(el.closest('tr[data-rid]').dataset.rid);
  const post = async (p, body) => { const r = await guard(() => api('/api/reminders/' + p, {method: 'POST', body: body || {}})); if (r && r.status === 'done' && r.done_reason === 'resolved') toast(t('All done: the card expiry reminder is resolved')); await after(); return r; };
  $$('[data-cxok]', v).forEach(b => b.onclick = () => post(`${rowOf(b).id}/schedules/${b.dataset.cxok}/confirm`));
  $$('[data-cxun]', v).forEach(b => b.onclick = () => post(`${rowOf(b).id}/schedules/${b.dataset.cxun}/confirm`, {undo: true}));
  $$('[data-cxmv]', v).forEach(sel => sel.onchange = async () => {
    if (!sel.value) return;
    const acc = (S.meta.accounts || []).find(a => a.id == sel.value);
    if (!await confirmBox(t('Move this scheduled payment to {account}?', {account: acc ? acc.name : ''}))) { sel.value = ''; return; }
    post(`${rowOf(sel).id}/schedules/${sel.dataset.cxmv}/reassign`, {account_id: +sel.value});
  });
  $$('[data-cxe]', v).forEach(a => a.onclick = async () => { const s = await guard(() => api('/api/schedules/' + a.dataset.cxe)); openSchedule(s, after); });
  $$('[data-cxack]', v).forEach(b => b.onclick = () => post(b.dataset.cxack + '/card', {action: 'acknowledge'}));
  $$('[data-cxcu]', v).forEach(b => b.onclick = () => post(b.dataset.cxcu + '/card', {action: 'undo'}));
  $$('[data-cxren]', v).forEach(b => b.onclick = () => remRenew(byId(b.dataset.cxren), after));
  $$('[data-cxdone]', v).forEach(b => b.onclick = async () => {
    const r = byId(b.dataset.cxdone);
    if (r.pending_count && !await confirmBox(t('{n} scheduled payment(s) still use this card and are not confirmed: {names}. Mark the reminder done anyway?', {n: r.pending_count, names: r.pending_names.join(', ')}))) return;
    await post(r.id + '/done');
  });
}
function remRenew(r, after) {
  const bg = modal(t('New card expiry'), `<form class="form" id="cxF" autocomplete="off">
    <p class="small muted full">${esc(t('{card} currently expires {date}. Enter the expiry printed on the new card.', {card: r.title || '', date: r.expiry_label}))}</p>
    <label class="f"><span>${t('Expires (MM/YY)')}</span><input name="expiry" placeholder="MM/YY" maxlength="7" inputmode="numeric" autofocus></label></form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${t('Save')}</button>`});
  const f = $('#cxF', bg); $('[data-x2]', bg).onclick = bg.close;
  const go = async () => { await guard(() => api(`/api/reminders/${r.id}/card`, {method: 'POST', body: {action: 'renew', expiry: f.expiry.value}})); bg.close(); toast(t('Card renewed')); await after(); };
  $('[data-ok]', bg).onclick = go; f.onsubmit = e => { e.preventDefault(); go(); };
}
function remPaid(r) {
  const card = r.kind === 'card';
  const bg = modal('Mark paid', `<form class="form" id="rpF">
    <label class="f full"><span><input type="checkbox" name="create" checked> Record the payment 记录还款${card ? ' (transfer to the card 转账到信用卡)' : ''}</span></label>
    <label class="f"><span>From</span><select name="from">${acctOptions(null, {blank: card ? '— choose —' : '(schedule default)', filter: a => a.id !== r.account_id})}</select></label>
    <label class="f"><span>Amount (${esc(r.currency || '')})</span><input name="amount" type="number" step="0.01" value="${r.remaining ?? r.statement_amount ?? ''}"></label>
    <label class="f"><span>Date</span><input name="date" type="date" value="${today()}"></label></form>`,
    {buttons: '<button class="btn" data-x2>Cancel</button><button class="btn pri" data-ok>Save</button>'});
  const f = $('#rpF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    await guard(() => api(`/api/reminders/${r.id}/paid`, {method: 'POST', body: {create_payment: f.create.checked, from_account_id: f.from.value ? +f.from.value : null, amount: f.amount.value, date: f.date.value}}));
    bg.close(); toast('Marked paid'); await loadMeta(); route();
  };
}
function remEdit(r) {
  const bg = modal('Edit reminder', `<form class="form" id="reF">
    <label class="f"><span>Statement amount</span><input name="amount_override" type="number" step="0.01" value="${r.amount_override ?? ''}" placeholder="computed ${r.amount ?? ''}"></label>
    <label class="f"><span>Minimum</span><input name="minimum" type="number" step="0.01" value="${r.minimum ?? ''}"></label>
    <label class="f"><span>Statement no.</span><input name="reference" value="${esc(r.reference || '')}"></label>
    <label class="f"><span>Due date</span><input name="due_date" type="date" value="${esc(r.due_date)}"></label>
    <label class="f full"><span>Note</span><input name="note" value="${esc(r.note || '')}"></label></form>`,
    {buttons: '<button class="btn" data-x2>Cancel</button><button class="btn pri" data-ok>Save</button>'});
  const f = $('#reF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => { await guard(() => api('/api/reminders/' + r.id, {method: 'PUT', body: Object.fromEntries(new FormData(f))})); bg.close(); toast('Saved'); await loadMeta(); route(); };
}
