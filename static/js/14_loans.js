/* ---------- Loan Center 借贷中心 (#/loans) ----------
   Money you lent or borrowed. The loan's account holds what is still owed; a repayment is split into the principal
   (a transfer: not spending) and the interest / escrow / fees (expense or income). Expected schedule from the current
   balance, an optional recurring schedule that posts the split payment, and reminders. */
S.ln = S.ln || {state: 'active', direction: ''};
const LN_STATE = {active: 'Active', paid_off: 'Paid off', closed: 'Closed'};
const LN_FREQ = {monthly: 'Monthly', biweekly: 'Every 2 weeks', weekly: 'Weekly', quarterly: 'Quarterly', yearly: 'Yearly', once: 'One repayment at the due date', none: 'Repay any time'};
const LN_ROLE = {disburse: ['➡️', 'Disbursed'], principal: ['🏦', 'Principal'], interest: ['％', 'Interest'], escrow: ['🏠', 'Escrow (tax / insurance)'], fee: ['🧾', 'Fee'], writeoff: ['✂️', 'Written off']};
const lnDir = x => `<span class="pill ln-d ln-${esc(x.direction)}">${x.direction === 'lent' ? '📤 ' + t('I lent') : '📥 ' + t('I borrowed')}</span>`;
const lnBadge = x => `<span class="rs-st ln-st-${esc(x.state)}">${t(LN_STATE[x.state] || x.state)}</span>`;
const lnTotals = o => o.by_currency.length ? o.by_currency.map(c => money(c.amount, c.currency)).join('<br>') + (o.by_currency.length > 1 ? `<div class="small muted">≈ ${money(o.total_base, o.base_currency)}</div>` : '') : money(0);
function lnNext(x) {
  if (x.closed) return '';
  if (x.overdue_days > 0) return `<div class="small neg">⏰ ${esc(t('{n} days overdue', {n: x.overdue_days}))}</div>`;
  const p = x.next_payment;
  if (x.next_due) return `<div class="small">${esc(x.next_due)}${p ? ' · ' + money(p.payment, x.currency) : ''}</div>`;
  if (x.maturity_date) return `<div class="small muted">${esc(t('Due by {date}', {date: x.maturity_date}))}</div>`;
  return `<div class="small muted">${t('Repay any time')}</div>`;
}

VIEWS.loans = async (v) => {
  const st = S.ln;
  const q = new URLSearchParams({state: st.state});
  if (st.direction) q.set('direction', st.direction);
  const d = await api('/api/loans?' + q);
  const sc = d.state_counts || {};
  const base = d.base_currency;
  v.innerHTML = `${demoBanner()}<div class="card"><div class="page-h"><h2>🤝 ${t('Loans')}</h2><div class="row"><a class="btn sm" href="/api/loans/export.csv">⬇ ${t('Export CSV')}</a><button class="btn pri" id="lnNew">＋ ${t('New loan')}</button></div></div>
    <p class="small muted">${t('Money you lent or borrowed. Only the interest (and escrow or fees) is income or spending; the principal is a transfer to or from the loan account.')}</p>
    <div class="rb-cards">
      <div class="ic"><div class="lbl">${t('Owed to me')}</div><div class="val">${lnTotals(d.owed_to_me)}</div><div class="small muted">${t('Money lent, not repaid yet')}</div></div>
      <div class="ic"><div class="lbl">${t('I owe')}</div><div class="val">${lnTotals(d.i_owe)}</div><div class="small muted">${t('Loans and money borrowed')}</div></div>
      <div class="ic"><div class="lbl">${t('Interest this year')}</div><div class="val">${money(d.interest_paid_ytd, base)}</div><div class="small muted">${esc(t('paid · earned {amt}', {amt: money(d.interest_earned_ytd, base)}))}</div></div>
      <div class="ic"><div class="lbl">${t('Overdue')}</div><div class="val ${d.overdue_count ? 'neg' : ''}">${d.overdue_count}</div><div class="small muted">${t('Payments past their due date')}</div></div>
    </div>
    <form class="rb-flt" id="lnFlt" autocomplete="off">
      <span class="chips" role="group" aria-label="${t('Status')}">${[...Object.entries(LN_STATE), ['all', 'All']].map(([k, l]) => `<button type="button" class="chip${st.state === k ? ' on' : ''}" data-st="${k}" aria-pressed="${st.state === k}">${t(l)}${k !== 'all' ? ` <span class="cnt">${sc[k] || 0}</span>` : ''}</button>`).join('')}</span>
      <select name="direction"><option value="">${t('Lent and borrowed')}</option><option value="lent" ${st.direction === 'lent' ? 'selected' : ''}>${t('I lent')}</option><option value="borrowed" ${st.direction === 'borrowed' ? 'selected' : ''}>${t('I borrowed')}</option></select></form>
    ${d.items.length ? `<div class="scroll-x"><table class="t ln-tbl"><thead><tr><th>${t('Loan')}</th><th class="right">${t('Outstanding')}</th><th class="right">${t('Rate')}</th><th>${t('Next payment')}</th><th>${t('Status')}</th><th></th></tr></thead><tbody>
      ${d.items.map(x => `<tr data-lid="${x.id}"><td class="ln-open"><div>${isDemoName(x.account_name) ? '<span class="pill demo">DEMO</span> ' : ''}${lnDir(x)} <b>${esc(x.label)}</b></div>
          <div class="small muted">${esc(x.account_name || '')}${x.counterparty && x.counterparty !== x.label ? ' · ' + esc(x.counterparty) : ''} · ${t(LN_FREQ[x.freq] || x.freq)}${x.schedule_id ? ' · 🔁' : ''}</div></td>
        <td class="right nowrap"><b>${money(x.outstanding, x.currency)}</b>${x.principal ? `<div class="small muted">${esc(t('of {amt}', {amt: money(x.principal, x.currency)}))}</div>` : ''}</td>
        <td class="right nowrap">${x.rate ? esc(String(+x.rate)) + '%' : '—'}</td>
        <td>${lnNext(x)}${x.payoff_date && !x.closed ? `<div class="small muted">${esc(t('Paid off {date}', {date: x.payoff_date}))}</div>` : ''}</td>
        <td>${lnBadge(x)}</td>
        <td class="rb-act nowrap">${x.closed ? '' : `<button class="btn sm pri" data-pay="${x.id}">💵 ${t('Record payment')}</button>`} <button class="btn sm" data-op="${x.id}">${t('Details')}</button></td></tr>`).join('')}</tbody></table></div>`
      : `<div class="empty">${st.state === 'active' ? t('No active loans. Add money you lent to someone, or a loan or mortgage you pay back.') : t('No matching loans')}</div>`}
    <div class="small muted">${t('Tip: an existing loan (e.g. a mortgage you already pay) can be tracked from now on: choose its account and nothing in the past is changed.')}</div></div>`;
  const f = $('#lnFlt', v);
  f.onchange = () => { st.direction = f.direction.value; route(); };
  $$('[data-st]', f).forEach(b => b.onclick = () => { st.state = b.dataset.st; route(); });
  $('#lnNew', v).onclick = () => loanForm({after: route});
  $$('[data-pay]', v).forEach(b => b.onclick = () => loanPay(+b.dataset.pay, {after: route}));
  $$('[data-op]', v).forEach(b => b.onclick = () => openLoan(+b.dataset.op, route));
  $$('td.ln-open', v).forEach(td => { td.style.cursor = 'pointer'; td.onclick = () => openLoan(+td.parentElement.dataset.lid, route); });
};

