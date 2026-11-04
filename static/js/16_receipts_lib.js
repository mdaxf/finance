/* Receipts library 小票库: every saved receipt photo, browsed by year / month / day (transaction date), and a viewer
   that previews the photo in the page (no download): zoom, previous / next (arrow keys, swipe), open the transaction */
function rcViewer(list, idx = 0, after) {
  if (!list || !list.length) return;
  let i = Math.max(0, Math.min(idx, list.length - 1)), zoom = false;
  const bg = modal('🧾 ' + esc(t('Receipt')), `<div class="rcv">
    <div class="rcv-img" id="rcvImg"><img alt="receipt"></div>
    <div class="row rcv-bar" style="justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px">
      <div class="rcv-info small" id="rcvInfo" style="min-width:0;flex:1 1 240px"></div>
      <div class="row" style="gap:6px;flex-wrap:wrap">
        <button type="button" class="btn sm" id="rcvPrev" title="${esc(t('Previous'))} (←)">◀</button>
        <span class="small muted" id="rcvPos"></span>
        <button type="button" class="btn sm" id="rcvNext" title="${esc(t('Next'))} (→)">▶</button>
        <button type="button" class="btn sm" id="rcvZoom">🔍 ${t('Zoom')}</button>
        <a class="btn sm" id="rcvOrig" target="_blank" rel="noopener" title="${esc(t('Open the full photo in a new tab'))}">↗</a>
        <button type="button" class="btn sm" id="rcvTx">${t('Open transaction')}</button>
      </div></div></div>`, {wide: true});
  const img = $('#rcvImg img', bg), box = $('#rcvImg', bg);
  const show = () => {
    const r = list[i];
    zoom = false; box.classList.remove('zoom');
    img.src = r.url;
    $('#rcvOrig', bg).href = r.url;
    $('#rcvPos', bg).textContent = list.length > 1 ? `${i + 1} / ${list.length}` : '';
    $('#rcvPrev', bg).disabled = i === 0; $('#rcvNext', bg).disabled = i === list.length - 1;
    $('#rcvPrev', bg).hidden = $('#rcvNext', bg).hidden = list.length < 2;
    $('#rcvTx', bg).hidden = !r.transaction_id;
    $('#rcvInfo', bg).innerHTML = r.date || r.merchant || r.amount != null ? `<b>${esc(r.merchant || t('Receipt'))}</b>
      ${r.amount != null ? ` · <b>${money(r.orig_amount ?? r.amount, r.orig_currency || r.currency)}</b>` : ''}
      <div class="muted">${[r.date, r.account_name, r.category_name, r.status && r.status !== 'posted' ? t('To review') : ''].filter(Boolean).map(esc).join(' · ')}</div>
      ${r.file_name ? `<div class="muted rcv-file">${esc(r.file_name)}</div>` : ''}` : '';
  };
  const go = d => { const n = i + d; if (n >= 0 && n < list.length) { i = n; show(); } };
  const toggleZoom = () => { zoom = !zoom; box.classList.toggle('zoom', zoom); };
  $('#rcvPrev', bg).onclick = () => go(-1); $('#rcvNext', bg).onclick = () => go(1);
  $('#rcvZoom', bg).onclick = toggleZoom; img.onclick = toggleZoom;
  $('#rcvTx', bg).onclick = () => { const tid = list[i].transaction_id; close(); openTx(tid, after || (() => route())); };
  const key = e => {
    if (e.key === 'ArrowLeft') go(-1); else if (e.key === 'ArrowRight') go(1);
    else if (e.key === 'Escape') { e.stopPropagation(); close(); }
  };
  let x0 = null;
  box.addEventListener('touchstart', e => { x0 = e.touches.length === 1 && !zoom ? e.touches[0].clientX : null; }, {passive: true});
  box.addEventListener('touchend', e => { if (x0 == null) return; const dx = e.changedTouches[0].clientX - x0; if (Math.abs(dx) > 50) go(dx < 0 ? 1 : -1); x0 = null; });
  document.addEventListener('keydown', key);
  const close0 = bg.close, close = () => { document.removeEventListener('keydown', key); close0(); };
  bg.close = close; $('[data-x]', bg).onclick = close;
  bg.addEventListener('mousedown', e => { if (e.target === bg) document.removeEventListener('keydown', key); });
  show();
}
/* a click on any receipt thumbnail (transaction form, …) previews it here instead of downloading; the photos next to
   it are the previous / next ones. Ctrl / middle click still opens the photo in a new tab. */
document.addEventListener('click', e => {
  const a = e.target.closest && e.target.closest('a.rc-thumb[data-rcv]');
  if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;
  e.preventDefault();
  const all = [...a.parentElement.querySelectorAll('a.rc-thumb[data-rcv]')];
  rcViewer(all.map(x => ({id: +x.dataset.rcv, url: x.getAttribute('href')})), all.indexOf(a));
});

