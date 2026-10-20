'use strict';
/* ---------- core helpers ---------- */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const S = {meta: null, ledger: localStorage.getItem('ledger') || '', charts: {}};
const nowLocal = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };  // local time, YYYY-MM-DDTHH:MM
const today = () => nowLocal().slice(0, 10);
const thisMonth = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`; };
function localDT(d = new Date()) { const p = n => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; }

async function api(path, opts = {}) {
  const o = {method: opts.method || 'GET', headers: {'Accept-Language': i18nLocale()}};  // API 'message' texts follow the UI language
  if (opts.body instanceof FormData) o.body = opts.body;
  else if (opts.body !== undefined) { o.body = JSON.stringify(opts.body); o.headers['Content-Type'] = 'application/json'; }
  const r = await fetch(path, o);
  if (r.status === 401) { location.href = '/login'; throw new Error('Not logged in'); }
  const ct = r.headers.get('content-type') || '';
  const j = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) { const e = new Error((j && j.detail) ? (typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)) : r.statusText); e.status = r.status; throw e; }
  return j;
}
function toast(msg, err = false, ms = 0) { const t = $('#toast'); t.textContent = msg; t.className = err ? 'err' : ''; t.style.display = 'block'; clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', ms || (err ? Math.min(15000, 5000 + String(msg).length * 40) : 2500)); }
async function guard(fn) { try { return await fn(); } catch (e) { toast(errText(e), true); if (e && typeof e === 'object') e._shown = true; throw e; } }
function errText(e) { return (e && (e.message || e.detail)) || String(e || i18nText('Something went wrong')); }
// null-safe event binding: an element missing (page changed while loading) is skipped instead of throwing
function bind(sel, ev, fn, root = document) { const x = typeof sel === 'string' ? $(sel, root) : sel; if (x) x['on' + ev] = fn; return x; }
// last line of defence: no "Uncaught (in promise)". Errors guard() already showed are just swallowed.
window.addEventListener('unhandledrejection', ev => {
  const e = ev.reason; ev.preventDefault();
  if (e && e._shown) return;
  if (e && e.status === 401) return;
  console.warn('unhandled', e); try { toast(errText(e), true); } catch (_) { /* toast not ready */ }
});

function money(v, cur) {
  cur = cur || (S.meta && S.meta.settings.base_currency) || 'USD';
  if (v === null || v === undefined || isNaN(v)) return '—';
  try { return new Intl.NumberFormat(i18nLocale(), {style: 'currency', currency: cur, minimumFractionDigits: 2, maximumFractionDigits: 2}).format(v); }
  catch { return `${Number(v).toFixed(2)} ${cur}`; }
}
const num = (v, d = 2) => v === null || v === undefined ? '' : Number(v).toLocaleString(i18nLocale(), {maximumFractionDigits: d});
const confPill = c => c === null || c === undefined ? '' : `<span class="conf ${c >= .75 ? 'hi' : c >= .5 ? 'md' : 'lo'}">${Math.round(c * 100)}%</span>`;
const kindLabel = {expense: 'Expense', income: 'Income', transfer: 'Transfer', refund: 'Refund', adjustment: 'Balance adj.', buy: 'Buy', sell: 'Sell', dividend: 'Dividend', invest_fee: 'Fee', reimbursement: 'Reimbursement'};
const srcLabel = {balance_set: 'Balance set', opening_balance: 'Opening balance', import: 'Import', scheduled: '🔁 Scheduled', reconcile: '✔ Reconcile'};
/* Reconciliation 对账: a transaction change dated on/before an account's latest checkpoint does not move today's balance.
   With Settings → General → lock on, the server answers 409 and we ask before retrying with confirm_reconciled. */
function reconWarning(date, ...acctIds) {
  const d = (date || '').replace('T', ' ');
  const hits = acctIds.map(acctById).filter(a => a && a.reconciled_at && d && d.slice(0, 16) <= a.reconciled_at.slice(0, 16));
  return hits.length ? `🔒 ${hits.map(a => `${esc(a.name)} was reconciled on ${esc(a.reconciled_at.slice(0, 10))}`).join('; ')}. Changing a transaction on or before that date will NOT change today's balance; it shows up as drift in the account's reconciliation history.` : '';
}
async function reconApi(path, opts = {}) {
  try { return await api(path, opts); }
  catch (e) {
    if (e.status !== 409 || !/Reconciled/.test(e.message)) { toast(e.message, true); throw e; }
    if (!await confirmBox(e.message)) throw e;
    if ((opts.method || 'GET') === 'DELETE') return guard(() => api(path + (path.includes('?') ? '&' : '?') + 'confirm_reconciled=true', opts));
    return guard(() => api(path, {...opts, body: {...opts.body, confirm_reconciled: true}}));
  }
}
const isDemoName = n => /^\[DEMO\]/.test(n || '');

async function loadMeta() {
  S.meta = await api('/api/meta');
  const want = i18nDetect(i18nSaved() || (S.meta.prefs && S.meta.prefs.ui_lang) || '');
  if (want !== I18N.lang) i18nSet(want, {persist: false});
  i18nMeta(S.meta); renderNav(); renderLedgers(); renderLangs(); return S.meta;
}
function renderLangs() {
  const sel = $('#langSel'); if (!sel) return;
  sel.title = t('Language'); sel.innerHTML = i18nSupported().map(c => `<option value="${c}" ${c === I18N.lang ? 'selected' : ''}>🌐 ${esc(I18N.names[c])}</option>`).join('');
  sel.onchange = async () => {
    i18nSet(sel.value);
    try { await api('/api/prefs', {method: 'PUT', body: {ui_lang: sel.value}}); } catch (e) { /* per-browser choice still applies */ }
    location.reload();
  };
}
const acctById = id => (S.meta.accounts || []).find(a => a.id == id);

function modal(title, html, {wide = false, onOpen, buttons} = {}) {
  const bg = document.createElement('div'); bg.className = 'modal-bg';
  bg.innerHTML = `<div class="modal ${wide ? 'wide' : ''}"><header><h2>${title}</h2><button class="btn ghost" data-x>✕</button></header><div class="body">${html}</div>${buttons ? `<footer>${buttons}</footer>` : ''}</div>`;
  document.body.appendChild(bg);
  const close = () => bg.remove();
  bg.addEventListener('mousedown', e => { if (e.target === bg) close(); });
  $('[data-x]', bg).onclick = close;
  bg.close = close;
  if (onOpen) onOpen(bg);
  return bg;
}
function confirmBox(msg) { return Promise.resolve(window.confirm(i18nText(msg))); }

function chart(id, cfg) {
  if (S.charts[id]) S.charts[id].destroy();
  const el = document.getElementById(id); if (!el || !window.Chart) return;
  cfg.options = Object.assign({responsive: true, maintainAspectRatio: false, animation: false, plugins: {legend: {position: 'bottom', labels: {boxWidth: 12}}}}, cfg.options || {});
  const tr = v => typeof v === 'string' ? i18nText(v) : v;  // canvas text is not reached by the DOM translator
  if (cfg.data) {
    if (Array.isArray(cfg.data.labels)) cfg.data.labels = cfg.data.labels.map(tr);
    (cfg.data.datasets || []).forEach(d => { if (typeof d.label === 'string') d.label = tr(d.label); });
  }
  S.charts[id] = new Chart(el, cfg);
}
const PALETTE = ['#e8553e','#3a7bd5','#14a06f','#e4a11b','#8e5cd9','#e05a9b','#17a2b8','#6c757d','#ff8a5c','#5ab4f0','#9bc53d','#c9a227'];

/* options builders */
function catOptions(kind, selected) {
  const roots = (S.meta.categories || []).filter(c => !kind || c.kind === kind || (kind === 'refund' && c.kind === 'expense'));
  return `<option value="">— Category —</option>` + roots.map(r => `<optgroup label="${esc(r.name + ' ' + (r.name_zh || ''))}">` +
    `<option value="${r.id}" ${r.id == selected ? 'selected' : ''}>${esc(r.name)} (all) ${esc(r.name_zh || '')}</option>` +
    r.children.map(c => `<option value="${c.id}" ${c.id == selected ? 'selected' : ''}>${esc(c.name)} ${esc(c.name_zh || '')}</option>`).join('') + '</optgroup>').join('');
}
function acctOptions(selected, {blank = '— Account —', filter} = {}) {
  let a = (S.meta.accounts || []).filter(x => !x.archived || x.id == selected);
  if (filter) a = a.filter(filter);
  return (blank !== null ? `<option value="">${blank}</option>` : '') + a.map(x => `<option value="${x.id}" ${x.id == selected ? 'selected' : ''}>${esc(x.name)} (${x.currency})</option>`).join('');
}
function listOptions(list, selected, blank) { return `<option value="">${blank}</option>` + (list || []).map(x => `<option value="${x.id}" ${x.id == selected ? 'selected' : ''}>${esc(x.name)}</option>`).join(''); }
const CUR_SYM = {USD: '$', CNY: '¥', RMB: '¥', JPY: '¥', EUR: '€', GBP: '£', HKD: 'HK$', TWD: 'NT$', CAD: 'C$', AUD: 'A$', SGD: 'S$', KRW: '₩', INR: '₹', CHF: 'Fr', MXN: 'MX$', THB: '฿', BTC: '₿'};
const curLabel = c => `${CUR_SYM[c] ? CUR_SYM[c] + ' ' : ''}${c}`;  // "$ USD", "¥ CNY", "¥ JPY"
function curOptions(selected) {
  const list = (S.meta.currencies || []).map(c => c.currency);
  if (selected && !list.includes(selected)) list.push(selected);
  return list.map(c => `<option value="${c}" ${c === selected ? 'selected' : ''}>${curLabel(c)}</option>`).join('');
}
/* balance-adjustment reasons 差额原因 */
const REASON_HELP = {
  investment_gain: 'Investment income (gain if positive, loss if negative). Not income/expense; counts in investment returns.',
  interest: 'Investment income. Not income/expense; counts in investment returns.',
  fees: 'Expense in reports — except on investment accounts, where it reduces investment income/returns.',
  unrecorded_spending: 'Counts as an expense in reports (labelled “Unrecorded spending 未记支出”).',
  unrecorded_income: 'Counts as income in reports (labelled “Unrecorded income 未记收入”).',
  correction: 'Excluded from reports (as before). For investment returns it counts as money moved in/out.',
  opening: 'Starting balance. Excluded from reports; for returns it counts as money moved in.'};
const isInvType = t => !!(S.meta.account_types[t] && S.meta.account_types[t].investment);
const defaultReason = acct => acct && isInvType(acct.type) ? 'investment_gain' : 'correction';
const reasonOptions = (sel, {opening = false} = {}) => (S.meta.adj_reasons || []).filter(r => opening || r.code !== 'opening' || sel === 'opening')
  .map(r => `<option value="${r.code}" ${r.code === sel ? 'selected' : ''}>${esc(r.name)} ${esc(r.name_zh)}</option>`).join('');
/* user preferences: server-side (/api/prefs, shared by phone and PC) with a localStorage fallback */
const PREFS_LS = 'txPrefs';
function txPrefs() {
  let ls = {}; try { ls = JSON.parse(localStorage.getItem(PREFS_LS) || '{}'); } catch (e) { /* ignore */ }
  const srv = (S.meta && S.meta.prefs) || {};
  return {quickadd_enter: srv.quickadd_enter || ls.quickadd_enter || 'add', tx_last: Object.assign({}, ls.tx_last || {}, srv.tx_last || {})};
}
function rememberLocal(kind, vals) {
  const p = txPrefs(); p.tx_last[kind] = Object.assign({}, p.tx_last[kind] || {}, vals);
  localStorage.setItem(PREFS_LS, JSON.stringify(p)); if (S.meta) S.meta.prefs = Object.assign({}, S.meta.prefs || {}, {tx_last: p.tx_last});
}
async function savePrefs(body) {
  const p = Object.assign(txPrefs(), body); localStorage.setItem(PREFS_LS, JSON.stringify(p));
  try { const r = await api('/api/prefs', {method: 'PUT', body}); if (S.meta) S.meta.prefs = r; } catch (e) { /* offline: localStorage keeps it */ }
}
function typeOptions(selected) { return Object.entries(S.meta.account_types).map(([k, v]) => `<option value="${k}" ${k === selected ? 'selected' : ''}>${esc(v.label)}</option>`).join(''); }

/* ---------- navigation ---------- */
const ROUTES = [
  ['home', '🏠', 'Home'], ['dashboard', '📊', 'Dashboard'], ['transactions', '🧾', 'Transactions'], ['accounts', '🏦', 'Accounts'],
  ['investments', '📈', 'Investments'], ['budgets', '🎯', 'Budgets'], ['scheduled', '🔁', 'Scheduled'], ['reimb', '🧾', 'Reimbursements'], ['reserves', '🔒', 'Reserves'], ['loans', '🤝', 'Loans'], ['reminders', '⏰', 'Reminders'], ['projects', '📁', 'Projects'], ['reports', '📉', 'Reports'],
  ['review', '📥', 'Quick add & Review'], ['settings', '⚙️', 'Settings'],
];
function renderNav() {
  let cur = (location.hash.slice(2) || 'home').split(/[/?]/)[0]; if (cur === 'account') cur = 'accounts';
  const rc = S.meta ? S.meta.review_count + (S.meta.reserve_review_count || 0) : 0;
  $('#nav').innerHTML = ROUTES.map(([k, i, l]) => `<a href="#/${k}" class="${k === cur ? 'active' : ''}"><span class="ico">${i}</span>${l}${k === 'review' && rc ? `<span class="badge">${rc}</span>` : ''}${k === 'reserves' && S.meta && S.meta.reserve_review_count ? `<span class="badge" title="Holds found in emails to review">${S.meta.reserve_review_count}</span>` : ''}${k === 'reminders' && S.meta && S.meta.reminder_count ? `<span class="badge">${S.meta.reminder_count}</span>` : ''}</a>`).join('');
  const mob = ['home', 'transactions', 'accounts', 'review', 'settings'];
  const moreBadge = S.meta ? (S.meta.reserve_review_count || 0) + (S.meta.reminder_count || 0) : 0;
  $('#mnav').innerHTML = ROUTES.filter(r => mob.includes(r[0])).map(([k, i, l]) => `<a href="#/${k}" class="${k === cur ? 'active' : ''}"><span class="ico">${i}</span>${l.split(' ')[0].replace('Quick', 'Review')}${k === 'review' && rc ? `<span class="badge">${rc}</span>` : ''}</a>`).join('')
    + `<a href="javascript:void 0" id="mnavMore" class="${mob.includes(cur) ? '' : 'active'}"><span class="ico">☰</span>More${moreBadge ? `<span class="badge">${moreBadge}</span>` : ''}</a>`;
  $('#mnavMore').onclick = mobileMore;
}
/* phone: every page that isn't on the bottom bar (Reserves, Investments, Budgets, Reminders, …) */
function mobileMore() {
  const mob = ['home', 'transactions', 'accounts', 'review', 'settings'], m = S.meta || {};
  const cur = (location.hash.slice(2) || 'home').split(/[/?]/)[0];
  const badge = k => k === 'reserves' && m.reserve_review_count ? m.reserve_review_count : k === 'reminders' && m.reminder_count ? m.reminder_count : 0;
  const bg = modal('More', `<div class="mnav-more">${ROUTES.filter(r => !mob.includes(r[0])).map(([k, i, l]) =>
    `<a href="#/${k}" class="${k === cur ? 'active' : ''}"><span class="ico">${i}</span><span>${l}</span>${badge(k) ? `<span class="badge">${badge(k)}</span>` : ''}</a>`).join('')}</div>`);
  $$('.mnav-more a', bg).forEach(a => a.onclick = () => bg.close());
}
function renderLedgers() {
  const sel = $('#ledgerSel');
  sel.innerHTML = `<option value="">All ledgers</option>` + S.meta.ledgers.map(l => `<option value="${l.id}" ${l.id == S.ledger ? 'selected' : ''}>📒 ${esc(l.name)}</option>`).join('');
}
const VIEWS = {};
const HIDDEN_ROUTES = [['account', '🏦', 'Account']];  // not in the sidebar (#/account/<id>)

/* Infinite scroll for cursor-paged lists (?before=<cursor>): an IntersectionObserver sentinel loads the next page,
   with a loading indicator and a "Load more" button as fallback. load(cursor) -> {items, has_more, next_cursor};
   append(page, isFirst) renders the rows. Returns {more(), done()} */
function infiniteList(anchor, {load, append, endHtml = 'No more transactions', margin = '600px'}) {
  let cursor = null, busy = false, done = false, first = true, dead = false;
  const foot = document.createElement('div'); foot.className = 'inf-foot';
  foot.innerHTML = '<div class="inf-spin" hidden><span class="spinner"></span> Loading… 加载中</div><button type="button" class="btn" hidden>Load more</button><div class="inf-end small muted" hidden></div><div class="inf-sentinel"></div>';
  anchor.after(foot);
  const [spin, btn, end, sent] = foot.children;
  const near = () => { const r = sent.getBoundingClientRect(); return r.top < innerHeight + 600 && r.bottom > -50 && foot.isConnected; };
  const next = async () => {
    if (busy || done || dead) return;
    busy = true; spin.hidden = false; btn.hidden = true;
    let p;
    try { p = await load(cursor); }
    catch (e) { busy = false; spin.hidden = true; btn.hidden = false; toast(e.message, true); return; }
    if (dead) return;
    append(p, first); first = false;
    cursor = p.next_cursor; done = !p.has_more || !cursor;
    busy = false; spin.hidden = true; btn.hidden = done;
    end.hidden = !done || !p.items; end.innerHTML = done ? endHtml : '';
    if (!done && near()) setTimeout(next, 0);  // short pages: keep filling the screen
  };
  btn.onclick = next;
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) next(); }, {rootMargin: margin});
    io.observe(sent); (S.observers = S.observers || []).push(io);
    const kill = io.disconnect.bind(io); io.disconnect = () => { dead = true; kill(); };
  } else btn.hidden = false;
  next();
  return {more: next, done: () => done, foot};
}
function catIcon(t) {
  if (t.kind === 'transfer') return '🔁';
  if (t.kind === 'adjustment') return '⚖️';
  if (['buy', 'sell'].includes(t.kind)) return '📈';
  if (t.kind === 'dividend') return '💰';
  if (t.kind === 'invest_fee') return '🧾';
  const n = ((t.parent_category_name || t.category_name || '') + ' ' + (t.category_name || '')).toLowerCase();
  const map = [[/food|dining|meal|restaurant|grocer|snack|drink|coffee/, '🍜'], [/shop|cloth|electronic|beauty/, '🛍️'], [/hous|rent|util|home|propert/, '🏠'],
    [/transport|car|gas|fuel|taxi|parking|transit|travel/, '🚗'], [/entertain|movie|game|sport|hobby|vacation/, '🎮'], [/health|medic|pharm|doctor|fitness/, '💊'],
    [/educat|book|course|tuition/, '📚'], [/social|gift|red envelope|donat/, '🎁'], [/financ|insur|tax|fee|interest/, '🏦'], [/famil|kid|child|baby|pet/, '👪'],
    [/salary|work|wage|bonus|payroll/, '💼'], [/invest/, '📈']];
  for (const [re, ic] of map) if (re.test(n)) return ic;
  return t.kind === 'income' ? '💵' : t.kind === 'refund' ? '↩️' : t.kind === 'reimbursement' ? '💼' : '🧾';
}
async function route() {
  const [name, ...rest] = (location.hash.slice(2) || 'home').split('/');
  const r = ROUTES.find(x => x[0] === name) || HIDDEN_ROUTES.find(x => x[0] === name) || ROUTES[0];
  $('#title').textContent = r[2];
  document.body.classList.toggle('on-home', r[0] === 'home');
  renderNav();
  Object.values(S.charts).forEach(c => c.destroy()); S.charts = {};
  (S.observers || []).forEach(o => o.disconnect()); S.observers = [];
  // each navigation renders into a fresh container: a slower, older view finishing later writes into a detached
  // element instead of over the new page (and its missing elements no longer throw "Cannot set properties of null")
  const v = document.createElement('div'); v.className = 'view-root'; v.innerHTML = '<div class="empty">Loading…</div>';
  $('#view').replaceChildren(v);
  try { await VIEWS[r[0]](v, rest); } catch (e) { if (v.isConnected) v.innerHTML = `<div class="card empty">⚠️ ${esc(errText(e))}</div>`; else console.debug('stale view', r[0], e); }
}
async function refresh() { await loadMeta(); await route(); }
const demoBanner = () => S.meta.has_demo ? `<div class="demo-banner">🧪 <b>DEMO DATA loaded</b> — accounts and transactions marked [DEMO] are fake sample data. <span class="sp"></span><button class="btn sm" onclick="clearDemo()">Clear demo data</button></div>` : '';
async function clearDemo() { if (!await confirmBox('Remove all [DEMO] accounts, transactions and budgets?')) return; await guard(() => api('/api/demo/clear', {method: 'POST'})); toast('Demo data cleared'); refresh(); }