/* detail */
function lnAmounts(x) {
  const m = v => money(v, x.currency);
  const rows = [['Outstanding', `<b>${m(x.outstanding)}</b>`], ...(x.principal ? [['Original amount', m(x.principal)]] : []),
    ['Principal repaid', m(x.principal_repaid)], [x.direction === 'lent' ? 'Interest earned' : 'Interest paid', m(x.interest_total)],
    ['This year', m(x.interest_ytd)], ...(x.accrued_interest ? [['Interest accrued', m(x.accrued_interest)]] : []),
    ...(x.payoff_date ? [['Paid off', esc(x.payoff_date)], ['Interest still to come', m(x.future_interest)]] : [])];
  return `<div class="rs-amts">${rows.map(([l, val]) => `<div><div class="lbl small muted">${t(l)}</div><div>${val}</div></div>`).join('')}</div>`;
}
function lnTerms(x) {
  const bits = [x.rate ? esc(t('{r}% a year', {r: +x.rate})) : t('No interest'), t(LN_FREQ[x.freq] || x.freq)];
  if (x.payment) bits.push(esc(t('payment {amt}', {amt: money(x.payment, x.currency)})) + (x.escrow ? ' + ' + esc(t('escrow {amt}', {amt: money(x.escrow, x.currency)})) : ''));
  if (x.term) bits.push(esc(t('{n} payments', {n: x.term})));
  if (x.maturity_date) bits.push(esc(t('due by {date}', {date: x.maturity_date})));
  if (x.pay_account_name) bits.push((x.direction === 'lent' ? '⬅ ' : '➡ ') + esc(x.pay_account_name));
  return `<div class="small">${bits.join(' · ')}</div>`;
}
function lnEvents(x) {
  if (!x.events.length) return `<div class="empty small">${t('No payments yet.')}</div>`;
  return `<ol class="rs-tl">${x.events.slice().reverse().map(e => {
    const head = e.kind === 'disburse' ? `➡️ <b>${t(x.direction === 'lent' ? 'Money lent' : 'Money received')}</b> ${money(e.disburse, x.currency)}`
      : e.kind === 'writeoff' ? `✂️ <b>${t('Written off')}</b> ${money(e.writeoff, x.currency)}`
      : `💵 <b>${t(x.direction === 'lent' ? 'Repayment received' : 'Payment')}</b> ${money(e.total, x.currency)} <span class="small muted">${[['principal', 'principal'], ['interest', 'interest'], ['escrow', 'escrow'], ['fee', 'fee']].filter(([k]) => e[k] > 0.005).map(([k, l]) => esc(t(l)) + ' ' + money(e[k], x.currency)).join(' · ')}</span>${e.due_date ? ` <span class="pill">${esc(t('for {date}', {date: e.due_date}))}</span>` : e.kind === 'payment' ? ` <span class="pill">${t('extra')}</span>` : ''}${e.posted ? '' : ` <span class="pill warn">${t('to review')}</span>`}`;
    const links = e.links.map(k => `<div class="small muted ln-lk">${LN_ROLE[k.role] ? LN_ROLE[k.role][0] : '•'} ${esc(t(LN_ROLE[k.role] ? LN_ROLE[k.role][1] : k.role))} ${money(k.amount, x.currency)} · ${esc(k.tx_kind === 'transfer' ? `${k.tx_account_name || ''} → ${k.tx_to_account_name || ''}` : (k.tx_account_name || '') + (k.tx_category_name ? ' · ' + k.tx_category_name : ''))}
      <button type="button" class="lnk" data-otx="${k.transaction_id}" title="${t('Open the transaction')}">↗</button><button type="button" class="lnk danger" data-lun="${k.id}" title="${t('Unlink')}" aria-label="${t('Unlink')}">✕</button></div>`).join('');
    return `<li class="rs-tl-l"><span class="rs-tl-d small muted">${esc((e.date || '').slice(0, 10))}</span><span class="rs-tl-b">${head} <button type="button" class="lnk danger" data-undo="${e.id}" title="${t('Undo this payment')}">↶</button>${links}</span></li>`;
  }).join('')}</ol>`;
}
function lnDue(x) {
  const late = x.due_rows.filter(r => r.status === 'overdue' || r.status === 'partial');
  if (!late.length) return '';
  return `<div class="demo-banner">⏰ ${late.map(r => esc(r.status === 'partial' ? t('{date}: {paid} of {amt} paid', {date: r.due_date, paid: money(r.paid, x.currency), amt: money(r.expected, x.currency)}) : t('{date}: not paid', {date: r.due_date}))).join(' · ')}</div>`;
}
function lnProjection(x, all) {
  if (x.projection_warning) return `<div class="small warn-t">⚠ ${esc(t(x.projection_warning))}</div>`;
  if (!x.projection.length) return x.freq === 'none' ? `<div class="small muted">${t('No fixed schedule: record repayments whenever they happen.')}</div>` : '';
  const rows = all ? x.projection : x.projection.slice(0, 12);
  return `<div class="scroll-x"><table class="t ln-proj"><thead><tr><th>${t('Due')}</th><th class="right">${t('Payment')}</th><th class="right">${t('Principal')}</th><th class="right">${t('Interest')}</th>${x.escrow ? `<th class="right">${t('Escrow')}</th>` : ''}<th class="right">${t('Balance after')}</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td class="nowrap">${esc(r.due_date)}</td><td class="right">${money(r.payment, x.currency)}</td><td class="right">${money(r.principal, x.currency)}</td><td class="right">${money(r.interest, x.currency)}</td>${x.escrow ? `<td class="right">${money(r.escrow, x.currency)}</td>` : ''}<td class="right">${money(r.balance_after, x.currency)}</td></tr>`).join('')}</tbody></table></div>
    ${!all && x.projection.length > 12 ? `<button type="button" class="btn sm" data-allproj>${esc(t('Show all {n} payments', {n: x.projection.length}))}</button>` : ''}
    <div class="small muted">${t('Expected from the current balance at the loan rate; extra payments shorten it. Your lender\'s numbers can differ by a few cents.')}</div>`;
}
async function openLoan(id, after) {
  const [x, sg] = await Promise.all([guard(() => api('/api/loans/' + id)), api('/api/loans/suggest?loan_id=' + id).catch(() => ({items: []}))]);
  const done = () => { bg.close(); if (after) after(); };
  const again = () => { bg.close(); openLoan(id, after); };
  const sched = x.schedule ? `🔁 ${esc(x.schedule.name)} · ${x.schedule.post_mode === 'remind' ? t('reminder only') : t('records each payment automatically')}${x.schedule.enabled ? '' : ' · ' + t('paused')}` : '';
  const bg = modal(`🤝 ${esc(x.label)}`, `<div class="rs-head">${lnDir(x)} ${lnBadge(x)} <span class="small muted">${esc(x.account_name || '')}${x.counterparty && x.counterparty !== x.label ? ' · ' + esc(x.counterparty) : ''}</span></div>
    ${lnTerms(x)}${lnDue(x)}${lnAmounts(x)}
    ${x.periodic !== false && ['monthly', 'biweekly', 'weekly', 'quarterly', 'yearly'].includes(x.freq) ? `<div class="small">${sched || `<span class="muted">${t('No schedule: you get a reminder for each payment.')}</span>`} ${x.closed ? '' : `<button class="btn sm" data-a="sched">${x.schedule ? '✎ ' + t('Schedule') : '🔁 ' + t('Set up schedule')}</button>`}${x.schedule ? ` <button class="btn sm" data-a="unsched">${t('Stop schedule')}</button>` : ''}</div>` : ''}
    ${sg.items.length && !x.closed ? `<h3>🔎 ${t('Possible payments')}</h3><div class="ln-sug">${sg.items.slice(0, 6).map(s => `<div class="rs-rv-row"><span>${esc(s.date.slice(0, 10))} · <b>${money(s.amount, s.currency)}</b> <span class="small muted">${esc(s.kind)} · ${esc(s.merchant_name || s.note || s.account_name || '').slice(0, 60)}</span></span><span class="sp"></span><button class="btn sm pri" data-cv="${s.id}">💵 ${t('Record as payment')}</button></div>`).join('')}</div>` : ''}
    <h3>${t('Payments')}</h3>${lnEvents(x)}
    <h3>${t('Expected schedule')}</h3><div id="lnProj">${lnProjection(x, false)}</div>
    ${x.note ? `<div class="small muted">📝 ${esc(x.note)}</div>` : ''}`,
    {wide: true, buttons: `${x.closed ? `<button class="btn" data-a="reopen">${t('Reopen')}</button>` : `<button class="btn pri" data-a="pay">💵 ${t('Record payment')}</button><button class="btn" data-a="link">🔗 ${t('Link transactions')}</button><button class="btn" data-a="more">➕ ${t(x.direction === 'lent' ? 'Lend more' : 'Borrow more')}</button><button class="btn" data-a="close">${t('Close loan')}</button>`}
      <button class="btn" data-a="edit">✎ ${t('Edit')}</button><button class="btn danger" data-a="delete">${t('Delete')}</button>`});
  $$('[data-otx]', bg).forEach(b => b.onclick = () => openTx(+b.dataset.otx, again));
  $$('[data-lun]', bg).forEach(b => b.onclick = async () => { if (!await confirmBox(t('Unlink this transaction? It stays in your transactions.'))) return; await guard(() => api('/api/loans/links/' + b.dataset.lun, {method: 'DELETE'})); again(); });
  $$('[data-undo]', bg).forEach(b => b.onclick = () => lnUndo(+b.dataset.undo, again));
  $$('[data-cv]', bg).forEach(b => b.onclick = () => { bg.close(); loanPay(id, {fromTx: +b.dataset.cv, after: () => openLoan(id, after)}); });
  const ap = $('[data-allproj]', bg); if (ap) ap.onclick = () => { $('#lnProj', bg).innerHTML = lnProjection(x, true); };
  const act = {
    pay: () => { bg.close(); loanPay(id, {after: () => openLoan(id, after)}); },
    link: () => { bg.close(); lnLinkDialog(id, () => openLoan(id, after)); },
    more: () => { bg.close(); lnMore(x, () => openLoan(id, after)); },
    edit: () => { bg.close(); loanForm({id, after: () => openLoan(id, after)}); },
    sched: () => { bg.close(); lnSchedule(x, () => openLoan(id, after)); },
    unsched: async () => { if (!await confirmBox(t('Stop the schedule? Payments already recorded stay.'))) return; await guard(() => api(`/api/loans/${id}/schedule`, {method: 'DELETE'})); again(); },
    close: () => { bg.close(); lnClose(x, after); },
    reopen: async () => { await guard(() => api(`/api/loans/${id}/close`, {method: 'POST', body: {action: 'reopen'}})); again(); },
    delete: async () => {
      const n = x.events.reduce((a, e) => a + e.links.length, 0);
      if (!await confirmBox(n ? t('Delete this loan? Its {n} linked transaction(s) and its account stay, only the links go.', {n}) : t('Delete this loan? Its account stays.'))) return;
      await guard(() => api(`/api/loans/${id}${n ? '?unlink=1' : ''}`, {method: 'DELETE'})); toast(t('Deleted')); done();
    },
  };
  $$('[data-a]', bg).forEach(b => b.onclick = act[b.dataset.a]);
}
async function lnUndo(eid, after) {
  const bg = modal(t('Undo this payment'), `<p>${t('Unlink keeps the transactions (they no longer count for the loan). Delete removes them from your transactions too.')}</p>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn" data-k="0">${t('Unlink only')}</button><button class="btn danger" data-k="1">${t('Delete the transactions')}</button>`});
  $('[data-x2]', bg).onclick = bg.close;
  $$('[data-k]', bg).forEach(b => b.onclick = async () => { await guard(() => api(`/api/loans/events/${eid}?delete_transactions=${b.dataset.k === '1'}`, {method: 'DELETE'})); bg.close(); toast(t('Done')); after(); });
}

