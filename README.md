# README: Nippon Rice Data Extraction

It extracts structured tables from monthly Japanese rice market PDF reports covering Jan 2017 to Nov 2025.

For each PDF, we aim to extract four table types:
- Ex-ante contract (事前契約)
- Price (相対取引価格・数量)
- Actual sales (産地別契約・販売状況)
- Inventory (産地別民間在庫の推移)

The data is automatically downloaded from: https://www.maff.go.jp/j/seisan/keikaku/soukatu/mr.html.

## Repository Structure

The project root folder is NP_Rice_Extraction. Raw PDFs are stored under raw_data:

```
NP_Rice_Extraction/
raw_data/
2017/
Jan2017.pdf, Feb2017.pdf, ...
...
2025/
... up to Nov2025.pdf
```

## General Extraction Mechanism

Most scripts use pdfplumber to read PDFs and extract tables using a keyword-based workflow:

1. Search pages using predefined title keywords (anchors) to locate the target table.
2. Extract the table region and parse rows/columns into structured data.
3. Export results to XLSX (one file per PDF).

If extraction fails, the most common fixes are:
- Adjust the keywords used to locate the anchor page(s).
- Expand the scan window (e.g., include a few pages after the anchor).
- Tune row/column heuristics (especially when PDF formatting changes).

Note: Manual Validation

Some PDFs contain notes or extra numbers near the bottom of the page. In certain cases (especially for Inventory), the parser may mistakenly treat these numbers as part of the table, shifting the detected table boundary and causing row misalignment.

Recommendation: manually verify outputs by comparing the last row in the extracted table against the corresponding PDF page.

## Single-PDF Debug

These scripts are intended for testing extraction on one specific PDF (useful for debugging and parameter tuning):
- price_test.py
- actual_sales.py
- inventory.py

## Price Extraction

Script: full_price_exraction.py
- Batch extracts tables whose title starts with 相対取引価格・数量.
- Output: one XLSX per PDF:

```
raw_data/2017/Jan2017.pdf

->

price/price_Jan2017.xlsx
```

## Inventory Extraction

Scripts:
- full_inventory_extraction.py (normal cases)
- inventory_splitted.py (special cases: a single page contains two split inventory tables)

The split-table months are:

Sep2020, Oct2020, Nov2020, Dec2020, Jan2021, Feb2021,
Sep2021, Oct2021, Nov2021, Dec2021, Jan2022, Feb2022,
Sep2022, Oct2022, Nov2022, Dec2022, Jan2023, Feb2023

## Actual Sales Extraction

Script: full_actual_sales.py
- Batch extraction for the “actual sales” table across PDFs.
- Uses the same keyword-based anchor mechanism + pdfplumber table parsing.

## Ex-ante Contract Extraction (Claude-assisted)

Script: claude_extraction.py
- Ex-ante contract is the main exception to the standard pipeline, as the format of this table varies considerably from year to year.
- Table structuring is performed via the Claude API to obtain the full table when formats differ substantially across PDFs.
