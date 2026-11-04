"""Receipt photos 小票: local OCR (Tesseract, optional) -> draft transaction, and image storage.

Files live under the receipts folder (setting `receipts_dir`; blank = <data>/receipts, may point at a NAS / synced
Google Drive folder) in sub-folders from `receipts_pattern` (default {yyyy}/{mm}/{dd}), named
YYYYMMDD_HHMMSS_<merchant-slug>_<amount>_<id>.<ext>. The DB stores only the path RELATIVE to the receipts folder, so
the folder can be moved (change the setting). Files follow their transaction: a date/merchant/amount change renames
or moves them. Nothing here talks to the network: OCR runs on this machine or not at all.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

from .db import Database

MAX_BYTES = 15 * 1024 * 1024       # per image
MAX_FILES = 6
MAX_PIXELS = 60_000_000
DEFAULT_PATTERN = "{yyyy}/{mm}/{dd}"
PATTERN_TOKENS = ("{yyyy}", "{mm}", "{dd}")
EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "BMP": "bmp", "TIFF": "tif"}


class ReceiptError(ValueError):
    pass


# ---------------------------------------------------------------- OCR (optional)
def _tesseract_cmd() -> str | None:
    cmd = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")
    if not cmd and os.name == "nt":
        for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if Path(p).exists():
                return p
    return cmd


def ocr_status() -> dict:
    """{available, version, languages, message}. OCR is optional: without it receipts are still stored and the
    form opens empty for manual entry."""
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return {"available": False, "languages": [], "message": "OCR packages missing (Pillow/pytesseract): run setup.bat on Windows or .venv/bin/pip install -r requirements.txt, then restart. Photos are still saved; enter the details by hand."}
    cmd = _tesseract_cmd()
    if not cmd:
        return {"available": False, "languages": [], "message": "Tesseract OCR not found on this machine (Linux: apt install tesseract-ocr tesseract-ocr-chi-sim; Windows: winget install UB-Mannheim.TesseractOCR). Photos are still saved; enter the details by hand."}
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = cmd
    try:
        ver = str(pytesseract.get_tesseract_version())
        langs = [l for l in pytesseract.get_languages(config="") if l != "osd"]
    except Exception as e:  # noqa: BLE001
        return {"available": False, "languages": [], "message": f"Tesseract failed to run: {e}"}
    msg = None if "chi_sim" in langs else "Chinese OCR data (chi_sim) missing: Chinese receipts will read poorly."
    return {"available": True, "version": ver, "languages": langs, "message": msg}


def _preprocess(img, variant: str = "plain", scale: float = 1.0):
    """Grayscale image prepared for Tesseract. Variants: plain (autocontrast), denoise (median), binar (OpenCV deskew +
    CLAHE contrast + adaptive threshold; falls back to PIL autocontrast+threshold when OpenCV isn't installed)."""
    from PIL import ImageFilter, ImageOps
    g = ImageOps.exif_transpose(img).convert("L")
    target = 1600 * scale  # Tesseract likes ~30px glyphs: upscale small photos (multi-scale via `scale`)
    if max(g.size) < target:
        f = target / max(g.size)
        g = g.resize((int(g.width * f), int(g.height * f)))
    if variant == "denoise":  # median filter removes speckle (but blurs small clean text)
        g = g.filter(ImageFilter.MedianFilter(3))
    if variant == "binar":
        try:
            import cv2
            import numpy as np
            a = np.array(g)
            a = cv2.fastNlMeansDenoising(a, None, 10, 7, 21)
            a = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(a)
            a = _deskew(cv2, np, a)
            a = cv2.adaptiveThreshold(a, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15)
            a = _despeckle(cv2, np, cv2.medianBlur(a, 3))
            from PIL import Image
            return Image.fromarray(a)
        except Exception:  # noqa: BLE001  no OpenCV / odd image: PIL-only binarization
            g = ImageOps.autocontrast(g.filter(ImageFilter.MedianFilter(3)), cutoff=2)
            return g.point(lambda v: 255 if v > 150 else 0)
    return ImageOps.autocontrast(g)


def _despeckle(cv2, np, a):
    """Drop isolated dark specks (paper texture, sensor noise) much smaller than a period at this resolution."""
    n, lab, stats, _ = cv2.connectedComponentsWithStats(255 - a, connectivity=8)
    min_area = max(4, int((a.shape[0] / 300) ** 2))
    small = np.where(stats[1:, cv2.CC_STAT_AREA] < min_area)[0] + 1
    if len(small):
        a = a.copy(); a[np.isin(lab, small)] = 255
    return a


def _deskew(cv2, np, a):
    """Rotate a slightly tilted receipt photo straight (angle of the text pixels' min-area rectangle, |angle| ≤ 15°)."""
    inv = cv2.threshold(a, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    pts = np.column_stack(np.where(inv > 0))
    if len(pts) < 50:
        return a
    ang = cv2.minAreaRect(pts[:, ::-1].astype(np.float32))[-1]
    ang = ang - 90 if ang > 45 else ang
    if abs(ang) < 0.5 or abs(ang) > 15:
        return a
    h, w = a.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
    return cv2.warpAffine(a, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def ocr_image(img, denoise: bool = False, psm: int = 4, variant: str | None = None, scale: float = 1.0) -> tuple[str, float | None]:
    """(text, mean word confidence 0..1) for a PIL image; ('', None) when OCR is unavailable."""
    if img is None:
        return "", None
    st = ocr_status()
    if not st["available"]:
        return "", None
    import pytesseract
    g = _preprocess(img, variant or ("denoise" if denoise else "plain"), scale)
    langs = "+".join(l for l in ("eng", "chi_sim") if l in st["languages"]) or "eng"
    data = pytesseract.image_to_data(g, lang=langs, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
    lines: dict = {}
    confs = []
    for i, w in enumerate(data["text"]):
        if not str(w).strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(str(w))
        try:
            c = float(data["conf"][i])
            if c >= 0:
                confs.append(c)
        except (TypeError, ValueError):
            pass
    text = "\n".join(_join_words(ws) for _, ws in sorted(lines.items()))
    return text, (round(sum(confs) / len(confs) / 100, 2) if confs else None)


# (variant, psm, scale) tried in order; stops early once a read is clearly good
OCR_PASSES = [("plain", 4, 1.0), ("plain", 6, 1.0), ("binar", 4, 1.0), ("denoise", 4, 1.0), ("binar", 6, 1.0),
              ("plain", 11, 1.0), ("plain", 4, 2.0)]


def _score(parsed: dict, ocr_conf: float | None) -> float:
    return parsed["confidence"] + 0.25 * (ocr_conf or 0) + (0.1 if parsed.get("total") is not None else 0)


def ocr_best(img, max_passes: int | None = None) -> tuple[str, float | None, dict]:
    """Multi-pass OCR (preprocessing variants × page-segmentation modes × scales); keeps the pass whose parsed
    result scores best (receipt-field confidence + OCR word confidence). Returns (text, ocr_conf, info)."""
    if img is None or not ocr_status()["available"]:
        return "", None, {"passes": 0}
    best = ("", None, -1.0, None)
    tried = []
    for n, (var, psm, sc) in enumerate(OCR_PASSES[:max_passes or len(OCR_PASSES)]):
        try:
            text, conf = ocr_image(img, psm=psm, variant=var, scale=sc)
        except Exception:  # noqa: BLE001  one failing pass shouldn't sink the others
            continue
        p = parse_receipt(text) if text else None
        sc_ = _score(p, conf) if p else 0.0
        tried.append(f"{var}/psm{psm}/x{sc:g}={sc_:.2f}")
        if sc_ > best[2]:
            best = (text, conf, sc_, f"{var}/psm{psm}/x{sc:g}")
        if p and p["confidence"] >= 0.8 and p.get("checked") and (conf or 0) >= 0.75:
            break  # totals cross-check and words read cleanly: good enough
        if n >= 1 and best[2] >= 1.05:
            break
    return best[0], best[1], {"passes": len(tried), "best": best[3], "tried": tried}


def _join_words(ws: list[str]) -> str:
    out = ""
    for w in ws:  # no spaces between CJK characters
        if out and not (re.match(r"[\u3400-\u9fff]", out[-1]) and re.match(r"[\u3400-\u9fff]", w[0])):
            out += " "
        out += w
    return out


# ---------------------------------------------------------------- text -> fields
TOTAL_KW = re.compile(r"(grand\s*total|total\s*due|amount\s*due|balance\s*due|total\s*amount|\btotal\b|\bamount\b|"
                      r"实付金额|实付|实收|应付|合计|总计|总额|金额合计|付款金额|支付金额|小计金额)", re.I)
STRONG_KW = re.compile(r"(grand\s*total|total\s*due|amount\s*due|balance\s*due|实付|应付|合计|总计|付款金额|支付金额)", re.I)
SKIP_KW = re.compile(r"(sub\s*-?\s*total|小计|\btax\b|\bvat\b|\bgst\b|税|\btip\b|gratuity|小费|change|找零|cash\b|现金|"
                     r"discount|优惠|折扣|saving|you saved|points|积分|card\s*#|\bauth|balance\s*(?!due))", re.I)
AMT_RX = re.compile(r"(?<![\d.])(?:[$¥￥€£]\s?)?(\d{1,3}(?:[,，]\d{3})+|\d+)[.．·](\d{2})(?!\d)")
CUR_SYM = [("¥", "CNY"), ("￥", "CNY"), ("元", "CNY"), ("RMB", "CNY"), ("CNY", "CNY"), ("人民币", "CNY"),
           ("€", "EUR"), ("EUR", "EUR"), ("£", "GBP"), ("$", "USD"), ("USD", "USD")]
DATE_RXS = [
    (re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})"), "ymd"),
    (re.compile(r"(\d{1,2})[/-](\d{1,2})[/-](20\d{2}|\d{2})(?!\d)"), "mdy"),
    (re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+(20\d{2})"), "mon"),
    (re.compile(r"(?i)\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(20\d{2})"), "dmon"),
]
TIME_RX = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?::[0-5]\d)?\s*(am|pm|AM|PM)?")
MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
NOT_MERCHANT = re.compile(r"(receipt|invoice|发票|小票|欢迎|welcome|thank|谢谢|tel|phone|电话|www\.|http|\d{3}[-\s]\d{3,4}|"
                          r"order|订单|date|日期|time|时间|cashier|收银|store\s*#|地址|address)", re.I)


SUBTOTAL_RX = re.compile(r"(sub\s*-?\s*total|小\s*计)", re.I)
TAX_RX = re.compile(r"(\b(?:sales\s*|state\s*|city\s*|local\s*)?tax(?:es)?\b|\bvat\b|\bgst\b|\bhst\b|\bpst\b|\bqst\b|税\s*额|税\s*金|税\s*费|增值税|(?<![免含])税(?![号务]))", re.I)
TAX_NOT = re.compile(r"(before\s*tax|pre-?\s*tax|incl\.?\s*tax|including\s*tax|excl\.?\s*tax|tax\s*(id|no|invoice|#|exempt)|含税|不含税|税号|税务)", re.I)
TIP_RX = re.compile(r"(\btips?\b|gratuity|小\s*费)", re.I)
TIP_NOT = re.compile(r"(suggest|guide|建议|tip\s*not|no\s*tip|\btip\s*%?\s*options?)", re.I)
SVC_RX = re.compile(r"(service\s*(?:charge|fee)|svc\s*chg|服务费)", re.I)


def _line_amount(lines: list[str], i: int) -> float | None:
    """Last amount on line i, or on the next line when the label stands alone (and that line isn't another label)."""
    amts = [_amt(m) for m in AMT_RX.finditer(lines[i])]
    if not amts and i + 1 < len(lines) and not (TOTAL_KW.search(lines[i + 1]) or SKIP_KW.search(lines[i + 1])):
        amts = [_amt(m) for m in AMT_RX.finditer(lines[i + 1])]
    return amts[-1] if amts else None


def _breakdown(lines: list[str]) -> dict:
    """Subtotal / tax / tip / service charge actually printed on the receipt (None when not found, never 0 by default)."""
    out: dict = {"subtotal": None, "tax": None, "tip": None, "service": None}
    taxes = []
    for i, l in enumerate(lines):
        if SUBTOTAL_RX.search(l):
            v = _line_amount(lines, i)
            if v is not None:
                out["subtotal"] = v
            continue
        if SVC_RX.search(l):
            v = _line_amount(lines, i)
            if v is not None:
                out["service"] = round((out["service"] or 0) + v, 2)
            continue
        if TIP_RX.search(l) and not TIP_NOT.search(l) and l.count("%") <= 1:
            v = _line_amount(lines, i)
            if v is not None:
                out["tip"] = v
            continue
        is_total = TOTAL_KW.search(l) and not re.search(r"(total\s*tax|tax\s*total)", l, re.I)
        if TAX_RX.search(l) and not TAX_NOT.search(l) and not is_total:
            v = _line_amount(lines, i)
            if v is not None:
                taxes.append(v)
    if taxes:  # several tax lines (state + city…) add up; a printed "total tax" equal to their sum is not double-counted
        out["tax"] = round(max(taxes) if len(taxes) > 1 and abs(max(taxes) * 2 - sum(taxes)) < 0.011 else sum(taxes), 2)
    return out


def _amt(m: re.Match) -> float:
    return float(m.group(1).replace(",", "").replace("，", "") + "." + m.group(2))


def parse_receipt(text: str, today: datetime | None = None) -> dict:
    """Best-effort fields from OCR text: total, currency, date (+time), merchant, confidence 0..1 and reasons."""
    today = today or datetime.now()
    t = unicodedata.normalize("NFKC", text or "")
    lines = [re.sub(r"\s+", " ", l).strip() for l in t.splitlines()]
    lines = [l for l in lines if l]
    reasons, conf = [], 0.0
    # --- total: amount on (or right after) a TOTAL-like line, skipping subtotal/tax/tip/change lines
    cands = []
    for i, l in enumerate(lines):
        if TOTAL_KW.search(l) and not (SKIP_KW.search(TAX_NOT.sub(" ", l)) and not STRONG_KW.search(l)):
            amts = [_amt(m) for m in AMT_RX.finditer(l)]
            if not amts and i + 1 < len(lines) and not TOTAL_KW.search(lines[i + 1]):
                amts = [_amt(m) for m in AMT_RX.finditer(lines[i + 1])]
            if amts:
                cands.append((2 if STRONG_KW.search(l) else 1, amts[-1], i, l))
    total = None
    if cands:
        best = max(cands, key=lambda c: (c[0], c[1], c[2]))  # strongest keyword, then the largest such amount
        total = best[1]; conf += 0.45 if best[0] == 2 else 0.35; reasons.append(f"total from '{best[3][:40]}'")
    else:
        allamts = [(_amt(m), l) for l in lines if not SKIP_KW.search(l) for m in AMT_RX.finditer(l)]
        if allamts:
            total = max(a for a, _ in allamts); conf += 0.15; reasons.append("total = largest amount (no TOTAL line)")
    # --- subtotal / tax / tip / service charge, and the sum cross-check
    bd = _breakdown(lines)
    parts = [bd[k] for k in ("tax", "tip", "service")]
    checked = False
    if bd["subtotal"] is not None and any(x is not None for x in parts):
        expect = round(bd["subtotal"] + sum(x or 0 for x in parts), 2)
        no_tip = round(expect - (bd["tip"] or 0), 2)
        if total is not None and abs(expect - total) <= 0.021:
            checked = True; conf += 0.15; reasons.append("subtotal + tax/tip adds up to the total ✓")
        elif total is not None and bd["tip"] is not None and abs(no_tip - total) <= 0.021:
            total = expect; checked = True; conf += 0.1  # tip written after the printed total: the charge includes it
            reasons.append("printed total excludes the tip: total = total + tip")
        elif total is None:
            total = expect; conf += 0.2; reasons.append("total = subtotal + tax/tip (no TOTAL line)")
        elif total is not None:
            conf -= 0.05; reasons.append(f"subtotal + tax/tip = {expect} ≠ total {total}: check the amounts")
    elif total is not None and bd["subtotal"] is not None and abs(bd["subtotal"] - total) <= 0.021 and not any(parts):
        checked = True; conf += 0.05
    if total is not None and (bd["tax"] or 0) + (bd["tip"] or 0) + (bd["service"] or 0) > total + 0.01:
        bd.update(tax=None, tip=None, service=None); reasons.append("tax/tip larger than the total: ignored")
    for k in ("tax", "tip", "service"):
        if bd[k] is not None:
            reasons.append(f"{k} {bd[k]}")
    # --- currency
    cur = None
    for sym, code in CUR_SYM:
        if sym in t:
            cur = code; break
    if not cur and re.search(r"[\u3400-\u9fff]", t):
        cur = "CNY"
    if cur:
        reasons.append(f"currency {cur}")
    # --- date / time
    date_s = None
    for rx, kind in DATE_RXS:
        m = rx.search(t)
        if not m:
            continue
        try:
            if kind == "ymd":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif kind == "mdy":
                a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                y += 2000 if y < 100 else 0
                mo, d = (a, b) if a <= 12 else (b, a)
            elif kind == "mon":
                mo, d, y = MONTHS.index(m.group(1).lower()[:3]) + 1, int(m.group(2)), int(m.group(3))
            else:
                d, mo, y = int(m.group(1)), MONTHS.index(m.group(2).lower()[:3]) + 1, int(m.group(3))
            dt = datetime(y, mo, d)
        except (ValueError, IndexError):
            continue
        if dt.date() <= today.date() and (today - dt).days < 3650:
            date_s = dt.strftime("%Y-%m-%d"); conf += 0.15; reasons.append(f"date {date_s}")
            tm = TIME_RX.search(t[m.end():m.end() + 40]) or TIME_RX.search(t)
            if tm:
                h, mi = int(tm.group(1)), int(tm.group(2))
                ap = (tm.group(3) or "").lower()
                h = h % 12 + (12 if ap == "pm" else 0) if ap else h
                date_s += f" {h:02d}:{mi:02d}"
            break
    # --- merchant: first meaningful line near the top
    merchant = None
    for l in lines[:6]:
        clean = re.sub(r"[^\w\u3400-\u9fff&'\[\] .-]", "", l).strip(" .-")
        if len(re.sub(r"[\W\d_]", "", clean)) >= 2 and not NOT_MERCHANT.search(l) and not AMT_RX.search(l):
            merchant = clean[:60]; conf += 0.15; reasons.append(f"merchant '{merchant}' (top line)"); break
    return {"total": total, "currency": cur, "date": date_s, "merchant": merchant, "confidence": round(max(min(conf, 0.95), 0), 2),
            "subtotal": bd["subtotal"], "tax": bd["tax"], "tip": bd["tip"], "service": bd["service"], "checked": checked,
            "reasons": reasons, "lines": lines[:60]}


# ---------------------------------------------------------------- storage
def base_dir(db: Database) -> Path:
    s = (db.get_setting("receipts_dir") or "").strip()
    return Path(os.path.expanduser(s)) if s else Path(db.path).parent / "receipts"


def validate_pattern(p: str) -> str:
    p = (p or "").strip().strip("/\\") or DEFAULT_PATTERN
    rest = p
    for tok in PATTERN_TOKENS:
        rest = rest.replace(tok, "")
    if ".." in p or p.startswith(("/", "\\")) or re.search(r"[:*?\"<>|{}]", rest):
        raise ReceiptError("Folder pattern: use {yyyy}, {mm}, {dd}, letters and '/' only, e.g. {yyyy}/{mm}/{dd} or {yyyy}-{mm}")
    return p


def slug(s: str | None, n: int = 30) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip()
    s = re.sub(r"[^\w\u3400-\u9fff]+", "-", s, flags=re.U).strip("-_").lower()
    return (s[:n].strip("-") or "receipt")


def rel_path_for(db: Database, rid: int, when: str | None, merchant: str | None, amount, ext: str) -> str:
    try:
        dt = datetime.strptime((when or "")[:16].replace("T", " "), "%Y-%m-%d %H:%M") if when and len(when) >= 16 else \
            datetime.strptime((when or "")[:10], "%Y-%m-%d")
    except ValueError:
        dt = datetime.now()
    pat = validate_pattern(db.get_setting("receipts_pattern") or DEFAULT_PATTERN)
    folder = pat.replace("{yyyy}", f"{dt:%Y}").replace("{mm}", f"{dt:%m}").replace("{dd}", f"{dt:%d}")
    amt = "" if amount in (None, "") else f"{abs(float(amount)):.2f}".replace(".", "-")
    name = f"{dt:%Y%m%d_%H%M%S}_{slug(merchant)}" + (f"_{amt}" if amt else "") + f"_{rid}.{ext}"
    return f"{folder}/{name}"


def abs_path(db: Database, rel: str) -> Path:
    base = base_dir(db).resolve()
    p = (base / rel).resolve()
    if base not in p.parents:
        raise ReceiptError("Bad receipt path")
    return p


def load_image(data: bytes, name: str = ""):
    """Validates & opens an upload (JPEG/PNG/WebP/GIF/BMP/TIFF; HEIC via pillow-heif if installed).
    Returns (PIL image, bytes to store, extension)."""
    if not data:
        raise ReceiptError("Empty file")
    if len(data) > MAX_BYTES:
        raise ReceiptError(f"Image too large (max {MAX_BYTES // 1024 // 1024} MB)")
    try:
        from PIL import Image
    except ImportError:  # no Pillow (e.g. an updated install without re-running setup): store the photo, skip OCR
        if not looks_like_image(data):
            raise ReceiptError("Not a supported image (JPEG, PNG, WebP, HEIC)")
        return None, data, magic_ext(data)
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    heic = data[4:12] in (b"ftypheic", b"ftypheix", b"ftyphevc", b"ftypmif1", b"ftypmsf1") or name.lower().endswith((".heic", ".heif"))
    if heic:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            raise ReceiptError("HEIC photos need pillow-heif on the server: please use JPEG (iPhone: Settings → Camera → Formats → Most Compatible)")
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise ReceiptError("Not a supported image (JPEG, PNG, WebP, HEIC)")
    if heic or img.format not in EXT:  # store as JPEG
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=88)
        return img, buf.getvalue(), "jpg"
    return img, data, EXT[img.format]


def decode_b64(s: str) -> bytes:
    s = re.sub(r"^data:[^,]*,", "", (s or "").strip())
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        raise ReceiptError("Invalid base64 image")


IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"BM", b"II*\x00", b"MM\x00*")


def magic_ext(b: bytes) -> str:
    if b.startswith(b"\x89PNG"):
        return "png"
    if b.startswith(b"GIF8"):
        return "gif"
    if b[:4] == b"RIFF":
        return "webp"
    if b[4:8] == b"ftyp":
        return "heic"
    if b.startswith(b"BM"):
        return "bmp"
    if b.startswith((b"II*\x00", b"MM\x00*")):
        return "tif"
    return "jpg"


def looks_like_image(b: bytes) -> bool:
    """Sniff the bytes (ignores filename / content type, which Shortcuts often gets wrong)."""
    if not b or len(b) < 12:
        return False
    return b.startswith(IMAGE_MAGIC) or (b[:4] == b"RIFF" and b[8:12] == b"WEBP") or b[4:8] == b"ftyp"


def _maybe_b64_image(v) -> bytes | None:
    if isinstance(v, bytes):
        try:
            v = v.decode("ascii")
        except UnicodeDecodeError:
            return None
    if not isinstance(v, str) or len(v) < 64:
        return None
    try:
        b = decode_b64(re.sub(r"\s+", "", v))
    except ReceiptError:
        return None
    return b if looks_like_image(b) else None


PARAM_KEYS = ("auto_post", "account_id", "ledger_id", "filename")


def extract_upload(raw: bytes, ctype: str) -> tuple[list[tuple[bytes, str]], dict]:
    """Every image in a request, whatever the shape: multipart with any field name (file or text part, with or
    without filename/content type), raw image body (image/*, octet-stream, or none), or JSON with base64 in
    image/images/file/photo/any value. Returns ([(bytes, filename)], params)."""
    items, params = [], {}
    ct = (ctype or "").lower()
    if not raw:
        return items, params
    if ct.startswith("multipart/"):
        import email.parser
        import email.policy
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + ctype.encode("latin-1", "replace") + b"\r\n\r\n" + raw)
        for part in msg.iter_parts() if msg.is_multipart() else []:
            name = part.get_param("name", header="content-disposition") or ""
            fname = part.get_filename() or ""
            data = part.get_payload(decode=True) or b""
            if looks_like_image(data):
                items.append((data, fname))
            elif name.lower() in PARAM_KEYS:
                params[name.lower()] = data.decode("utf-8", "replace").strip()
            elif (b := _maybe_b64_image(data)) is not None:
                items.append((b, fname))
            elif fname or len(data) > 1024:
                items.append((data, fname))  # unknown file: let load_image give a clear error (e.g. a PDF)
        return items, params
    if ct.startswith("application/x-www-form-urlencoded"):  # Shortcuts "Form" with Text fields (base64)
        from urllib.parse import parse_qsl
        for k, v in parse_qsl(raw.decode("latin-1"), keep_blank_values=True):
            if k.lower() in PARAM_KEYS:
                params[k.lower()] = v.strip()
            elif (b := _maybe_b64_image(v)) is not None:
                items.append((b, ""))
        return items, params
    if ct.startswith("application/json") or raw[:1] in (b"{", b"["):
        try:
            body = json.loads(raw)
        except ValueError:
            raise ReceiptError("Body looks like JSON but isn't valid JSON")
        vals = body if isinstance(body, list) else []
        if isinstance(body, dict):
            params = {k: body[k] for k in PARAM_KEYS if body.get(k) not in (None, "")}
            for k, v in body.items():
                if k in PARAM_KEYS:
                    continue
                vals += v if isinstance(v, list) else [v]
        for v in vals:
            if (b := _maybe_b64_image(v)) is not None:
                items.append((b, str(params.get("filename") or "")))
        if not items and any(isinstance(v, str) and len(v) > 64 for v in vals):
            raise ReceiptError("Got JSON but the image isn't valid base64 (Shortcuts: use Base64 Encode on the photo)")
        return items, params
    if (b := _maybe_b64_image(raw)) is not None:  # text/plain base64 body
        return [(b, "")], params
    return [(raw, "")], params  # raw image body (image/*, octet-stream, missing content type)