/* new / edit */
async function loanForm(opts = {}) {
  await (S.meta || loadMeta());
  const m = S.meta;
  const x = opts.id ? await guard(() => api('/api/loans/' + opts.id)) : Object.assign({direction: 'borrowed', kind: 'person', freq: 'none', start_date: today(), remind: 1, rate: ''}, opts.preset || {});
  const edit = !!opts.id;
  const loanAccts = dir => a => !a.archived && !a.system_role && (dir === 'lent' ? a.type === 'receivable' : ['loan', 'payable'].includes(a.type));
  const bank = a => !a.archived && !a.system_role && !['loan', 'payable', 'receivable'].includes(a.type) || a.id == x.pay_account_id;
  const bg = modal(edit ? `✎ ${esc(x.label)}` : `🤝 ${t('New loan')}`, `<form class="form" id="lnF" autocomplete="off">
    <div class="full kind-tabs">${[['borrowed', '📥 ' + t('I borrowed')], ['lent', '📤 ' + t('I lent')]].map(([k, l]) => `<button type="button" data-dir="${k}" class="${x.direction === k ? 'on' : ''}" ${edit ? 'disabled' : ''}>${l}</button>`).join('')}</div>
    <label class="f"><span>${t('Who with (person or bank)')}</span><input name="counterparty" value="${esc(x.counterparty || '')}" placeholder="${t('e.g. Mom, Chase, RoundPoint')}"></label>
    <label class="f"><span>${t('Type')}</span><select name="kind">${[['person', 'Person'], ['bank', 'Bank / lender'], ['other', 'Other']].map(([k, l]) => `<option value="${k}" ${x.kind === k ? 'selected' : ''}>${t(l)}</option>`).join('')}</select></label>
    <label class="f"><span>${t('Name')} <small class="muted">${t('(optional)')}</small></span><input name="name" value="${esc(x.name && x.name !== x.counterparty ? x.name : '')}" placeholder="${t('e.g. House mortgage')}"></label>
    <label class="f"><span>${t('Start date')}</span><input name="start_date" type="date" value="${esc(x.start_date || today())}"></label>
    ${edit ? '' : `<fieldset class="full rs-co-box"><legend>${t('Where is the money?')}</legend>
      <label class="chk full"><input type="checkbox" name="existing"> ${t('Already exists: track it from now on (nothing in the past is changed)')}</label>
      <label class="f" data-ex="1" hidden><span>${t('Loan account')}</span><select name="account_id"><option value="">${t('— new account —')}</option></select></label>
      <label class="f" data-ex="1" data-newacct hidden><span>${t('Still owed today')}</span><input name="balance" type="number" step="0.01" min="0" inputmode="decimal"></label>
      <label class="f" data-ex="0"><span>${t('Amount')}</span><input name="principal" type="number" step="0.01" min="0.01" inputmode="decimal"></label>
      <label class="f" data-newacct><span>${t('Currency')}</span><select name="currency">${curOptions(m.settings.base_currency)}</select></label>
      <div class="full small muted" data-ex="0" data-for="borrowed">${t('Records a transfer from a new loan account into the account below: not income.')}</div>
      <div class="full small muted" data-ex="0" data-for="lent">${t('Records a transfer from the account below into a new receivable account: not spending.')}</div></fieldset>`}
    <label class="f"><span data-for="borrowed">${t('Paid from (and money received into)')}</span><span data-for="lent">${t('Repayments arrive in (and money lent from)')}</span><select name="pay_account_id">${acctOptions(x.pay_account_id, {filter: bank})}</select></label>
    <label class="f"><span>${t('Interest rate (% a year)')}</span><input name="rate" type="number" step="0.001" min="0" inputmode="decimal" value="${x.rate || ''}" placeholder="0"></label>
    <label class="f"><span>${t('Repayment')}</span><select name="freq">${Object.entries(LN_FREQ).map(([k, l]) => `<option value="${k}" ${x.freq === k ? 'selected' : ''}>${t(l)}</option>`).join('')}</select></label>
    <label class="f" data-per><span>${t('First / next payment date')}</span><input name="first_due" type="date" value="${esc(x.first_due || '')}"></label>
    <label class="f" data-per><span>${t('Number of payments')} <small class="muted">${t('(left)')}</small></span><input name="term" type="number" min="1" max="1200" step="1" value="${x.term || ''}"></label>
    <label class="f" data-per><span>${t('Regular payment')} <small class="muted">${t('(blank = from the number of payments)')}</small></span><input name="payment" type="number" step="0.01" min="0" inputmode="decimal" value="${x.payment || ''}"></label>
    <label class="f" data-mat><span>${t('Due by')} <small class="muted">${t('(optional for repay any time)')}</small></span><input name="maturity_date" type="date" value="${esc(x.maturity_date || '')}"></label>
    <label class="f" data-for="borrowed"><span>${t('Interest category')}</span><select name="interest_category_id" data-kind="expense">${catOptions('expense', x.direction === 'borrowed' ? x.interest_category_id : null)}</select></label>
    <label class="f" data-for="lent"><span>${t('Interest category')}</span><select name="interest_category_id_in" data-kind="income">${catOptions('income', x.direction === 'lent' ? x.interest_category_id : null)}</select></label>
    <details class="full" data-for="borrowed" ${x.escrow ? 'open' : ''}><summary class="small">${t('Escrow (optional): property tax / insurance paid with each payment')}</summary><div class="form">
      <label class="f"><span>${t('Escrow per payment')}</span><input name="escrow" type="number" step="0.01" min="0" inputmode="decimal" value="${x.escrow || ''}"></label>
      <label class="f"><span>${t('Escrow category')}</span><select name="escrow_category_id">${catOptions('expense', x.escrow_category_id)}</select></label></div></details>
    <label class="f full"><span>${t('Note')}</span><input name="note" value="${esc(x.note || '')}"></label>
    <label class="chk full"><input type="checkbox" name="remind" ${x.remind ? 'checked' : ''}> ${t('Remind me when a payment is due')}</label>
    <div class="full small muted" id="lnHint"></div></form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${edit ? t('Save') : t('Create')}</button>`});
  const f = $('#lnF', bg); let dir = x.direction;
  const sync = () => {
    $$('[data-dir]', bg).forEach(b => b.classList.toggle('on', b.dataset.dir === dir));
    $$('[data-for]', bg).forEach(el => el.hidden = el.dataset.for !== dir);
    const per = ['monthly', 'biweekly', 'weekly', 'quarterly', 'yearly'].includes(f.freq.value);
    $$('[data-per]', bg).forEach(el => el.hidden = !per);
    $$('[data-mat]', bg).forEach(el => el.hidden = per);
    if (!edit) {
      const ex = f.existing.checked;
      const sel = f.account_id, keep = sel.value;
      sel.innerHTML = `<option value="">${t('— new account —')}</option>` + (m.accounts || []).filter(loanAccts(dir)).map(a => `<option value="${a.id}">${esc(a.name)} (${esc(a.currency)})</option>`).join('');
      if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
      $$('[data-ex]', bg).forEach(el => el.hidden = (el.dataset.ex === '1') !== ex || (el.dataset.for && el.dataset.for !== dir));
      $$('[data-newacct]', bg).forEach(el => el.hidden = ex && !!sel.value || (el.dataset.ex === '1' && !ex));
    }
    $('#lnHint', bg).textContent = per ? t('An existing loan: enter the next payment date and the number of payments left; the schedule starts from what is owed today.') : '';
  };
  $$('[data-dir]', bg).forEach(b => b.onclick = () => { dir = b.dataset.dir; sync(); });
  f.addEventListener('change', sync);
  sync();
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const per = !$('[data-per]', bg).hidden;
    const body = {counterparty: f.counterparty.value, kind: f.kind.value, name: f.name.value, start_date: f.start_date.value,
      pay_account_id: +f.pay_account_id.value || null, rate: f.rate.value || 0, freq: f.freq.value,
      first_due: per ? f.first_due.value : null, term: per ? f.term.value : null, payment: per ? f.payment.value : null,
      maturity_date: per ? null : f.maturity_date.value || null,
      interest_category_id: (dir === 'lent' ? f.interest_category_id_in.value : f.interest_category_id.value) || null,
      escrow: dir === 'borrowed' ? f.escrow.value || null : null, escrow_category_id: dir === 'borrowed' ? f.escrow_category_id.value || null : null,
      note: f.note.value, remind: f.remind.checked};
    if (!edit) {
      body.direction = dir;
      if (f.existing.checked) {
        body.disburse = false;
        if (f.account_id.value) body.account_id = +f.account_id.value;
        else { body.balance = f.balance.value; body.currency = f.currency.value; }
        if (f.principal.value) body.principal = f.principal.value;
      } else { body.principal = f.principal.value; body.currency = f.currency.value; body.disburse = true; }
    }
    const r = await guard(() => edit ? api('/api/loans/' + opts.id, {method: 'PUT', body}) : api('/api/loans', {method: 'POST', body}));
    bg.close(); toast(edit ? t('Saved') : t('Loan saved'));
    await loadMeta();
    if (opts.after) opts.after(r); else location.hash = '#/loans';
  };
}

