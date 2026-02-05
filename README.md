# README: Nippon Rice Data Extraction

This project extracts structured tables from monthly Japanese rice market PDF reports covering Jan 2017 to Nov 2025.

For each PDF, we aim to extract four table types:
- Ex-ante contract (事前契約)
- Price (相対取引価格・数量)
- Actual sales (産地別契約・販売状況)
- Inventory (産地別民間在庫の推移)

The source PDFs are from:
https://www.maff.go.jp/j/seisan/keikaku/soukatu/mr.html

## Repository Structure

```
NP_Rice/
raw_data/
  2017/
    Jan2017.pdf, Feb2017.pdf, ...
  ...
  2025/
    ... up to Nov2025.pdf
Results/
  (extracted XLSX outputs)
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

## Price Extraction

Script: full_price_extraction.py
- Batch extracts tables whose title starts with 相対取引価格・数量.
- Outputs one XLSX per PDF, named price_{pdf_stem}.xlsx.

Note: this script currently uses absolute paths for RAW_ROOT and OUT_DIR. Update those paths before running.

Example output naming:

```
raw_data/2017/Jan2017.pdf

->

price/price_Jan2017.xlsx
```

## Inventory Extraction

Script: full_inventory_extraction.py
- Batch extracts inventory tables with title keyword 産地別民間在庫の推移.
- Writes outputs to inventory_new1/ (plus debug/ and debug_images/).
- Logic is designed for PDFs AFTER Feb 2022 (see script header).

## Actual Sales Extraction

Script: full_actual_sales.py
- Batch extraction for the “actual sales” table across PDFs.
- Uses the same keyword-based anchor mechanism + pdfplumber table parsing.
- Writes outputs to actual_sales1/.

## Ex-ante Contract Extraction (Claude-assisted)

Script: claude_ec_extraction.py
- Ex-ante contract is the main exception to the standard pipeline, as the format of this table varies considerably from year to year.
- This script reads images under exante_conract/ and uses the Claude API to return a table as JSON, then exports to Excel.
- Outputs are written to ec_claude/.

Note: set your Anthropic key and model via environment variables as required by the script.