def _dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def save_upload(db: Database, data: bytes, name: str = "") -> dict:
    """Stores one image (dated today until linked), runs OCR. Returns the receipt row + parsed fields."""
    img, blob, ext = load_image(data, name)
    text, ocr_conf, info = ocr_best(img)  # several preprocess/psm passes, best-scoring read wins
    parsed = parse_receipt(text) if text else {"total": None, "currency": None, "date": None, "merchant": None,
                                                 "confidence": 0.0, "reasons": [], "lines": []}
    if info.get("best"):
        parsed["ocr_pass"] = info["best"]
    with db.tx() as con:
        rid = con.execute("""INSERT INTO receipts(orig_name, mime, size, sha256, ocr_text, ocr_confidence, parsed, rel_path)
            VALUES(?,?,?,?,?,?,?,'')""", (name[:200], f"image/{'jpeg' if ext == 'jpg' else ext}", len(blob),
                                         hashlib.sha256(blob).hexdigest(), text[:20000], ocr_conf, _dumps(parsed))).lastrowid
    rel = rel_path_for(db, rid, parsed.get("date") or datetime.now().strftime("%Y-%m-%d %H:%M"), parsed.get("merchant"),
                       parsed.get("total"), ext)
    p = abs_path(db, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(blob)
    with db.tx() as con:
        con.execute("UPDATE receipts SET rel_path=? WHERE id=?", (rel, rid))
    return {**get(db, rid), "parsed": parsed}


def get(db: Database, rid: int) -> dict | None:
    r = db.one("SELECT * FROM receipts WHERE id=?", (rid,))
    if r:
        r = dict(r)
        r["parsed"] = json.loads(r["parsed"] or "{}")
        r["url"] = f"/api/receipts/{rid}/image"
        r["thumb_url"] = f"/api/receipts/{rid}/image?thumb=1"
    return r


def for_tx(db: Database, tid: int) -> list[dict]:
    return [get(db, r["id"]) for r in db.all("SELECT id FROM receipts WHERE transaction_id=? ORDER BY id", (tid,))]


def relocate(db: Database, rid: int) -> str | None:
    """Rename/move one receipt file to match its transaction (date, merchant, amount). Returns the new rel path."""
    r = db.one("""SELECT r.*, t.date tdate, t.amount tamount, t.orig_amount, m.name merchant FROM receipts r
        LEFT JOIN transactions t ON t.id=r.transaction_id LEFT JOIN merchants m ON m.id=t.merchant_id WHERE r.id=?""", (rid,))
    if not r or not r["rel_path"]:
        return None
    ext = r["rel_path"].rsplit(".", 1)[-1]
    if r["transaction_id"] and r["tdate"]:
        want = rel_path_for(db, rid, r["tdate"], r["merchant"] or json.loads(r["parsed"] or "{}").get("merchant"),
                            r["orig_amount"] if r["orig_amount"] is not None else r["tamount"], ext)
    else:
        return r["rel_path"]
    if want == r["rel_path"]:
        return want
    src, dst = abs_path(db, r["rel_path"]), abs_path(db, want)
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        _prune_empty(db, src.parent)
    with db.tx() as con:
        con.execute("UPDATE receipts SET rel_path=? WHERE id=?", (want, rid))
    return want


def relocate_for_tx(db: Database, tid: int) -> None:
    for r in db.all("SELECT id FROM receipts WHERE transaction_id=?", (tid,)):
        try:
            relocate(db, r["id"])
        except (OSError, ReceiptError):
            pass


def link(db: Database, rids: list[int], tid: int) -> list[dict]:
    if not db.one("SELECT 1 x FROM transactions WHERE id=?", (tid,)):
        raise ReceiptError("Transaction not found")
    with db.tx() as con:
        for rid in rids:
            con.execute("UPDATE receipts SET transaction_id=? WHERE id=?", (tid, int(rid)))
    relocate_for_tx(db, tid)
    return for_tx(db, tid)


def _prune_empty(db: Database, d: Path) -> None:
    base = base_dir(db).resolve()
    d = d.resolve()
    while d != base and base in d.parents:
        try:
            d.rmdir()
        except OSError:
            break
        d = d.parent


def delete(db: Database, rid: int) -> None:
    r = db.one("SELECT rel_path FROM receipts WHERE id=?", (rid,))
    if not r:
        return
    if r["rel_path"]:
        try:
            p = abs_path(db, r["rel_path"])
            p.unlink(missing_ok=True)
            _prune_empty(db, p.parent)
        except (OSError, ReceiptError):
            pass
    with db.tx() as con:
        con.execute("DELETE FROM receipts WHERE id=?", (rid,))


def on_tx_delete(db: Database, tids: list[int], keep: bool = False) -> int:
    """Called when transactions are deleted: delete their receipt images, or keep them (unlinked) if keep=True."""
    rows = db.all(f"SELECT id FROM receipts WHERE transaction_id IN ({','.join('?' * len(tids)) or 'NULL'})", tids)
    for r in rows:
        if keep:
            with db.tx() as con:
                con.execute("UPDATE receipts SET transaction_id=NULL WHERE id=?", (r["id"],))
        else:
            delete(db, r["id"])
    return len(rows)


def cleanup_drafts(db: Database, days: int = 14) -> int:
    """Unconfirmed uploads (never linked) older than `days` are removed."""
    rows = db.all("SELECT id FROM receipts WHERE transaction_id IS NULL AND kept=0 AND created_at < datetime('now','localtime',?)",
                  (f"-{int(days)} days",))
    for r in rows:
        delete(db, r["id"])
    return len(rows)


def thumbnail(path: Path, size: int = 360) -> bytes:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return path.read_bytes()
    img = ImageOps.exif_transpose(Image.open(path))
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=80)
    return buf.getvalue()


