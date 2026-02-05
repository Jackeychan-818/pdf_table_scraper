#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Batch extract tables whose title starts with "相対取引価格・数量" from MANY PDFs.

Folder structure:
  RAW_ROOT/
    2017/
      Jan2017.pdf, Feb2017.pdf, ...
    ...
    2025/
      ... up to Nov2025.pdf

Output:
  Writes one XLSX per PDF into OUT_DIR, named:
    price_{pdf_stem}.xlsx
  Example:
    raw_data/2017/Jan2017.pdf -> price/price_Jan2017.xlsx

Requirements:
  pip install pdfplumber pandas openpyxl
"""

import os
import re
import sys
from pathlib import Path

import pdfplumber
import pandas as pd

# =========================
# SETTINGS (EDIT THESE)
# =========================
RAW_ROOT = Path("/Users/jackey/NUS Dropbox/Zeqi Chen/Tiffany/raw_data")

OUT_DIR = Path("/Users/jackey/NUS Dropbox/Zeqi Chen/Tiffany/pricelist")

TITLE_PREFIX = "相対取引価格・数量"
SHEET_NAME = "Sheet1"
MASTER_HEADER = ["prefecture", "brand", "価格", "数量"]

# If your PDF has very dense layout, these help table detection:
TABLE_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "edge_min_length": 3,
    "min_words_vertical": 1,
    "min_words_horizontal": 1,
    "intersection_tolerance": 3,
}

VALID_PREFECTURES = {
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
}

# =========================
# HELPERS
# =========================
def looks_like_number(s: str) -> bool:
    if s is None:
        return False
    s = str(s).strip()
    if s == "":
        return False
    s = s.replace(",", "")
    return bool(re.fullmatch(r"[-+]?\d+(\.\d+)?", s))


def clean_cell(x):
    if x is None:
        return ""
    x = str(x)
    x = re.sub(r"\s+", " ", x).strip()
    return x


def page_has_target_title(page) -> bool:
    """
    Check top area text for a line starting with TITLE_PREFIX.
    Crop top ~22% to reduce false matches.
    """
    try:
        w, h = page.width, page.height
        top = page.crop((0, 0, w, h * 0.22))
        txt = top.extract_text() or ""
    except Exception:
        txt = page.extract_text() or ""

    for line in (txt or "").splitlines():
        if line.strip().startswith(TITLE_PREFIX):
            return True
    return False


def normalize_rows(raw_rows):
    """
    raw_rows: list of lists from pdfplumber tables
    Output: list[dict] with MASTER_HEADER keys
    """
    out = []
    for r in raw_rows:
        if not r or all((clean_cell(x) == "" for x in r)):
            continue

        cells = [clean_cell(x) for x in r]

        while len(cells) > 0 and cells[-1] == "":
            cells.pop()

        if len(cells) < 3:
            continue

        c0 = cells[0] if len(cells) > 0 else ""
        c1 = cells[1] if len(cells) > 1 else ""
        c2 = cells[2] if len(cells) > 2 else ""
        c3 = cells[3] if len(cells) > 3 else ""
        c4 = cells[4] if len(cells) > 4 else ""

        # Skip header-ish rows
        if any(k in c0 for k in ["都道府県", "銘柄", "価格", "数量"]) or c0 == "産地":
            continue

        prefecture = ""
        brand = ""
        price = ""
        qty = ""

        # Case A: prefecture=c0, brand=c1, price=c2, qty=c3
        if c0 != "" and c1 != "":
            prefecture = c0
            brand = c1
            price = c2
            qty = c3

        # Case B: c0="北海道 ななつぼし"
        if prefecture == "" or brand == "":
            m = re.match(r"^(\S+)\s+(.+)$", c0)
            if m:
                prefecture = m.group(1)
                brand = m.group(2).strip()
                price = c2
                qty = c3
            else:
                prefecture = c0
                brand = c1
                price = c2
                qty = c3

        # Fix price/qty swaps using rough magnitude heuristic
        p = price.replace(",", "")
        q = qty.replace(",", "")
        if looks_like_number(p) and looks_like_number(q):
            try:
                p_val = float(p)
                q_val = float(q)
                if q_val > 5000 and p_val < 1000:
                    price, qty = qty, price
            except Exception:
                pass
        else:
            p2 = c2.replace(",", "")
            q2 = c3.replace(",", "")
            if looks_like_number(q2) and looks_like_number(p2):
                try:
                    p2_val = float(p2)
                    q2_val = float(q2)
                    if q2_val > 5000 and p2_val < 1000:
                        price, qty = c3, c2
                except Exception:
                    pass

        if qty == "" and c4 != "":
            qty = c4

        if prefecture not in VALID_PREFECTURES:
            continue

        out.append({"prefecture": prefecture, "brand": brand, "価格": price, "数量": qty})

    return out


def extract_tables_from_page(page):
    tables = []
    try:
        tables = page.extract_tables(TABLE_SETTINGS) or []
    except Exception:
        tables = []

    if not tables:
        try:
            tables = page.extract_tables() or []
        except Exception:
            tables = []

    return tables


def extract_one_pdf(pdf_path: Path) -> tuple[pd.DataFrame, list[int]]:
    """
    Returns:
      df_out: DataFrame with exactly 4 columns (MASTER_HEADER) and all rows
      matched_pages: list of 1-indexed page numbers where title matched
    """
    matched_pages = []
    all_rows = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            if page_has_target_title(page):
                matched_pages.append(i)
                tables = extract_tables_from_page(page)
                for t in tables:
                    raw_rows = [[clean_cell(x) for x in row] for row in t]
                    all_rows.extend(raw_rows)

    records = normalize_rows(all_rows)
    df = pd.DataFrame(records, columns=MASTER_HEADER)
    df_out = df[MASTER_HEADER].copy()
    return df_out, matched_pages


def safe_stem(p: Path) -> str:
    # keep "Jan2017" etc, but sanitize just in case
    s = p.stem
    s = re.sub(r"[^\w\-]+", "_", s)
    return s


# =========================
# MAIN
# =========================
def main():
    if not RAW_ROOT.exists():
        print(f"ERROR: RAW_ROOT not found: {RAW_ROOT}")
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Collect PDFs under 4-digit year subfolders
    pdf_files = []
    for year_dir in sorted([p for p in RAW_ROOT.iterdir() if p.is_dir()]):
        if not re.fullmatch(r"\d{4}", year_dir.name):
            continue
        pdf_files.extend(sorted(year_dir.glob("*.pdf")))

    if not pdf_files:
        print(f"ERROR: No PDFs found under: {RAW_ROOT}")
        sys.exit(2)

    ok = 0
    fail = 0

    for pdf_path in pdf_files:
        date_tag = safe_stem(pdf_path)  # e.g., "Jan2017"
        out_xlsx = OUT_DIR / f"price_{date_tag}.xlsx"

        print(f"\n===== Processing: {pdf_path} =====")
        try:
            df_out, matched_pages = extract_one_pdf(pdf_path)

            if df_out.empty:
                print(f"⚠️  No usable rows extracted. Matched pages: {matched_pages}")
                fail += 1
                continue

            with pd.ExcelWriter(str(out_xlsx), engine="openpyxl") as writer:
                df_out.to_excel(writer, index=False, sheet_name=SHEET_NAME)

            print(f"✅ Wrote: {out_xlsx} (rows={len(df_out)}, matched_pages={matched_pages})")
            ok += 1

        except Exception as e:
            print(f"❌ Failed on {pdf_path.name}: {e}")
            fail += 1

    print("\n==========================")
    print(f"Done. Success: {ok}, Failed: {fail}")
    print(f"Output folder: {OUT_DIR}")


if __name__ == "__main__":
    main()