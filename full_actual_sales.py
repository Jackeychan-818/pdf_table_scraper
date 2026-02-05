#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
FIXED VERSION: Better prefecture detection for partial/split text
"""

from pathlib import Path
import re
import traceback
import pandas as pd
import pdfplumber
import logging

# Silence noisy pdfminer/pdfplumber warnings
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

# =========================
# SETTINGS (EDIT THESE)
# =========================
RAW_ROOT = Path("raw_data")
OUT_DIR  = Path("actual_sales1")

TABLE_TITLE_KEYWORD = "産地別契約・販売状況"

PAGES_TO_CAPTURE_AFTER_TABLE_START = 2
MAX_FORWARD_SCAN_FROM_TITLE = 2

# CRITICAL FIX: Increase crop ratio to capture full prefecture names
CROP_RIGHT_RATIO = 0.75  # Increased from 0.65

ROW_BASE_TOL = 3
ROW_MERGE_GAP = 6

YEAR_FOLDERS = [str(y) for y in range(2017, 2026)]

PREF_ORDER_LIST = [
    "北海道",
    "青森", "岩手", "宮城", "秋田", "山形", "福島",
    "茨城", "栃木", "群馬", "埼玉", "千葉", "東京", "神奈川",
    "新潟", "富山", "石川", "福井", "山梨", "長野",
    "岐阜", "静岡", "愛知", "三重",
    "滋賀", "京都", "大阪", "兵庫", "奈良", "和歌山",
    "鳥取", "島根", "岡山", "広島", "山口",
    "徳島", "香川", "愛媛", "高知",
    "福岡", "佐賀", "長崎", "熊本", "大分", "宮崎", "鹿児島",
    "沖縄",
]

ONLY_PROCESS = {
    "2019": {"Dec"},
    "2020": {"Jan", "Feb", "Apr", "May", "Jun", "Sep", "Oct", "Nov"},
    "2021": {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug"},
    "2025": {"Nov"},
}

# =========================
# INTERNALS
# =========================
NUM_RE = re.compile(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?")
HDR_WORDS = ["集荷数量", "契約数量", "販売数量", "集荷・契約・販売数量"]

DECIMAL_TOKEN_RE = re.compile(r"^\d{1,3}(?:,\d{3})*(?:\.\d+)$|^\d+\.\d+$")

# CRITICAL FIX: Adjust boundaries to capture full prefecture names
PREF_X_MAX = 140  # Increased from 120
BRAND_X_MAX = 320  # Increased from 300
NUM_TOKEN_MIN_X = 160


def _norm_no_space(s: str) -> str:
    s = (s or "").replace("\u3000", " ")
    return re.sub(r"\s+", "", s)


def normalize_pref_label(s: str) -> str:
    return _norm_no_space(s)


PREF_MAP = {normalize_pref_label(p): p for p in PREF_ORDER_LIST}


# CRITICAL FIX: Add fuzzy matching for partial prefecture names
def is_prefecture_label_fuzzy(label: str):
    """
    Match prefecture names even if partially extracted.
    Returns the full prefecture name if matched, None otherwise.
    """
    if not label:
        return None
    
    key = normalize_pref_label(label)
    
    # Direct match
    if key in PREF_MAP:
        return PREF_MAP[key]
    
    # Partial match: check if label is a prefix of any prefecture
    # e.g., "鳥" matches "鳥取", "島" matches "島根"
    for pref_key, pref_full in PREF_MAP.items():
        if pref_key.startswith(key) and len(key) >= 1:
            # Only match if it's a reasonable prefix (at least 1 char)
            return pref_full
    
    return None


def is_prefecture_label(label: str):
    """Keep original function for backward compatibility"""
    return is_prefecture_label_fuzzy(label)


def clean_brand_label(s: str) -> str:
    s = (s or "").strip()
    if (s.startswith("(") and s.endswith(")")) or (s.startswith("（") and s.endswith("）")):
        s = s[1:-1].strip()
    return s


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


def join_text(words):
    if not words:
        return ""
    words = sorted(words, key=lambda z: z["x0"])
    return "".join(w["text"] for w in words).strip()


def detect_row_label(pref_col_text: str, brand_zone_text: str) -> str:
    """
    CRITICAL FIX: Better label detection
    """
    pref_text = (pref_col_text or "").strip()
    brand_text = (brand_zone_text or "").strip()
    
    # If we have text in prefecture column, prioritize it
    if pref_text:
        return pref_text
    
    # Otherwise use brand zone
    return brand_text


def is_zenkoku_label(label: str) -> bool:
    key = normalize_pref_label(label)
    return bool(key) and key.startswith("全国")


def page_has_table_signals(page: pdfplumber.page.Page) -> bool:
    w, h = page.width, page.height
    top_text = (page.crop((0, 0, w, min(h, 600))).extract_text() or "")
    top_text_norm = _norm_no_space(top_text)

    if "目次" in top_text_norm or "もくじ" in top_text_norm:
        return False

    if any(k in top_text for k in ["集荷数量", "契約数量", "販売数量"]):
        return True

    crop_right = w * CROP_RIGHT_RATIO
    crop = page.crop((0, 0, crop_right, h))
    words = crop.extract_words(keep_blank_chars=False, use_text_flow=False)
    if not words:
        return False

    rows = build_rows_with_merge(words, base_tol=ROW_BASE_TOL, merge_gap=ROW_MERGE_GAP)
    strong = 0
    for y in sorted(rows.keys()):
        wds = sorted(rows[y], key=lambda z: z["x0"])
        row_text = " ".join(wd["text"] for wd in wds).replace("．", ".").replace("。", ".")
        if len(NUM_RE.findall(row_text)) >= 3:
            strong += 1
            if strong >= 3:
                return True
    return False


def find_title_page(pdf: pdfplumber.PDF, keyword: str, debug_dir: Path, max_scan_pages=None) -> int:
    key_norm = _norm_no_space(keyword)
    scan_n = len(pdf.pages) if max_scan_pages is None else min(len(pdf.pages), max_scan_pages)

    candidates = []
    for i in range(scan_n):
        page = pdf.pages[i]
        w, h = page.width, page.height

        top_crop = page.crop((0, 0, w, min(h, 420)))
        if key_norm in _norm_no_space(top_crop.extract_text() or ""):
            candidates.append(i)
            continue

        if key_norm in _norm_no_space(page.extract_text() or ""):
            candidates.append(i)

    hits_1based = [i + 1 for i in candidates]
    out = debug_dir / "title_match_debug.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"Keyword: {keyword}\n")
        f.write(f"Normalized keyword: {key_norm}\n\n")
        f.write("Candidate pages (1-based) containing keyword:\n")
        f.write(", ".join(map(str, hits_1based)) if hits_1based else "(none)\n")

    for i in candidates:
        if page_has_table_signals(pdf.pages[i]):
            return i

    return candidates[0] if candidates else -1


def count_prefecture_rows_on_page(page: pdfplumber.page.Page) -> int:
    w, h = page.width, page.height
    crop_right = w * CROP_RIGHT_RATIO
    crop = page.crop((0, 0, crop_right, h))
    words = crop.extract_words(keep_blank_chars=False, use_text_flow=False)
    if not words:
        return 0

    rows = build_rows_with_merge(words, base_tol=ROW_BASE_TOL, merge_gap=ROW_MERGE_GAP)
    hits = 0
    for y in sorted(rows.keys()):
        wds = sorted(rows[y], key=lambda z: z["x0"])
        pref_w = [wd for wd in wds if wd["x0"] < PREF_X_MAX]
        brand_w = [wd for wd in wds if PREF_X_MAX <= wd["x0"] < BRAND_X_MAX]
        label = detect_row_label(join_text(pref_w), join_text(brand_w))
        if not label:
            continue
        if any(hw in label for hw in HDR_WORDS):
            continue
        if "資料" in label or "注" in label:
            continue
        if is_prefecture_label(label):
            hits += 1
    return hits


def find_table_start_page(pdf: pdfplumber.PDF, title_i: int, debug_dir: Path) -> int:
    n_pages = len(pdf.pages)
    start = title_i
    end = min(n_pages - 1, title_i + MAX_FORWARD_SCAN_FROM_TITLE)

    scan_log = debug_dir / "table_start_scan.txt"
    with open(scan_log, "w", encoding="utf-8") as f:
        f.write(f"Title page (1-based): {title_i+1}\n")
        f.write(f"Scan forward pages (1-based): {start+1}..{end+1}\n\n")

        for i in range(start, end + 1):
            page = pdf.pages[i]
            has_sig = page_has_table_signals(page)
            pref_hits = count_prefecture_rows_on_page(page) if has_sig else 0
            f.write(f"Page {i+1}: has_signals={has_sig}, pref_hits={pref_hits}\n")

            if has_sig and pref_hits >= 2:
                f.write(f"\n==> Chosen table start page: {i+1}\n")
                return i

        f.write(f"\n==> Fallback table start page: {title_i+1}\n")

    return title_i


def _collect_decimal_numeric_tokens(wds):
    toks = []
    for w in wds:
        if w["x0"] < NUM_TOKEN_MIN_X:
            continue
        txt = (w.get("text") or "").replace("．", ".").replace("。", ".")
        if not DECIMAL_TOKEN_RE.fullmatch(txt):
            continue
        try:
            val = float(txt.replace(",", ""))
        except Exception:
            continue
        xc = (w["x0"] + w["x1"]) / 2.0
        toks.append({"xc": xc, "val": val, "raw": txt})
    return toks


def infer_col_centers_from_page(rows_dict):
    triples = []
    for y in sorted(rows_dict.keys()):
        wds = sorted(rows_dict[y], key=lambda z: z["x0"])
        toks = _collect_decimal_numeric_tokens(wds)
        if len(toks) >= 3:
            xs = sorted([t["xc"] for t in toks])[:3]
            triples.append(xs)
            if len(triples) >= 8:
                break
    if len(triples) < 2:
        return None

    s1 = sorted([t[0] for t in triples])
    s2 = sorted([t[1] for t in triples])
    s3 = sorted([t[2] for t in triples])
    mid = len(triples) // 2
    return (s1[mid], s2[mid], s3[mid])


def extract_numbers_by_columns_decimal_only(wds, col_centers, max_dist=45):
    toks = _collect_decimal_numeric_tokens(wds)
    if not toks:
        return None

    vals = [float("nan"), float("nan"), float("nan")]
    best = [None, None, None]

    for t in toks:
        xc, val = t["xc"], t["val"]
        dists = [abs(xc - c) for c in col_centers]
        j = min(range(3), key=lambda k: dists[k])
        if dists[j] > max_dist:
            continue
        if best[j] is None or dists[j] < best[j][0]:
            best[j] = (dists[j], val)

    for j in range(3):
        if best[j] is not None:
            vals[j] = best[j][1]

    if all(pd.isna(x) for x in vals):
        return None
    return tuple(vals)


def parse_one_pdf(pdf_path: Path, year_out_dir: Path):
    stem = pdf_path.stem

    debug_dir = year_out_dir / "debug_actual_sales" / stem
    debug_dir.mkdir(parents=True, exist_ok=True)

    records = []
    notes = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        title_i = find_title_page(pdf, TABLE_TITLE_KEYWORD, debug_dir, max_scan_pages=None)
        if title_i < 0:
            raise RuntimeError(f"Title keyword not found in extractable text for {pdf_path}")

        table_start_i = find_table_start_page(pdf, title_i, debug_dir)

        page_idxs = list(
            range(
                table_start_i,
                min(len(pdf.pages), table_start_i + 1 + PAGES_TO_CAPTURE_AFTER_TABLE_START),
            )
        )

        current_pref = None
        zenkoku_seen = False

        for pi in page_idxs:
            page = pdf.pages[pi]
            w, h = page.width, page.height
            crop_right = w * CROP_RIGHT_RATIO
            crop = page.crop((0, 0, crop_right, h))

            words = crop.extract_words(
                keep_blank_chars=False,
                use_text_flow=False,
                extra_attrs=["fontname", "size"],
            )

            pd.DataFrame(words).to_csv(
                debug_dir / f"words_page_{pi+1}.csv",
                index=False,
                encoding="utf-8-sig",
            )

            rows = build_rows_with_merge(words, base_tol=ROW_BASE_TOL, merge_gap=ROW_MERGE_GAP)

            col_centers = infer_col_centers_from_page(rows)
            if col_centers is None:
                col_centers = (210.0, 235.0, 305.0)
                notes.append(f"[WARN] {stem} page {pi+1}: could not infer column centers; using fallback {col_centers}")

            recon_path = debug_dir / f"reconstructed_table_page_{pi+1}.txt"
            with open(recon_path, "w", encoding="utf-8") as f:
                f.write(f"PDF: {pdf_path}\n")
                f.write(f"Title page: {title_i+1} | Table start page: {table_start_i+1}\n")
                f.write(f"Parsing page: {pi+1} (crop_right_ratio={CROP_RIGHT_RATIO})\n")
                f.write(f"Decimal-only numeric centers (xc): {col_centers}\n")
                f.write("label | pref_hit | zenkoku_hit | brand | nums_by_x(decimal-only)\n")
                f.write("-" * 160 + "\n")

                for y in sorted(rows.keys()):
                    wds = sorted(rows[y], key=lambda z: z["x0"])
                    pref_w = [wd for wd in wds if wd["x0"] < PREF_X_MAX]
                    brand_w = [wd for wd in wds if PREF_X_MAX <= wd["x0"] < BRAND_X_MAX]
                    
                    pref_text = join_text(pref_w)
                    brand_text = join_text(brand_w)
                    label = detect_row_label(pref_text, brand_text)

                    if not label:
                        continue
                    if any(hw in label for hw in HDR_WORDS):
                        continue
                    if "資料" in label or "注" in label:
                        continue

                    pref_hit = is_prefecture_label(label)
                    z_hit = is_zenkoku_label(label)
                    nums = extract_numbers_by_columns_decimal_only(wds, col_centers)
                    brand_dbg = "general" if (pref_hit or z_hit) else clean_brand_label(label)
                    f.write(f"{label} | {pref_hit or ''} | {'YES' if z_hit else ''} | {brand_dbg} | {nums or ''} | pref_col='{pref_text}' brand_col='{brand_text}'\n")

            # CRITICAL FIX: Parse with better logic
            for y in sorted(rows.keys()):
                wds = sorted(rows[y], key=lambda z: z["x0"])
                pref_w = [wd for wd in wds if wd["x0"] < PREF_X_MAX]
                brand_w = [wd for wd in wds if PREF_X_MAX <= wd["x0"] < BRAND_X_MAX]
                
                pref_text = join_text(pref_w)
                brand_text = join_text(brand_w)
                label = detect_row_label(pref_text, brand_text)

                if not label:
                    continue
                if any(hw in label for hw in HDR_WORDS):
                    continue
                if "資料" in label or "注" in label:
                    continue

                pref_hit = is_prefecture_label(label)
                z_hit = is_zenkoku_label(label)

                nums = extract_numbers_by_columns_decimal_only(wds, col_centers)

                # Keep prefecture/全国 even if empty
                if nums is None:
                    if pref_hit or z_hit:
                        n1 = n2 = n3 = float("nan")
                    else:
                        continue
                else:
                    n1, n2, n3 = nums

                # CRITICAL FIX: Better prefecture/brand assignment
                if z_hit:
                    if zenkoku_seen:
                        continue
                    current_pref = "全国"
                    brand = "general"
                    zenkoku_seen = True

                elif pref_hit:
                    # This is a prefecture row
                    current_pref = pref_hit
                    brand = "general"

                else:
                    # This might be a brand row
                    if not current_pref:
                        # No current prefecture set, skip
                        continue
                    if current_pref == "全国" and zenkoku_seen:
                        # Already processed 全国
                        continue
                    
                    # Use the label as brand name
                    brand = clean_brand_label(label)
                    if not brand:
                        continue

                records.append(
                    {
                        "prefecture": current_pref,
                        "brand": brand,
                        "集荷数量": n1,
                        "契約数量": n2,
                        "販売数量": n3,
                        "page": pi + 1,
                        "y": y,
                    }
                )

            # Annotated image
            try:
                im = crop.to_image(resolution=200)
                for wd in words:
                    im.draw_rect(wd, stroke="red", fill=None, stroke_width=1)
                im.save(debug_dir / f"annotated_page_{pi+1}.png", format="PNG")
            except Exception as e:
                notes.append(f"[WARN] Could not save annotated image for {stem} page {pi+1}: {e}")

    df = pd.DataFrame(records)
    return df, debug_dir, (title_i + 1), (table_start_i + 1), [p + 1 for p in page_idxs], notes


_MONTH_RE = re.compile(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b")
def month_from_stem(stem: str):
    s = (stem or "").replace("_", " ").replace("-", " ")
    m = _MONTH_RE.search(s)
    if not m:
        m2 = re.search(r"(?i)(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", stem or "")
        if not m2:
            return None
        mon = m2.group(1).lower()
    else:
        mon = m.group(1).lower()
    return mon.capitalize()


def iter_pdfs(raw_root: Path):
    for y in YEAR_FOLDERS:
        if y not in ONLY_PROCESS:
            continue

        year_dir = raw_root / y
        if not year_dir.exists():
            continue

        allowed_months = ONLY_PROCESS[y]

        for pdf_path in sorted(year_dir.glob("*.pdf")):
            stem = pdf_path.stem
            mon = month_from_stem(stem)
            if mon is None:
                continue
            if mon not in allowed_months:
                continue
            yield pdf_path, y


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log_path = OUT_DIR / "batch_actual_sales_log.txt"

    pdfs = list(iter_pdfs(RAW_ROOT))
    if not pdfs:
        print(f"[ERROR] No matching PDFs found under: {RAW_ROOT}")
        print(f"Filter ONLY_PROCESS={ONLY_PROCESS}")
        return

    ok = 0
    fail = 0

    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"RAW_ROOT={RAW_ROOT}\nOUT_DIR={OUT_DIR}\n")
        log.write(f"TITLE={TABLE_TITLE_KEYWORD}\nCROP_RIGHT_RATIO={CROP_RIGHT_RATIO}\n")
        log.write(f"MAX_FORWARD_SCAN_FROM_TITLE={MAX_FORWARD_SCAN_FROM_TITLE}\n")
        log.write(f"PAGES_TO_CAPTURE_AFTER_TABLE_START={PAGES_TO_CAPTURE_AFTER_TABLE_START}\n")
        log.write("NUMBERS: decimal-only tokens\n")
        log.write(f"ONLY_PROCESS={ONLY_PROCESS}\n")
        log.write(f"PREF_X_MAX={PREF_X_MAX}, BRAND_X_MAX={BRAND_X_MAX}\n")
        log.write("=" * 80 + "\n\n")

        for i, (pdf_path, year_str) in enumerate(pdfs, start=1):
            stem = pdf_path.stem

            year_out_dir = OUT_DIR / year_str
            year_out_dir.mkdir(parents=True, exist_ok=True)

            try:
                print(f"\n===== ({i}/{len(pdfs)}) Processing: {pdf_path} =====")
                df, debug_dir, title_page, table_start_page, pages_used, notes = parse_one_pdf(pdf_path, year_out_dir)

                out_xlsx = year_out_dir / f"actual_sales_{stem}.xlsx"

                if not df.empty:
                    df_out = df[["prefecture", "brand", "集荷数量", "契約数量", "販売数量"]].copy()
                else:
                    df_out = df.copy()

                df_out.to_excel(out_xlsx, index=False)

                ok += 1
                print(f"✅ Saved XLSX: {out_xlsx}")
                print(f"✅ Title page: {title_page} | Table start: {table_start_page} | Pages used: {pages_used}")
                print(f"✅ Debug: {debug_dir}")

                log.write(f"[OK] {pdf_path}\n")
                log.write(f"     Year out: {year_out_dir}\n")
                log.write(f"     Title page: {title_page}, Table start: {table_start_page}, Pages used: {pages_used}\n")
                log.write(f"     Rows: {len(df_out)}\n")
                log.write(f"     Out: {out_xlsx}\n")
                log.write(f"     Debug: {debug_dir}\n")
                if notes:
                    for n in notes:
                        log.write(f"     {n}\n")
                log.write("\n")

            except Exception as e:
                fail += 1
                print(f"❌ FAILED: {pdf_path} -> {e}")
                log.write(f"[FAIL] {pdf_path}\n")
                log.write(f"       Error: {repr(e)}\n")
                log.write(traceback.format_exc() + "\n\n")

    print("\n" + "=" * 60)
    print(f"Done. OK={ok}, FAIL={fail}")
    print(f"Batch log: {log_path}")
    print(f"Outputs: {OUT_DIR} (grouped by year)")
    print("=" * 60)


if __name__ == "__main__":
    main()