/* record a repayment: amount → default split (editable), previewed with dry_run; fromTx = split that transaction in place */
async function loanPay(id, opts = {}) {
  const x = await guard(() => api('/api/loans/' + id));
  const tx = opts.fromTx ? await guard(() => api('/api/transactions/' + opts.fromTx)) : null;
  const cur = x.currency, lent = x.direction === 'lent';
  const amt0 = tx ? (tx.orig_currency === cur ? tx.orig_amount : tx.kind === 'transfer' && tx.to_amount != null && tx.to_account_id === x.account_id ? tx.to_amount : tx.amount) : '';
  const bank = a => !a.archived && a.id !== x.account_id && !a.system_role;
  const bg = modal(`💵 ${t(lent ? 'Repayment received' : 'Record payment')} · ${esc(x.label)}`, `<form class="form" id="lpF" autocomplete="off">
    ${tx ? `<div class="full small muted">${esc(t('This transaction becomes the first part of the payment (same transaction, receipts kept); the other parts are added: {d}', {d: `${tx.date.slice(0, 10)} · ${money(tx.amount, tx.currency)} · ${tx.merchant_name || tx.note || ''}`}))}</div>` : ''}
    <label class="f"><span>${t('Total')} (${esc(cur)})</span><input name="amount" type="number" step="0.01" min="0" inputmode="decimal" value="${amt0 ?? ''}" placeholder="${x.next_payment ? x.next_payment.payment : ''}" ${tx ? 'readonly' : ''}></label>
    <label class="f"><span>${t('Date')}</span><input name="date" type="date" value="${esc(tx ? tx.date.slice(0, 10) : opts.date || today())}" ${tx ? 'readonly' : ''}></label>
    <label class="f"><span>${t(lent ? 'Received into' : 'Paid from')}</span><select name="account_id" ${tx ? 'disabled' : ''}>${acctOptions(tx ? (tx.kind === 'transfer' ? (lent ? tx.to_account_id : tx.account_id) : tx.account_id) : x.pay_account_id, {filter: bank})}</select></label>
    <label class="chk"><input type="checkbox" name="extra"> ${t('Extra payment (all principal, not an installment)')}</label>
    <fieldset class="full rs-co-box"><legend>${t('Split')} <small class="muted">${t('(filled in for you; edit to match the statement)')}</small></legend>
      <label class="f"><span>🏦 ${t('Principal')}</span><input name="principal" type="number" step="0.01" min="0" inputmode="decimal"></label>
      <label class="f"><span>％ ${t('Interest')}</span><input name="interest" type="number" step="0.01" min="0" inputmode="decimal"></label>
      ${lent ? '' : `<label class="f"><span>🏠 ${t('Escrow')}</span><input name="escrow" type="number" step="0.01" min="0" inputmode="decimal"></label>`}
      <label class="f"><span>🧾 ${t('Fee')}</span><input name="fee" type="number" step="0.01" min="0" inputmode="decimal"></label></fieldset>
    <label class="f full"><span>${t('Note')}</span><input name="note"></label></form><div class="rs-prev" id="lpPrev"></div>`,
    {wide: true, buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>✓ ${t('Save')}</button>`});
  const f = $('#lpF', bg);
  const parts = ['principal', 'interest', 'escrow', 'fee'].filter(k => f[k]);
  let manual = false;
  const fill = async () => {
    const q = new URLSearchParams({date: f.date.value || today(), extra: f.extra.checked});
    if (f.amount.value) q.set('amount', f.amount.value);
    try {
      const s = await api(`/api/loans/${id}/split?` + q);
      parts.forEach(k => { f[k].value = s[k] ? s[k].toFixed(2) : ''; });
      if (!f.amount.value) f.amount.placeholder = s.total.toFixed(2);
    } catch (e) { /* preview shows the error */ }
  };
  const payload = () => {
    const b = {date: f.date.value, extra: f.extra.checked, note: f.note.value || undefined};
    if (!tx) b.account_id = +f.account_id.value || null;
    if (manual) parts.forEach(k => { b[k] = f[k].value || 0; });
    if (f.amount.value) b.amount = f.amount.value;
    else if (manual) b.amount = parts.reduce((a, k) => a + (+f[k].value || 0), 0).toFixed(2);
    return b;
  };
  const url = tx ? `/api/transactions/${tx.id}/to-loan-payment` : `/api/loans/${id}/payments`;
  const extraBody = tx ? {loan_id: id} : {};
  const line = p => `${LN_ROLE[p.role][0]} ${esc(t(LN_ROLE[p.role][1]))} <b>${money(p.amount, cur)}</b> <span class="muted">${esc(p.kind === 'transfer' ? t('transfer') : t(p.kind))}${p.in_place ? ' · ' + t('this transaction') : ''}</span>`;
  let h;
  const preview = () => { clearTimeout(h); h = setTimeout(async () => {
    const el = $('#lpPrev', bg); if (!el) return;
    try {
      const r = await api(url, {method: 'POST', body: {...extraBody, ...payload(), dry_run: true}});
      el.innerHTML = `<h3>${t('Preview')}</h3><ul class="rs-plan">${r.plan.map(p => `<li>${line(p)}</li>`).join('')}</ul>
        <div class="small">${r.due_date ? esc(t('Installment {date}', {date: r.due_date})) + ' · ' : r.extra ? t('Extra payment') + ' · ' : ''}${esc(t('{amt} still owed after this', {amt: money(r.outstanding_after, cur)}))}</div>
        <div class="small muted">${t('Only the interest, escrow and fee count as spending or income. Nothing is saved until you press Save.')}</div>`;
      $('[data-ok]', bg).disabled = false;
    } catch (e) { el.innerHTML = `<div class="neg">⚠ ${esc(errText(e))}</div>`; $('[data-ok]', bg).disabled = true; }
  }, 250); };
  f.addEventListener('input', async e => {
    if (parts.includes(e.target.name)) { manual = true; if (!tx) f.amount.value = parts.reduce((a, k) => a + (+f[k].value || 0), 0).toFixed(2); }
    else if (['amount', 'date', 'extra'].includes(e.target.name)) { manual = false; await fill(); }
    preview();
  });
  f.addEventListener('change', async e => { if (e.target.name === 'extra') { manual = false; await fill(); } preview(); });
  await fill(); preview();
  $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    await guard(() => api(url, {method: 'POST', body: {...extraBody, ...payload()}}));
    bg.close(); toast(t('Payment recorded')); await loadMeta(); if (opts.after) opts.after(); else route();
  };
}

/* link existing transactions as one payment */
async function lnLinkDialog(id, after) {
  const d = await guard(() => api(`/api/loans/suggest?loan_id=${id}&limit=60`));
  const roles = ['principal', 'interest', 'escrow', 'fee', 'disburse'];
  const bg = modal(`🔗 ${t('Link transactions')}`, d.items.length ? `<div class="scroll-x"><table class="t"><thead><tr><th></th><th>${t('Date')}</th><th>${t('Transaction')}</th><th class="right">${t('Amount')}</th><th>${t('As')}</th></tr></thead><tbody>
    ${d.items.map(s => `<tr><td><input type="checkbox" data-pick="${s.id}" aria-label="${t('Select')}"></td><td class="small nowrap">${esc(s.date.slice(0, 10))}</td><td>${esc(s.merchant_name || s.note || s.account_name || '').slice(0, 60)}<div class="small muted">${esc(s.kind)} · ${esc(s.account_name || '')}</div></td><td class="right nowrap">${money(s.amount, s.currency)}</td>
      <td><select data-role="${s.id}">${roles.map(r => `<option value="${r}" ${r === s.role ? 'selected' : ''}>${t(LN_ROLE[r][1])}</option>`).join('')}</select></td></tr>`).join('')}</tbody></table></div>
    <div class="small muted">${t('The selected transactions become one payment and are not changed. To split a single payment into principal and interest, use Record as payment instead.')}</div>`
    : `<div class="empty">${t('No matching transactions in the last 75 days.')}</div>`,
    {wide: true, buttons: `<button class="btn" data-x2>${t('Cancel')}</button>${d.items.length ? `<button class="btn pri" data-ok>🔗 ${t('Link')}</button>` : ''}`});
  $('[data-x2]', bg).onclick = bg.close;
  const ok = $('[data-ok]', bg);
  if (ok) ok.onclick = async () => {
    const items = $$('[data-pick]', bg).filter(c => c.checked).map(c => ({transaction_id: +c.dataset.pick, role: $(`[data-role="${c.dataset.pick}"]`, bg).value}));
    if (!items.length) { toast(t('Select at least one transaction'), true); return; }
    await guard(() => api(`/api/loans/${id}/links`, {method: 'POST', body: {items}})); bg.close(); toast(t('Linked')); if (after) after();
  };
}

async function lnMore(x, after) {
  const bg = modal(`➕ ${t(x.direction === 'lent' ? 'Lend more' : 'Borrow more')}`, `<form class="form" id="lmF">
    <label class="f"><span>${t('Amount')} (${esc(x.currency)})</span><input name="amount" type="number" step="0.01" min="0.01" inputmode="decimal"></label>
    <label class="f"><span>${t('Account')}</span><select name="account_id">${acctOptions(x.pay_account_id, {filter: a => !a.archived && a.id !== x.account_id && !a.system_role})}</select></label>
    <label class="f"><span>${t('Date')}</span><input name="date" type="date" value="${today()}"></label></form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${t('Save')}</button>`});
  const f = $('#lmF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => { await guard(() => api(`/api/loans/${x.id}/disburse`, {method: 'POST', body: {amount: f.amount.value, account_id: +f.account_id.value, date: f.date.value}})); bg.close(); toast(t('Saved')); after(); };
}

async function lnSchedule(x, after) {
  const sch = (await api('/api/schedules').catch(() => ({items: []}))).items.filter(s => !s.loan_id && s.enabled);
  const bg = modal(`🔁 ${t('Schedule')} · ${esc(x.label)}`, `<form class="form" id="lsF">
    <div class="full small muted">${esc(t('Each {freq} payment of {amt} is split into principal and interest automatically.', {freq: t(LN_FREQ[x.freq]).toLowerCase(), amt: money((x.payment || 0) + (x.escrow || 0), x.currency)}))}</div>
    <label class="f full"><span>${t('Mode')}</span><select name="post_mode"><option value="post" ${x.schedule && x.schedule.post_mode === 'remind' ? '' : 'selected'}>${t('Record each payment automatically (autopay)')}</option><option value="remind" ${x.schedule && x.schedule.post_mode === 'remind' ? 'selected' : ''}>${t('Reminder only (I record it when I pay)')}</option></select></label>
    <label class="f"><span>${t('First payment')}</span><input name="start_date" type="date" value="${esc(x.next_due && x.next_due >= today() ? x.next_due : '')}"></label>
    ${sch.length && !x.schedule ? `<label class="f full"><span>${t('Replace an existing schedule')} <small class="muted">${t('(it is paused; its history stays)')}</small></span><select name="adopt"><option value="">—</option>${sch.map(s => `<option value="${s.id}">${esc(s.name)}${s.template && s.template.amount ? ' · ' + money(+s.template.amount, s.currency) : ''}${s.next_dates && s.next_dates[0] ? ' · ' + esc(s.next_dates[0].slice(0, 10)) : ''}</option>`).join('')}</select></label>` : ''}</form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${t('Save')}</button>`});
  const f = $('#lsF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const body = {post_mode: f.post_mode.value, start_date: f.start_date.value || null};
    if (f.adopt && f.adopt.value) body.adopt_schedule_id = +f.adopt.value;
    await guard(() => api(`/api/loans/${x.id}/schedule`, {method: 'POST', body})); bg.close(); toast(t('Saved')); after();
  };
}

async function lnClose(x, after) {
  const owed = x.outstanding > 0.005;
  const lent = x.direction === 'lent';
  const bg = modal(`${t('Close loan')} · ${esc(x.label)}`, `<form class="form" id="lcF">
    ${owed ? `<div class="full">${esc(t('{amt} is still owed.', {amt: money(x.outstanding, x.currency)}))}</div>
      <label class="chk full"><input type="checkbox" name="wo" checked> ${t(lent ? 'Write it off: it will not be repaid (recorded as an expense)' : 'The rest was forgiven (recorded as income)')}</label>
      <label class="f"><span>${t('Category')}</span><select name="cat">${catOptions(lent ? 'expense' : 'income', null)}</select></label>
      <label class="f"><span>${t('Date')}</span><input name="date" type="date" value="${today()}"></label>` : `<div class="full">${t('Nothing is owed: the loan is closed.')}</div>`}</form>`,
    {buttons: `<button class="btn" data-x2>${t('Cancel')}</button><button class="btn pri" data-ok>${t('Close loan')}</button>`});
  const f = $('#lcF', bg); $('[data-x2]', bg).onclick = bg.close;
  $('[data-ok]', bg).onclick = async () => {
    const body = {action: 'close'};
    if (owed) Object.assign(body, {write_off: f.wo.checked, category_id: f.cat.value || null, date: f.date.value});
    await guard(() => api(`/api/loans/${x.id}/close`, {method: 'POST', body})); bg.close(); toast(t('Closed')); await loadMeta(); if (after) after(); else route();
  };
}

/* transaction dialog: the loan a transaction belongs to, or "Record as a loan payment" */
async function lnTxBox(box, tid, after) {
  const [tx, sg] = await Promise.all([api('/api/transactions/' + tid), api('/api/loans/suggest?transaction_id=' + tid).catch(() => ({items: []}))]);
  const mine = tx.loans || [];
  if (!mine.length && !sg.items.length) return;
  box.hidden = false;
  box.innerHTML = `<div class="small"><b>🤝 ${t('Loans')}</b> ${mine.map(l => `<a href="javascript:void 0" class="pill" data-oln="${l.id}">${esc(l.title || '#' + l.id)} · ${esc(t(LN_ROLE[l.role] ? LN_ROLE[l.role][1] : l.role))}</a>`).join(' ')}
    ${sg.items.length ? `<select id="lnPick" class="rs-pick"><option value="">${t('Record as a loan payment…')}</option>${sg.items.map(l => `<option value="${l.id}" data-cv="${l.convert ? 1 : 0}" data-role="${l.role}">${esc(l.label)} · ${money(l.outstanding, l.currency)}${l.convert ? '' : ' · ' + esc(t('link as {role}', {role: t(LN_ROLE[l.role][1])}))}</option>`).join('')}</select>` : ''}</div>`;
  $$('[data-oln]', box).forEach(a => a.onclick = () => openLoan(+a.dataset.oln, after));
  const p = $('#lnPick', box);
  if (p) p.onchange = async () => {
    if (!p.value) return;
    const o = p.selectedOptions[0];
    if (o.dataset.cv === '1') loanPay(+p.value, {fromTx: tid, after});
    else { await guard(() => api(`/api/loans/${p.value}/links`, {method: 'POST', body: {items: [{transaction_id: tid, role: o.dataset.role}]}})); toast(t('Linked')); lnTxBox(box, tid, after); }
  };
}
/* list badge */
const txLoanPill = x => (x.loans || []).map(l => `<span class="pill" title="${t('Part of a loan payment')}">🤝 ${esc((l.title || '').replace(/^\[DEMO\]\s*/, '') || t('Loan'))}${l.role === 'principal' ? ' · ' + t('principal') : l.role === 'interest' ? ' · ' + t('interest') : ''}</span> `).join('');
/* reminders page */
function lnReminderActions(r) {
  return `<button class="btn sm pri" data-lnpay="${r.loan_id}" data-due="${esc(r.kind === 'schedule' ? r.occurrence_date || '' : '')}" data-rid="${r.id}">💵 ${t('Record payment')}</button> <button class="btn sm" data-lnop="${r.loan_id}">${t('Open loan')}</button>`;
}
function lnBindReminders(root, after) {
  $$('[data-lnpay]', root).forEach(b => b.onclick = () => loanPay(+b.dataset.lnpay, {after: async () => {
    if (b.dataset.due) await api(`/api/reminders/${b.dataset.rid}/done`, {method: 'POST'}).catch(() => {});
    after();
  }, date: b.dataset.due && b.dataset.due < today() ? b.dataset.due : undefined}));
  $$('[data-lnop]', root).forEach(b => b.onclick = () => openLoan(+b.dataset.lnop, after));
}
