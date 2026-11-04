/* ---------- Receipt photos 小票: capture / pick / drop → client resize → local OCR on the server → pre-filled form ---------- */
const RC_MAX_SIDE = 2000;
async function rcResize(file) {
  // shrink big phone photos before upload; files the browser can't decode (e.g. HEIC outside Safari) go as-is
  try {
    const bmp = await createImageBitmap(file, {imageOrientation: 'from-image'});
    const k = Math.min(1, RC_MAX_SIDE / Math.max(bmp.width, bmp.height));
    if (k === 1 && file.size < 2.5e6 && /jpe?g|png|webp/i.test(file.type)) return file;
    const c = document.createElement('canvas'); c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
    c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
    const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.86));
    return blob ? new File([blob], (file.name || 'receipt').replace(/\.\w+$/, '') + '.jpg', {type: 'image/jpeg'}) : file;
  } catch (e) { return file; }
}
function rcUpload(files, onProgress, extra = {}) {
  return new Promise((resolve, reject) => {
    const fd = new FormData(); files.forEach(f => fd.append('file', f, f.name || 'receipt.jpg'));
    Object.entries(extra).forEach(([k, v]) => fd.append(k, v));
    const x = new XMLHttpRequest(); x.open('POST', '/api/receipts');
    x.upload.onprogress = e => e.lengthComputable && onProgress(e.loaded / e.total);
    x.upload.onload = () => onProgress(1);
    x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch (e) { /* ignore */ }
      x.status < 300 ? resolve(j) : reject(new Error(j.detail || (x.status === 401 ? 'Not signed in — reload the page' : x.status === 413 ? 'Photo too large' : 'Upload failed (HTTP ' + x.status + ')'))); };
    x.onerror = () => reject(new Error('Network error'));
    x.send(fd);
  });
}
/* Shows previews + progress, uploads, OCRs. Resolves with the /api/receipts response (draft, receipt ids) or null. */
async function rcProcess(fileList) {
  const files = [...fileList].filter(f => !f.type || /^image\//.test(f.type) || /\.(heic|heif|jpe?g|png|webp)$/i.test(f.name || '') || f.type === 'application/octet-stream');
  if (!files.length) { toast('Please choose image files (JPEG, PNG, HEIC)', true); return null; }
  const bg = modal('Receipt', `<div class="rc-prev">${files.map(() => '<div class="rc-ph"></div>').join('')}</div>
    <div class="rc-prog"><div class="bar"><i id="rcBar" style="width:2%"></i></div><div class="small muted" id="rcStep">Preparing</div></div>`);
  files.forEach((f, i) => { const u = URL.createObjectURL(f); const im = new Image(); im.onload = () => URL.revokeObjectURL(u); im.onerror = () => { $$('.rc-ph', bg)[i].textContent = '🧾 ' + (f.name || 'photo'); }; im.src = u; $$('.rc-ph', bg)[i].appendChild(im); });
  const step = (t, p) => { $('#rcStep', bg).textContent = t; if (p !== undefined) $('#rcBar', bg).style.width = Math.round(p * 100) + '%'; };
  try {
    const small = []; for (const f of files) small.push(await rcResize(f));
    step('Uploading', .05);
    let ocrTimer;
    const res = await rcUpload(small, p => { step(p < 1 ? `Uploading ${Math.round(p * 100)}%` : 'Reading the receipt (OCR on your server)…', .05 + p * .45);
      if (p >= 1 && !ocrTimer) { let q = .5; ocrTimer = setInterval(() => { q = Math.min(.95, q + .04); $('#rcBar', bg).style.width = q * 100 + '%'; }, 400); } });
    clearInterval(ocrTimer); step('Done', 1);
    bg.close();
    if (res.ocr && !res.ocr.available) toast(res.ocr.message, true);
    return res;
  } catch (e) { bg.close(); toast(e.message, true); return null; }
}
/* chooser: camera (mobile), files, drag & drop (desktop) */
function rcPick(onFiles) {
  const bg = modal('Add receipt photo', `<div class="rc-drop" id="rcDrop"><div style="font-size:34px">🧾</div><div>Drop receipt photos here</div><div class="small muted">JPEG / PNG / HEIC · several photos of one long receipt are fine</div></div>
    <div class="row" style="justify-content:center;margin-top:12px"><label class="btn pri">📷 Take photo<input type="file" accept="image/*" capture="environment" hidden id="rcCam"></label>
    <label class="btn">🖼 Choose files<input type="file" accept="image/*,.heic,.heif" multiple hidden id="rcFiles"></label></div>
    <p class="small muted" style="margin-bottom:0">Text is read on your own server (Tesseract). Nothing is saved until you confirm the form.</p>`);
  const go = fl => { if (fl && fl.length) { bg.close(); onFiles(fl); } };
  $('#rcCam', bg).onchange = e => go(e.target.files); $('#rcFiles', bg).onchange = e => go(e.target.files);
  const dz = $('#rcDrop', bg);
  dz.ondragover = e => { e.preventDefault(); dz.classList.add('on'); }; dz.ondragleave = () => dz.classList.remove('on');
  dz.ondrop = e => { e.preventDefault(); go(e.dataTransfer.files); };
}
function rcPreset(res) {
  const d = res.draft || {}; const p = {kind: 'expense'};
  for (const k of ['amount', 'currency', 'merchant', 'category_id', 'account_id', 'note', 'tax_amount', 'tip_amount']) if (d[k] !== null && d[k] !== undefined && d[k] !== '') p[k] = d[k];
  if (d.date) p.date = d.date.length > 10 ? d.date : d.date + ' 12:00';
  return p;
}
/* entry point from the quick-add bar / home: photo → OCR → pre-filled form */
async function receiptCapture(files) {
  if (!files) return rcPick(receiptCapture);
  const res = await rcProcess(files); if (!res) return;
  const conf = Math.round((res.confidence || 0) * 100);
  openTx(null, null, rcPreset(res), {receipt_ids: res.receipt_ids, receipts: res.receipts, uncertain: res.uncertain, raw_text: res.ocr_text,
    banner: `🧾 From receipt (${res.ocr && res.ocr.available ? conf + '% sure' : 'no OCR: fill in by hand'}). Check and press Save; nothing is recorded until then.`});
}
const rcThumbsHtml = list => (list || []).map(r => `<a class="rc-thumb" href="${r.url}" target="_blank" data-rcv="${r.id}" title="Preview the photo"><img src="${r.thumb_url}" alt="receipt" loading="lazy"></a>`).join('');
/* drag & drop a photo onto the quick-add bar (desktop) */
(fn => document.readyState === 'loading' ? document.addEventListener('DOMContentLoaded', fn) : fn())(() => {
  const qa = $('#qaForm'); if (!qa) return;
  if (!$('#qaCam')) $('#qaFormBtn').insertAdjacentHTML('beforebegin', '<button type="button" class="btn" id="qaCam" title="Receipt photo (or drop an image here)">📷</button>');
  $('#qaCam').onclick = () => receiptCapture();
  qa.addEventListener('dragover', e => { if ([...e.dataTransfer.items].some(i => i.kind === 'file')) { e.preventDefault(); qa.classList.add('drop'); } });
  qa.addEventListener('dragleave', () => qa.classList.remove('drop'));
  qa.addEventListener('drop', e => { if (e.dataTransfer.files.length) { e.preventDefault(); qa.classList.remove('drop'); receiptCapture(e.dataTransfer.files); } });
});