def backup_copy(db: Database, dest_root: Path, include_external: bool = False) -> int:
    """Mirror receipt files into the backups folder (new/changed files only). A receipts folder outside the data
    folder is skipped unless include_external (backups then hold every photo, wherever it's stored)."""
    base = base_dir(db)
    data = Path(db.path).parent.resolve()
    if not base.exists():
        return 0
    if not include_external and data not in base.resolve().parents and base.resolve() != data:
        return 0
    try:
        if dest_root.resolve() == base.resolve() or base.resolve() in dest_root.resolve().parents:
            return 0  # never copy a folder into itself
    except OSError:
        return 0
    n = 0
    for p in base.rglob("*"):
        if p.is_file():
            q = dest_root / p.relative_to(base)
            if not q.exists() or q.stat().st_size != p.stat().st_size or q.stat().st_mtime < p.stat().st_mtime - 1:
                q.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, q); n += 1
    return n


# ---------------------------------------------------------------- library (browse saved receipts by date)
def _lib_where(year: str | None, month: str | None, day: str | None, q: str | None, ledger_id: int | None):
    """Saved receipts only (linked to a transaction, or kept): filed under the transaction date, else the upload day."""
    w, a = ["(r.transaction_id IS NOT NULL OR r.kept=1)", "r.rel_path<>''"], []
    if ledger_id:
        w.append("t.ledger_id=?"); a.append(int(ledger_id))
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        cond = "(m.name LIKE ? OR t.note LIKE ? OR r.orig_name LIKE ? OR r.ocr_text LIKE ? OR a.name LIKE ? OR c.name LIKE ?"
        a += [like] * 6
        try:
            n = abs(float(q.replace(",", "").lstrip("$")))
            cond += " OR ABS(ABS(COALESCE(t.orig_amount, t.amount)) - ?) < 0.005 OR ABS(ABS(t.amount) - ?) < 0.005"
            a += [n, n]
        except ValueError:
            pass
        w.append(cond + ")")
    for part, val, n in (("y", year, 4), ("m", month, 2), ("d", day, 2)):
        if val:
            if not str(val).isdigit():
                raise ReceiptError("Year, month and day are numbers")
            w.append({"y": "substr(dt,1,4)=?", "m": "substr(dt,6,2)=?", "d": "substr(dt,9,2)=?"}[part])
            a.append(str(val).zfill(n))
    return " AND ".join(w), a


