/* Low balance 余额预警: a bank account's balance over the next days (schedules, bills, card statements, pending) */
const CF_TYPES = ['checking', 'savings', 'cash', 'ewallet'];
const CF_SRC = {posted: 'Dated ahead', pending: 'Pending', schedule: 'Scheduled', bill: 'Bill', card: 'Card statement'};
const cfAlertable = tp => CF_TYPES.includes(tp);
async function forecastDialog(aid, days, after) {
  const f = await guard(() => api(`/api/accounts/${aid}/forecast` + (days ? '?days=' + days : '')));
  const cur = f.currency, thr = f.threshold;
  const opts = [...new Set([7, 14, 30, 60, 90, f.days])].sort((a, b) => a - b);
  const row = e => `<tr class="${thr != null && e.balance < thr ? 'cf-low' : ''}"><td class="nowrap">${esc(e.date)}</td>
    <td>${e.transaction_id ? `<a href="#" data-txo="${e.transaction_id}">${esc(e.title || '')}</a>` : e.schedule_id ? `<a href="#" data-cfs="${e.schedule_id}">${esc(e.title || '')}</a>` : esc(e.title || '')}
      <div class="small muted">${esc(t(CF_SRC[e.source]))}${e.remind ? ' · ' + esc(t('Remind only')) : ''}${e.overdue ? ' · <span class="neg">' + esc(t('overdue, due {date}', {date: e.due_date})) + '</span>' : ''}${e.estimated ? ' · ' + esc(t('estimated from the balance owed now')) : ''}${e.statement_date ? ' · ' + esc(t('statement {date}', {date: e.statement_date})) : ''}</div></td>
    <td class="right nowrap ${e.amount < 0 ? 'neg' : e.amount > 0 ? 'pos' : ''}">${e.amount == null ? `<span class="muted">${t('no amount')}</span>` : money(e.amount, cur)}</td>
    <td class="right nowrap ${thr != null && e.balance < thr ? 'neg' : ''}">${money(e.balance, cur)}</td></tr>`;
  const bg = modal(esc(t('Balance forecast · {name}', {name: f.account_name})), `
    <div class="row cf-kpi" style="gap:22px;flex-wrap:wrap;align-items:flex-end;margin-bottom:8px">
      <div><div class="small muted">${t('Now')}</div><b>${money(f.start_balance, cur)}</b></div>
      <div><div class="small muted">${t('Lowest')}</div><b class="${f.below ? 'neg' : ''}">${money(f.lowest.balance, cur)}</b> <span class="small muted">${esc(f.lowest.date)}</span></div>
      <div><div class="small muted">${esc(t('In {n} days', {n: f.days}))}</div><b>${money(f.end_balance, cur)}</b></div>
      <div><div class="small muted">${t('Alert below')}</div><b>${thr != null ? money(thr, cur) : '—'}</b></div>
      <label class="small">${t('Days')} <select id="cfDays" style="width:auto;display:inline-block">${opts.map(n => `<option ${n === f.days ? 'selected' : ''}>${n}</option>`).join('')}</select></label>
    </div>
    ${f.below ? `<p class="neg"><b>⚠️ ${esc(t('May drop below {thr} on {date}', {thr: money(thr, cur), date: f.first_below.date}))}</b></p>`
      : thr != null ? `<p class="pos small">✓ ${esc(t('Stays above {thr} for the next {n} days', {thr: money(thr, cur), n: f.days}))}</p>`
      : `<p class="small muted">${t('No low balance alert on this account: set one with Edit.')}</p>`}
    ${f.unknown_count ? `<p class="small muted">${esc(t('{n} item(s) have no amount yet and count as 0', {n: f.unknown_count}))}</p>` : ''}
    <div style="height:220px"><canvas id="cfChart"></canvas></div>
    <div style="overflow-x:auto"><table class="t"><thead><tr><th>${t('Date')}</th><th>${t('Item')}</th><th class="right">${t('Amount')}</th><th class="right">${t('Balance')}</th></tr></thead><tbody>
      <tr><td class="nowrap">${esc(f.as_of)}</td><td class="muted">${t('Balance today')}</td><td></td><td class="right nowrap">${money(f.start_balance, cur)}</td></tr>
      ${f.events.map(row).join('') || `<tr><td colspan="4" class="muted">${t('Nothing scheduled or due in this period')}</td></tr>`}
    </tbody></table></div>
    <p class="small muted">${t('Counts posted transactions dated ahead, pending ones, scheduled transactions, open bills paid from this account and the statements of cards paid from it.')}</p>`, {wide: true});
  chart('cfChart', {type: 'line', data: {labels: f.daily.map(x => x.date.slice(5)), datasets: [
    {label: 'Balance', data: f.daily.map(x => x.balance), borderColor: PALETTE[1], backgroundColor: PALETTE[1], stepped: true, pointRadius: 2, borderWidth: 2},
    ...(thr != null ? [{label: 'Alert below', data: f.daily.map(() => thr), borderColor: PALETTE[0], borderDash: [6, 4], pointRadius: 0, borderWidth: 1.5}] : [])]},
    options: {scales: {y: {ticks: {callback: v => money(v, cur)}}}}});
  $('#cfDays', bg).onchange = e => { bg.close(); forecastDialog(aid, +e.target.value, after); };
  $$('[data-txo]', bg).forEach(a => a.onclick = e => { e.preventDefault(); bg.close(); openTx(+a.dataset.txo, after || (() => route())); });
  $$('[data-cfs]', bg).forEach(a => a.onclick = async e => { e.preventDefault(); const s = await guard(() => api('/api/schedules/' + a.dataset.cfs)); bg.close(); openSchedule(s, after || (() => route())); });
}
/* account form: the alert on bank / cash accounts, the paying account on credit cards */
function cfAcctFields(a) {
  return `<label class="f" data-lb><span>${t('Low balance alert below')}</span><input name="low_bal_threshold" type="number" step="0.01" value="${a.low_bal_threshold ?? ''}" placeholder="${t('blank = no alert (e.g. 500)')}"></label>
   <label class="f" data-lb><span>${t('Look ahead (days)')}</span><input name="low_bal_days" type="number" min="1" max="120" value="${a.low_bal_days ?? ''}" placeholder="${esc(t('default (Reminders, {n})', {n: S.meta.settings.low_balance_days || 14}))}"></label>
   <label class="f" data-cc><span>${t('Statements paid from')}</span><select name="pay_from_account_id">${acctOptions(a.pay_from_account_id, {blank: t('— not set —'), filter: x => cfAlertable(x.type) && x.id !== a.id})}</select><span class="small muted">${t('Counted in that account\'s balance forecast.')}</span></label>`;
}
function cfReminderLine(r) {
  return `<span class="neg">${esc(t('Below {thr} from {date}', {thr: money(r.threshold, r.currency), date: r.due_date}))}</span> · ${esc(t('lowest {low}', {low: money(r.lowest, r.currency)}))}`;
}