const RL = {y: null, m: null, d: null, q: ''};
const rlMonth = (y, m) => new Date(+y, +m - 1, 1).toLocaleDateString(i18nLocale(), {month: 'long'});
const rlDay = s => new Date(s + 'T12:00').toLocaleDateString(i18nLocale(), {weekday: 'short', year: 'numeric', month: 'short', day: 'numeric'});
VIEWS.receipts = async (v, rest) => {
  if (rest && rest[0]) { [RL.y, RL.m, RL.d] = [rest[0] || null, rest[1] || null, rest[2] || null]; }
  v.innerHTML = `<div class="card" style="margin-bottom:12px"><div class="row" style="gap:10px;flex-wrap:wrap;align-items:center">
      <input type="search" id="rlQ" placeholder="${esc(t('Search merchant, amount, note or receipt text'))}" value="${esc(RL.q)}" style="flex:1 1 260px">
      <button type="button" class="btn pri" id="rlAdd">📷 ${t('Add receipt')}</button></div></div>
    <div class="rl-wrap"><div class="card rl-tree" id="rlTree"></div><div id="rlGrid"><div class="empty">${t('Loading…')}</div></div></div>`;
  $('#rlAdd', v).onclick = () => receiptCapture();
  let timer, seq = 0;
  const load = async () => {
    const my = ++seq;
    const p = new URLSearchParams([['limit', '300'], ...[['year', RL.y], ['month', RL.m], ['day', RL.d], ['q', RL.q], ['ledger_id', S.ledger]].filter(x => x[1])]);
    const d = await guard(() => api('/api/receipts/library?' + p));
    if (my !== seq || !v.isConnected) return;
    const path = ['receipts', RL.y, RL.m, RL.d].filter(Boolean).join('/');
    if (location.hash !== '#/' + path) history.replaceState(null, '', '#/' + path);
    renderTree(d.tree); renderGrid(d);
  };
  const pick = (y, m, d) => { [RL.y, RL.m, RL.d] = [y, m, d]; load(); };
  const node = (lvl, label, n, on, data) => `<button type="button" class="rl-node lv${lvl} ${on ? 'on' : ''}" ${data}><span>${label}</span><span class="muted small">${n}</span></button>`;
  const renderTree = tree => {
    const all = tree.reduce((s, y) => s + y.n, 0);
    $('#rlTree', v).innerHTML = node(0, esc(t('All receipts')), all, !RL.y, 'data-rl=""') + tree.map(y => node(0, (RL.y === y.y ? '▾ ' : '▸ ') + y.y, y.n, RL.y === y.y && !RL.m, `data-rl="${y.y}"`)
      + (RL.y === y.y ? y.months.map(m => node(1, (RL.m === m.m ? '▾ ' : '▸ ') + esc(rlMonth(y.y, m.m)), m.n, RL.m === m.m && !RL.d, `data-rl="${y.y}/${m.m}"`)
        + (RL.m === m.m ? m.days.map(d => node(2, esc(rlDay(`${y.y}-${m.m}-${d.d}`)), d.n, RL.d === d.d, `data-rl="${y.y}/${m.m}/${d.d}"`)).join('') : '')).join('') : '')).join('')
      + (tree.length ? '' : `<div class="small muted" style="padding:6px">${t('No receipts found')}</div>`);
    $$('[data-rl]', v).forEach(b => b.onclick = () => {
      const [y, m, d] = b.dataset.rl ? b.dataset.rl.split('/') : [];
      // a click on the open year / month closes it again
      if (!d && ((m && RL.m === m && !RL.d && RL.y === y) || (!m && y && RL.y === y && !RL.m))) return pick(m ? y : null, null, null);
      pick(y || null, m || null, d || null);
    });
  };
  const renderGrid = d => {
    const g = $('#rlGrid', v);
    const where = RL.d ? rlDay(`${RL.y}-${RL.m}-${RL.d}`) : RL.m ? `${rlMonth(RL.y, RL.m)} ${RL.y}` : RL.y || t('All receipts');
    if (!d.items.length) { g.innerHTML = `<div class="card empty">🧾 ${RL.q ? esc(t('No receipts match “{q}”', {q: RL.q})) : t('No receipts saved for this period yet. Add one with 📷 when you record a purchase.')}</div>`; return; }
    const days = [];
    d.items.forEach((r, i) => { if (!days.length || days[days.length - 1].date !== r.date) days.push({date: r.date, list: []}); days[days.length - 1].list.push([r, i]); });
    g.innerHTML = `<div class="row small muted" style="justify-content:space-between;margin:0 2px 8px"><span><b>${esc(where)}</b> · ${esc(t('{n} receipt(s)', {n: d.total}))}</span>
      ${d.truncated ? `<span>${esc(t('Showing the newest {n}: choose a month or day to see the rest', {n: d.items.length}))}</span>` : ''}</div>`
      + days.map(day => `<div class="rl-day"><div class="rl-dh">${esc(rlDay(day.date))}</div><div class="rl-grid">${day.list.map(([r, i]) => `
        <button type="button" class="rl-tile" data-rli="${i}" title="${esc(r.file_name || '')}">
          <img src="${r.thumb_url}" alt="receipt" loading="lazy">
          <span class="rl-cap"><b>${esc(r.merchant || t('Receipt'))}</b>${r.amount != null ? `<span>${money(r.orig_amount ?? r.amount, r.orig_currency || r.currency)}</span>` : ''}</span>
          ${r.status && r.status !== 'posted' ? `<span class="pill rl-st">${t('To review')}</span>` : ''}</button>`).join('')}</div></div>`).join('');
    $$('[data-rli]', g).forEach(b => b.onclick = () => rcViewer(d.items, +b.dataset.rli, load));
  };
  $('#rlQ', v).oninput = e => { clearTimeout(timer); timer = setTimeout(() => { RL.q = e.target.value.trim(); load(); }, 300); };
  await load();
};