_LIB_FROM = """FROM (SELECT r.*, COALESCE(substr(t.date,1,10), substr(r.created_at,1,10)) dt FROM receipts r
    LEFT JOIN transactions t ON t.id=r.transaction_id) r LEFT JOIN transactions t ON t.id=r.transaction_id
    LEFT JOIN merchants m ON m.id=t.merchant_id LEFT JOIN accounts a ON a.id=t.account_id
    LEFT JOIN categories c ON c.id=t.category_id"""


def library(db: Database, year: str | None = None, month: str | None = None, day: str | None = None,
            q: str | None = None, ledger_id: int | None = None, limit: int = 300) -> dict:
    """{tree: [{y, n, months: [{m, n, days: [{d, n}]}]}] (the search applies, the date does not), items (newest first,
    the chosen year / month / day), total, truncated}."""
    limit = max(1, min(int(limit or 300), 1000))
    tw, ta = _lib_where(None, None, None, q, ledger_id)
    tree: list[dict] = []
    for r in db.all(f"SELECT r.dt, COUNT(*) n {_LIB_FROM} WHERE {tw} GROUP BY r.dt ORDER BY r.dt DESC", ta):
        y, m, d = r["dt"][:4], r["dt"][5:7], r["dt"][8:10]
        if not tree or tree[-1]["y"] != y:
            tree.append({"y": y, "n": 0, "months": []})
        ms = tree[-1]["months"]
        if not ms or ms[-1]["m"] != m:
            ms.append({"m": m, "n": 0, "days": []})
        ms[-1]["days"].append({"d": d, "n": r["n"]})
        ms[-1]["n"] += r["n"]
        tree[-1]["n"] += r["n"]
    w, a = _lib_where(year, month, day, q, ledger_id)
    total = db.one(f"SELECT COUNT(*) n {_LIB_FROM} WHERE {w}", a)["n"]
    rows = db.all(f"""SELECT r.id, r.transaction_id, r.orig_name, r.size, r.mime, r.created_at, r.rel_path, r.dt date,
        t.date tx_date, t.kind, t.status, t.amount, t.currency, t.orig_amount, t.orig_currency, t.note,
        m.name merchant, a.name account_name, c.name category_name {_LIB_FROM} WHERE {w}
        ORDER BY r.dt DESC, t.date DESC, r.id DESC LIMIT ?""", a + [limit])
    items = []
    for r in rows:
        r = dict(r)
        r["url"] = f"/api/receipts/{r['id']}/image"
        r["thumb_url"] = f"/api/receipts/{r['id']}/image?thumb=1"
        r["file_name"] = r.pop("rel_path").rsplit("/", 1)[-1]
        items.append(r)
    return {"tree": tree, "items": items, "total": total, "truncated": total > len(items)}
