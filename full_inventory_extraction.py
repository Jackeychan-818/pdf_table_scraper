#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Batch extract inventory tables from MANY PDFs (AFTER Feb 2022 ONLY).

Your new logic implemented:
A) Decide the full table block pages:
   - Anchor page must pass STRICT detector (months + row labels + decimals) to avoid TOC.
   - Continuation pages are detected by footer keyword "在庫" in bottom ~10% band
     + at least one decimal in body crop.

B) Infer how many valid numeric columns the table has (K):
   - Find the FIRST real data row (a row-label row that contains decimal numbers).
   - Count how many decimal tokens are on that row => K.
   - Infer K column centers (x positions) from that row (optional refine using later rows).

C) Extract per-row value using x-alignment, NOT by "2nd token":
   - For each detected table row, collect decimal tokens with x positions.
   - Assign tokens to closest of K column centers.
   - Choose a target column (default: last column) for output.
   - Keep empty rows => None for that row if target column missing.

D) NEW: NOTE-CUTOFF (資料/注) is applied ONLY on the first page of the chosen block
   (the anchor/first page), to avoid losing real data on continuation pages.

Output (same 9-rows-per-prefecture structure):
  prefecture | stage | rice_type | l2_stock

Debug:
  OUT_DIR/debug/...
  OUT_DIR/debug_images/<pdf_stem>/...
"""

from pathlib import Path
import re
import os
import sys
import logging
import warnings
from contextlib import contextmanager
from typing import List, Dict, Tuple, Optional

import pdfplumber
import pandas as pd
import unicodedata

# =========================
# NOISE SUPPRESSION
# =========================
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)
warnings.filterwarnings("ignore")


@contextmanager
def silence_stderr():
    old = sys.stderr
    f = None
    try:
        f = open(os.devnull, "w")
        sys.stderr = f
        yield
    finally:
        sys.stderr = old
        if f:
            try:
                f.close()
            except Exception:
                pass


# =========================
# PATHS
# =========================
RAW_ROOT = Path("raw_data")

OUT_DIR = Path("inventory_new1")
OUT_DIR.mkdir(parents=True, exist_ok=True)

DEBUG_DIR = OUT_DIR / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

DEBUG_IMG_ROOT = OUT_DIR / "debug_images"
DEBUG_IMG_ROOT.mkdir(exist_ok=True)

TABLE_TITLE_KEYWORD = "産地別民間在庫の推移"

# =========================
# PREFECTURES ORDER (fixed)
# =========================
PREF_ORDER_LIST = [
    "北海道",
    "青森", "岩手", "宮城", "秋田", "山形", "福島",
    "茨城", "栃木", "群馬", "埼玉", "千葉", "東京", "神奈川","山梨",  "長野","静岡",
    "新潟", "富山", "石川", "福井",
    "岐阜",  "愛知", "三重",
    "滋賀", "京都", "大阪", "兵庫", "奈良", "和歌山",
    "鳥取", "島根", "岡山", "広島", "山口",
    "徳島", "香川", "愛媛", "高知",
    "福岡", "佐賀", "長崎", "熊本", "大分", "宮崎", "鹿児島",
    "沖縄",
]

STAGES = ["出荷＋販売段階", "出荷段階", "販売段階"]
RICE_TYPES = ["general", "今年産米", "去年古米"]

# Row labels/hints (what makes a "table row")
ROW_HINTS = STAGES + ["年産米", "古米"]

FOOTNOTE_MARKERS = [
    "資料", "注", "（お知らせ）", "お知らせ",
    "http://", "https://", "www.", "maff.go.jp",
]

MONTH_HINTS = [
    "7月", "７月",
    "8月", "８月",
    "9月", "９月",
    "10月", "11月", "12月",
    "1月", "１月",
    "2月", "２月",
    "3月", "３月",
    "4月", "４月",
    "5月", "５月",
    "6月", "６月",
]

# =========================
# MAIN CROP SETTINGS
# =========================
CROP_LEFT_FRAC = 0.02
CROP_TOP_FRAC = 0.02
CROP_RIGHT_FRAC = 0.98
CROP_BOTTOM_FRAC = 0.97

# =========================
# FOOTER DETECTION SETTINGS
# =========================
FOOTER_KEYWORD = "在庫"
FOOTER_BAND_HEIGHT_FRAC = 0.10  # bottom 10%

# =========================
# COLUMN INFERENCE + EXTRACTION SETTINGS
# =========================
# Choose which column to output:
#   -1 = last column (your “2nd component” when K=2)
#    0 = first column, 1 = second, ...
TARGET_COL_INDEX = -1

# Row clustering tolerance
ROW_BASE_TOL = 3
ROW_MERGE_GAP = 6

# Numeric token patterns
DOT_TOKENS = {".", "．", "。"}
INT_TOKEN_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})*$|^-?\d+$")
DECIMAL_TOKEN_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d+)$|^-?\d+\.\d+$")

# “Any decimal exists”
DECIMAL_RE = re.compile(r"-?\d+\.\d+")

# Distance threshold for assigning a token to a column center
MAX_COL_DIST = 60

# =========================
# AFTER FEB 2022 FILTER
# =========================
MONTH_ABBR = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
STEM_MY_RE = re.compile(r"^([A-Za-z]{3})(\d{4})$")  # e.g. Mar2022


def parse_stem_month_year(stem: str) -> Optional[Tuple[int, int]]:
    m = STEM_MY_RE.match(stem)
    if not m:
        return None
    mon_abbr, year_s = m.group(1), m.group(2)
    mon_abbr = mon_abbr[:1].upper() + mon_abbr[1:3].lower()
    if mon_abbr not in MONTH_ABBR:
        return None
    return (int(year_s), MONTH_ABBR[mon_abbr])


def is_after_feb_2022(stem: str) -> bool:
    ym = parse_stem_month_year(stem)
    if not ym:
        return False
    y, mo = ym
    return (y > 2022) or (y == 2022 and mo >= 3)


# =========================
# NOTE CUTOFF (anchor page only)
# =========================
NOTE_MARKERS = ["資料", "注"]


def find_note_cutoff_y(rows: dict) -> Optional[float]:
    """
    rows: {ykey: [word dicts]} in crop coordinates.
    Return a cutoff y (top) so that anything below is ignored.
    Only intended to be used on the FIRST page of the chosen block.
    """
    for y in sorted(rows.keys()):
        wds = sorted(rows[y], key=lambda z: z["x0"])
        txt = join_row_text(wds).replace(" ", "")
        if any(m in txt for m in NOTE_MARKERS) or txt.startswith("注"):
            top = min(w["top"] for w in wds)
            return top - 2.0
    return None


# =========================
# UTILS
# =========================
def norm(s) -> str:
    if s is None:
        return ""
    return str(s).replace("\u3000", " ").strip()


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def crop_body_bbox(page) -> Tuple[float, float, float, float]:
    w, h = page.width, page.height
    return (
        CROP_LEFT_FRAC * w,
        CROP_TOP_FRAC * h,
        CROP_RIGHT_FRAC * w,
        CROP_BOTTOM_FRAC * h,
    )


def crop_footer_bbox(page) -> Tuple[float, float, float, float]:
    w, h = page.width, page.height
    y1 = h
    y0 = h * (1.0 - FOOTER_BAND_HEIGHT_FRAC)
    return (0.0, y0, w, y1)


def extract_text_in_bbox(page, bbox) -> str:
    try:
        return norm(page.crop(bbox).extract_text() or "")
    except Exception:
        return ""


def count_row_hits(txt: str) -> int:
    return sum(1 for h in ROW_HINTS if h in (txt or ""))


def count_month_hits(txt: str) -> int:
    return sum(1 for m in MONTH_HINTS if m in (txt or ""))


def has_decimals(txt: str) -> bool:
    return bool(DECIMAL_RE.search((txt or "").replace(",", "")))


def is_table_row_label_text(line: str) -> bool:
    """
    Keep it simple (your current rule): row label must contain hints,
    and must not contain footnote markers / URLs.
    """
    if not line:
        return False
    if any(m in line for m in FOOTNOTE_MARKERS):
        return False
    return any(h in line for h in ROW_HINTS)


# =========================
# Page detectors
# =========================
def looks_like_inventory_table_strict(page) -> bool:
    """
    Strict detector: used for anchor pages only (avoid TOC false positives).
    Requires row labels + months + decimals within the body crop.
    """
    txt = extract_text_in_bbox(page, crop_body_bbox(page))
    if not txt:
        return False
    if count_row_hits(txt) < 2:
        return False
    if count_month_hits(txt) < 4:
        return False
    if not has_decimals(txt):
        return False
    return True


def looks_like_inventory_table_by_footer(page) -> bool:
    """
    Continuation detector:
    bottom band has '在庫' AND body has at least one decimal.
    """
    footer_txt = extract_text_in_bbox(page, crop_footer_bbox(page))
    if FOOTER_KEYWORD not in footer_txt:
        return False
    body_txt = extract_text_in_bbox(page, crop_body_bbox(page))
    if not has_decimals(body_txt):
        return False
    return True


# =========================
# Anchor + forward expansion
# =========================
def find_anchor_pages(pdf, keyword: str) -> List[int]:
    matched = []
    for i, page in enumerate(pdf.pages):
        txt = page.extract_text() or ""
        if keyword in txt:
            matched.append(i)
    return matched


def expand_forward_by_footer(pdf, start_idx: int, pdf_stem: str, max_gap_pages: int = 1) -> List[int]:
    """
    Expand forward:
    - Anchor must pass STRICT detector.
    - Continuation pages: footer detector.
    - Allow up to `max_gap_pages` pages that fail footer detector before stopping.
    """
    n = len(pdf.pages)
    if start_idx < 0 or start_idx >= n:
        return []

    if not looks_like_inventory_table_strict(pdf.pages[start_idx]):
        rej = DEBUG_DIR / f"{pdf_stem}_page{start_idx}_REJECTED_ANCHOR.txt"
        try:
            with open(rej, "w", encoding="utf-8") as f:
                f.write(norm(pdf.pages[start_idx].extract_text() or ""))
        except Exception:
            pass
        return []

    block = [start_idx]
    gap = 0
    j = start_idx + 1

    while j < n:
        if looks_like_inventory_table_by_footer(pdf.pages[j]):
            block.append(j)
            gap = 0
        else:
            gap += 1
            if gap > max_gap_pages:
                break
        j += 1

    return block


def choose_best_block_forward(pdf, anchor_pages: List[int], pdf_stem: str) -> List[int]:
    """
    Expand forward from each anchor and choose the longest block.
    Fallback: longest consecutive run of footer-detected pages.
    """
    if anchor_pages:
        blocks = [expand_forward_by_footer(pdf, a, pdf_stem, max_gap_pages=1) for a in anchor_pages]
        blocks = [b for b in blocks if b]
        if blocks:
            blocks.sort(key=lambda b: (len(b), -b[0]), reverse=True)
            return blocks[0]

    table_pages = [i for i, p in enumerate(pdf.pages) if looks_like_inventory_table_by_footer(p)]
    if not table_pages:
        return []

    best = []
    cur = [table_pages[0]]
    for p in table_pages[1:]:
        if p == cur[-1] + 1:
            cur.append(p)
        else:
            if len(cur) > len(best):
                best = cur
            cur = [p]
    if len(cur) > len(best):
        best = cur
    return best


# =========================
# Word-row building (y clustering)
# =========================
def y_center(wd) -> float:
    return (wd["top"] + wd["bottom"]) / 2.0


def ykey(word, tol=3):
    yc = y_center(word)
    return int(round(yc / tol) * tol)


def build_rows_with_merge(words, base_tol=3, merge_gap=6):
    rows = {}
    for wd in words:
        y = ykey(wd, tol=base_tol)
        rows.setdefault(y, []).append(wd)

    ys = sorted(rows.keys())
    if not ys:
        return {}

    merged = {}
    cur_y = ys[0]
    cur_words = list(rows[cur_y])

    for y in ys[1:]:
        if abs(y - cur_y) <= merge_gap:
            cur_words.extend(rows[y])
        else:
            merged[cur_y] = cur_words
            cur_y = y
            cur_words = list(rows[y])

    merged[cur_y] = cur_words
    return merged


def join_row_text(words) -> str:
    if not words:
        return ""
    wds = sorted(words, key=lambda z: z["x0"])
    return norm("".join(_nfkc(w.get("text", "")) for w in wds))


def row_bbox(words) -> Optional[Tuple[float, float, float, float]]:
    if not words:
        return None
    x0 = min(w["x0"] for w in words)
    x1 = max(w["x1"] for w in words)
    top = min(w["top"] for w in words)
    bottom = max(w["bottom"] for w in words)
    return (x0, top, x1, bottom)


# =========================
# Numeric token extraction (x-aware)
# =========================
def _normalize_token_text(s: str) -> str:
    s = _nfkc(s).strip()
    s = s.replace("．", ".").replace("。", ".")
    return s


def _coalesce_split_decimals(tokens):
    """
    tokens: list of dict {x0,x1,xc,text}
    Merge patterns: INT + '.' + INT => INT.INT
    """
    out = []
    i = 0
    while i < len(tokens):
        t0 = tokens[i]
        txt0 = _normalize_token_text(t0["text"])

        if i + 2 < len(tokens):
            t1 = tokens[i + 1]
            t2 = tokens[i + 2]
            txt1 = _normalize_token_text(t1["text"])
            txt2 = _normalize_token_text(t2["text"])

            if INT_TOKEN_RE.fullmatch(txt0) and (txt1 in DOT_TOKENS or txt1 == ".") and INT_TOKEN_RE.fullmatch(txt2):
                merged_txt = f"{txt0}.{txt2}"
                x0 = t0["x0"]
                x1 = t2["x1"]
                xc = (x0 + x1) / 2.0
                out.append({"x0": x0, "x1": x1, "xc": xc, "text": merged_txt})
                i += 3
                continue

        out.append({"x0": t0["x0"], "x1": t0["x1"], "xc": t0["xc"], "text": txt0})
        i += 1

    return out


def collect_decimal_tokens(row_words: List[Dict]) -> List[Dict]:
    """
    Return decimal tokens with x centers from this row.
    """
    raw = []
    for w in row_words:
        txt = _normalize_token_text(w.get("text", ""))
        if not txt:
            continue
        if any(ch.isdigit() for ch in txt) or txt in DOT_TOKENS:
            raw.append({"x0": w["x0"], "x1": w["x1"], "xc": (w["x0"] + w["x1"]) / 2.0, "text": txt})

    raw.sort(key=lambda d: d["x0"])
    merged = _coalesce_split_decimals(raw)

    out = []
    for t in merged:
        txt = _normalize_token_text(t["text"]).replace(",", "")
        if DECIMAL_TOKEN_RE.fullmatch(txt) is None:
            continue
        try:
            val = float(txt)
        except Exception:
            continue
        out.append({"x0": t["x0"], "x1": t["x1"], "xc": t["xc"], "text": txt, "val": val})
    return out


def assign_tokens_to_centers(tokens: List[Dict], centers: List[float], max_dist: float) -> List[Optional[Dict]]:
    """
    For each center, keep the closest token within max_dist.
    Returns list length K of token dicts (or None).
    """
    K = len(centers)
    best: List[Optional[Tuple[float, Dict]]] = [None] * K

    for t in tokens:
        dists = [abs(t["xc"] - c) for c in centers]
        j = min(range(K), key=lambda k: dists[k])
        if dists[j] > max_dist:
            continue
        if best[j] is None or dists[j] < best[j][0]:
            best[j] = (dists[j], t)

    out: List[Optional[Dict]] = []
    for j in range(K):
        out.append(best[j][1] if best[j] is not None else None)
    return out


# =========================
# Column inference (K + centers) from first real data row
# =========================
def infer_k_and_centers_from_block(pdf, page_indices: List[int], pdf_stem: str) -> Tuple[int, List[float], Optional[Tuple[int, str]]]:
    """
    Find FIRST row that:
      - is a table row label row (contains ROW_HINTS)
      - has decimal tokens
    Let K = number of decimal tokens on that row.
    centers = sorted x-centers of those tokens (length K).
    Then refine centers by medians from later rows that have >=K tokens.
    """
    debug_path = DEBUG_DIR / f"{pdf_stem}_column_inference.txt"

    first_hit = None  # (page_idx, label_text)
    K = 0
    centers: List[float] = []
    first_row_tokens: List[Dict] = []

    # pass 1: find first row with decimals
    for pi in page_indices:
        page = pdf.pages[pi]
        crop = page.crop(crop_body_bbox(page))
        words = crop.extract_words(keep_blank_chars=False, use_text_flow=False)
        rows = build_rows_with_merge(words, base_tol=ROW_BASE_TOL, merge_gap=ROW_MERGE_GAP)

        for y in sorted(rows.keys()):
            row_wds = sorted(rows[y], key=lambda z: z["x0"])
            row_text = join_row_text(row_wds).replace(" ", "")
            if not is_table_row_label_text(row_text):
                continue

            toks = collect_decimal_tokens(row_wds)
            if toks:
                toks_sorted = sorted(toks, key=lambda t: t["xc"])
                K = len(toks_sorted)
                centers = [t["xc"] for t in toks_sorted]
                first_row_tokens = toks_sorted
                first_hit = (pi, row_text)
                break

        if K > 0:
            break

    if K == 0:
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write("FAILED to infer K/centers: no row-label row with decimals found.\n")
        return 1, [0.0], None

    # pass 2: refine by medians (stable)
    per_col_samples: List[List[float]] = [[] for _ in range(K)]
    for j in range(K):
        per_col_samples[j].append(centers[j])

    for pi in page_indices:
        page = pdf.pages[pi]
        crop = page.crop(crop_body_bbox(page))
        words = crop.extract_words(keep_blank_chars=False, use_text_flow=False)
        rows = build_rows_with_merge(words, base_tol=ROW_BASE_TOL, merge_gap=ROW_MERGE_GAP)

        for y in sorted(rows.keys()):
            row_wds = sorted(rows[y], key=lambda z: z["x0"])
            row_text = join_row_text(row_wds).replace(" ", "")
            if not is_table_row_label_text(row_text):
                continue

            toks = collect_decimal_tokens(row_wds)
            if len(toks) >= K:
                xs = sorted([t["xc"] for t in toks])[:K]
                for j in range(K):
                    per_col_samples[j].append(xs[j])

        if sum(len(s) for s in per_col_samples) >= K * 8:
            break

    refined = []
    for j in range(K):
        s = sorted(per_col_samples[j])
        refined.append(s[len(s) // 2])

    with open(debug_path, "w", encoding="utf-8") as f:
        f.write(f"Inferred K (num columns) = {K}\n")
        f.write(f"First-hit page idx (0-based) = {first_hit[0] if first_hit else None}\n")
        f.write(f"First-hit label text = {first_hit[1] if first_hit else None}\n\n")
        f.write("Initial centers (xc): " + ", ".join(f"{c:.1f}" for c in centers) + "\n")
        f.write("Refined centers (xc): " + ", ".join(f"{c:.1f}" for c in refined) + "\n")
        f.write(f"TARGET_COL_INDEX = {TARGET_COL_INDEX}\n")

    return K, refined, first_hit


# =========================
# Annotated screenshots (rows + tokens + chosen token)
# =========================
def save_annotated_screenshots(page, pdf_stem: str, page_idx: int, centers: Optional[List[float]], K: int):
    out_dir = DEBUG_IMG_ROOT / pdf_stem
    out_dir.mkdir(parents=True, exist_ok=True)

    page.to_image(resolution=180).save(out_dir / f"page{page_idx}_FULL.png")

    body_bbox = crop_body_bbox(page)
    crop = page.crop(body_bbox)

    words = crop.extract_words(keep_blank_chars=False, use_text_flow=False)
    rows = build_rows_with_merge(words, base_tol=ROW_BASE_TOL, merge_gap=ROW_MERGE_GAP)

    im = page.to_image(resolution=180)

    for y in sorted(rows.keys()):
        row_wds = sorted(rows[y], key=lambda z: z["x0"])
        row_text = join_row_text(row_wds).replace(" ", "")
        if not is_table_row_label_text(row_text):
            continue

        rb = row_bbox(row_wds)
        if rb:
            x0, top, x1, bottom = rb
            x0p = body_bbox[0] + x0
            x1p = body_bbox[0] + x1
            top_p = body_bbox[1] + top
            bot_p = body_bbox[1] + bottom
            im.draw_rect((x0p, top_p, x1p, bot_p), stroke="red", fill=None, stroke_width=2)

        toks = collect_decimal_tokens(row_wds)
        if not toks or not centers:
            continue

        assigned = assign_tokens_to_centers(toks, centers, MAX_COL_DIST)
        tcol = TARGET_COL_INDEX if TARGET_COL_INDEX >= 0 else (K + TARGET_COL_INDEX)
        chosen = assigned[tcol] if 0 <= tcol < K else None

        for t in toks:
            x0p = body_bbox[0] + t["x0"]
            x1p = body_bbox[0] + t["x1"]
            if rb:
                top_p = body_bbox[1] + rb[1]
                bot_p = body_bbox[1] + rb[3]
            else:
                top_p = body_bbox[1]
                bot_p = body_bbox[1] + 5
            im.draw_rect((x0p, top_p, x1p, bot_p), stroke="orange", fill=None, stroke_width=2)

        if chosen:
            x0p = body_bbox[0] + chosen["x0"]
            x1p = body_bbox[0] + chosen["x1"]
            if rb:
                top_p = body_bbox[1] + rb[1]
                bot_p = body_bbox[1] + rb[3]
            else:
                top_p = body_bbox[1]
                bot_p = body_bbox[1] + 5
            im.draw_rect((x0p, top_p, x1p, bot_p), stroke="green", fill=None, stroke_width=3)

    im.save(out_dir / f"page{page_idx}_ANNOT.png")


# =========================
# Extraction using inferred centers (keeps empty rows)
# =========================
def extract_values_with_debug(pdf, page_indices: List[int], pdf_stem: str) -> List[Optional[float]]:
    """
    Extract ONE value per detected table row (row-label row):
      value = token aligned to TARGET_COL_INDEX among K inferred columns
      None if that cell empty

    Debug per page:
      - words_page_{pi}.csv
      - reconstructed_page_{pi}.txt
      - screenshots FULL + ANNOT

    NOTE-CUTOFF: applied ONLY on the first page in page_indices.
    """
    values: List[Optional[float]] = []

    K, centers, first_hit = infer_k_and_centers_from_block(pdf, page_indices, pdf_stem)

    tcol = TARGET_COL_INDEX if TARGET_COL_INDEX >= 0 else (K + TARGET_COL_INDEX)
    if tcol < 0 or tcol >= K:
        tcol = K - 1  # fallback to last

    first_page_idx = page_indices[0] if page_indices else None

    for pi in page_indices:
        page = pdf.pages[pi]
        body_bbox = crop_body_bbox(page)
        crop = page.crop(body_bbox)

        words = crop.extract_words(
            keep_blank_chars=False,
            use_text_flow=False,
            extra_attrs=["fontname", "size"],
        )

        try:
            pd.DataFrame(words).to_csv(
                DEBUG_DIR / f"{pdf_stem}_words_page_{pi}.csv",
                index=False,
                encoding="utf-8-sig",
            )
        except Exception:
            pass

        rows = build_rows_with_merge(words, base_tol=ROW_BASE_TOL, merge_gap=ROW_MERGE_GAP)

        # ✅ Apply note cutoff ONLY on the first page (anchor/first page of block)
        cutoff_y = find_note_cutoff_y(rows) if (first_page_idx is not None and pi == first_page_idx) else None

        # save annotated images
        try:
            save_annotated_screenshots(page, pdf_stem, pi, centers, K)
        except Exception as e:
            with open(DEBUG_DIR / f"{pdf_stem}_page{pi}_IMG_DEBUG_ERROR.txt", "w", encoding="utf-8") as f:
                f.write(str(e))

        recon_path = DEBUG_DIR / f"{pdf_stem}_reconstructed_page_{pi}.txt"
        with open(recon_path, "w", encoding="utf-8") as f:
            f.write(f"PDF stem: {pdf_stem}\n")
            f.write(f"Page idx (0-based): {pi}\n")
            f.write(f"Inferred K={K}, centers={', '.join(f'{c:.1f}' for c in centers)}\n")
            f.write(f"TARGET_COL_INDEX={TARGET_COL_INDEX} => using col={tcol} (0-based)\n")
            if first_hit:
                f.write(f"First-hit was on page idx={first_hit[0]} label={first_hit[1]}\n")
            if cutoff_y is not None:
                f.write(f"NOTE CUTOFF enabled (FIRST PAGE ONLY): ignore rows with top >= {cutoff_y:.1f}\n")
            f.write("-" * 140 + "\n")
            f.write("row_label | tokens(xc=val) | assigned_cols | chosen\n")
            f.write("-" * 140 + "\n")

            for y in sorted(rows.keys()):
                row_wds = sorted(rows[y], key=lambda z: z["x0"])

                # ✅ skip note block only on first page
                if cutoff_y is not None:
                    row_top = min(w["top"] for w in row_wds)
                    if row_top >= cutoff_y:
                        continue

                row_text = join_row_text(row_wds).replace(" ", "")
                if not is_table_row_label_text(row_text):
                    continue

                toks = collect_decimal_tokens(row_wds)
                tok_dbg = ", ".join([f"{t['xc']:.1f}={t['val']}" for t in sorted(toks, key=lambda t: t["xc"])]) if toks else ""

                chosen_val: Optional[float] = None
                assigned_dbg = ""

                if toks:
                    assigned = assign_tokens_to_centers(toks, centers, MAX_COL_DIST)
                    assigned_vals = []
                    for j in range(K):
                        assigned_vals.append("" if assigned[j] is None else str(assigned[j]["val"]))
                    assigned_dbg = "[" + ", ".join(assigned_vals) + "]"
                    if assigned[tcol] is not None:
                        chosen_val = float(assigned[tcol]["val"])

                values.append(chosen_val if chosen_val is not None else None)

                f.write(f"{row_text} | {tok_dbg} | {assigned_dbg} | {chosen_val}\n")

    return values


# =========================
# Build dataframe (same as your original output)
# =========================
def build_df(values: List[Optional[float]]):
    nblocks = len(values) // 9
    values = values[: nblocks * 9]

    records = []
    for b in range(nblocks):
        if b >= len(PREF_ORDER_LIST):
            break
        pref = PREF_ORDER_LIST[b]
        block = values[b * 9:(b + 1) * 9]

        for i, v in enumerate(block):
            records.append(
                {
                    "prefecture": pref,
                    "stage": STAGES[i // 3],
                    "rice_type": RICE_TYPES[i % 3],
                    "l2_stock": v,
                }
            )

        if pref == "沖縄":
            break

    return pd.DataFrame(records)


# =========================
# Process one PDF
# =========================
def process_one_pdf(pdf_path: Path) -> bool:
    pdf_stem = pdf_path.stem
    try:
        with silence_stderr():
            with pdfplumber.open(str(pdf_path)) as pdf:
                anchors = find_anchor_pages(pdf, TABLE_TITLE_KEYWORD)
                block_pages = choose_best_block_forward(pdf, anchors, pdf_stem)

                if not block_pages:
                    print(f"[SKIP] {pdf_path.name}: no table-like page block found.")
                    return False

                print(f"  Anchor pages: {anchors}")
                print(f"  Using block:  {block_pages[0]}..{block_pages[-1]} (len={len(block_pages)})")

                values = extract_values_with_debug(pdf, block_pages, pdf_stem)

                if not values:
                    print(f"[FAIL] {pdf_path.name}: extracted 0 row-values.")
                    return False

                if len(values) % 9 != 0:
                    print(f"[WARN] {pdf_path.name}: extracted rows={len(values)} (not multiple of 9)")

                df = build_df(values)
                if df.empty:
                    print(f"[FAIL] {pdf_path.name}: df empty (values={len(values)}).")
                    return False

                out_xlsx = OUT_DIR / f"inventory_{pdf_stem}.xlsx"
                df.to_excel(out_xlsx, index=False)

                print(f"[OK] {pdf_path.name} -> {out_xlsx.name} (rows={len(df)})")
                return True

    except Exception as e:
        print(f"[ERROR] {pdf_path.name}: {e}")
        return False


# =========================
# Iterate PDFs after Feb 2022
# =========================
def iter_pdfs_after_feb2022(root: Path):
    for pdf_path in sorted(root.glob("*/*.pdf")):
        if is_after_feb_2022(pdf_path.stem):
            yield pdf_path


def main():
    pdfs_all = list(sorted(RAW_ROOT.glob("*/*.pdf")))
    pdfs = list(iter_pdfs_after_feb2022(RAW_ROOT))

    print(f"Found {len(pdfs_all)} PDFs under {RAW_ROOT}")
    print(f"Target PDFs AFTER Feb2022: {len(pdfs)}")
    if pdfs:
        print("First few target PDFs:", [p.name for p in pdfs[:10]])

    if not pdfs:
        raise RuntimeError("No PDFs matched AFTER Feb2022. Expected stems like Mar2022.pdf, Apr2022.pdf, ...")

    ok = fail = 0
    for i, pdf_path in enumerate(pdfs, start=1):
        print(f"\n===== ({i}/{len(pdfs)}) Processing: {pdf_path} =====")
        if process_one_pdf(pdf_path):
            ok += 1
        else:
            fail += 1

    print("\n========== SUMMARY ==========")
    print(f"OK:   {ok}")
    print(f"FAIL: {fail}")
    print(f"Output dir: {OUT_DIR}")
    print(f"Debug text dir: {DEBUG_DIR}")
    print(f"Debug image dir: {DEBUG_IMG_ROOT}")


if __name__ == "__main__":
    main()