#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import base64
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import pandas as pd
from anthropic import Anthropic

# =========================
# SETTINGS (EDIT THESE)
# =========================
INPUT_DIR = Path("exante_conract")
OUT_DIR = Path("ec_claude")

# Claude model - Sonnet 4 is best for accuracy/cost balance
# Options: "claude-sonnet-4-20250514", "claude-opus-4-20250514"
MODEL = os.getenv("ANTHROPIC_TABLE_MODEL", "claude-sonnet-4-20250514")

# Max tokens for response
MAX_TOKENS = 8000


# =========================
# HELPERS
# =========================
def is_image_file(p: Path) -> bool:
    return p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}

def list_images(input_dir: Path) -> List[Path]:
    """
    Recursively find all image files in subdirectories
    """
    if not input_dir.exists():
        raise FileNotFoundError(f"Directory not found: {input_dir}")
    
    if not input_dir.is_dir():
        raise ValueError(f"Not a directory: {input_dir}")
    
    # Find all images recursively
    imgs = sorted([p for p in input_dir.rglob("*") if p.is_file() and is_image_file(p)])
    
    if not imgs:
        raise ValueError(f"No image files found under: {input_dir}")
    
    return imgs

def get_image_media_type(img_path: Path) -> str:
    """Get the correct media type for Claude API"""
    suffix = img_path.suffix.lower()
    media_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif"
    }
    return media_types.get(suffix, "image/jpeg")

def encode_image(img_path: Path) -> str:
    """Encode image to base64"""
    return base64.b64encode(img_path.read_bytes()).decode("utf-8")

def extract_json_from_response(text: str) -> str:
    """Extract JSON from Claude's response"""
    text = text.strip()
    
    # Remove markdown code blocks
    text = re.sub(r'^```json\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^```\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*```$', '', text, flags=re.MULTILINE)
    text = text.strip()
    
    # If it's clean JSON
    if (text.startswith("{") and text.endswith("}")) or (text.startswith("[") and text.endswith("]")):
        return text
    
    # Find JSON object
    json_match = re.search(r'\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}', text, re.DOTALL)
    if json_match:
        return json_match.group(0)
    
    # Last resort
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        return text[start:end+1]
    
    raise ValueError(f"Could not extract JSON. First 300 chars:\n{text[:300]}")

def validate_and_normalize_table(obj: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the extracted table data"""
    if "columns" not in obj:
        raise ValueError("Missing 'columns' in extracted data")
    
    cols = obj.get("columns", [])
    rows = obj.get("rows", [])
    
    if not cols:
        raise ValueError("No columns extracted from table")
    
    if not rows:
        print("  Warning: No data rows extracted (table might be empty or header-only)")
        return obj
    
    # Normalize all rows to match column count
    col_count = len(cols)
    normalized_rows = []
    
    for i, row in enumerate(rows):
        if not isinstance(row, list):
            print(f"  Warning: Row {i} is not a list, skipping")
            continue
        
        # Pad short rows with None
        if len(row) < col_count:
            row = row + [None] * (col_count - len(row))
            print(f"  Warning: Row {i} had only {len(row)} cells, padded to {col_count}")
        # Truncate long rows
        elif len(row) > col_count:
            print(f"  Warning: Row {i} had {len(row)} cells, truncated to {col_count}")
            row = row[:col_count]
        
        normalized_rows.append(row)
    
    obj["rows"] = normalized_rows
    return obj

def table_to_dataframe(obj: Dict[str, Any]) -> pd.DataFrame:
    """Convert validated table JSON to DataFrame"""
    cols = obj.get("columns", [])
    rows = obj.get("rows", [])
    
    if not cols:
        return pd.DataFrame()
    
    return pd.DataFrame(rows, columns=cols)

def safe_sheet_name(name: str) -> str:
    """Create valid Excel sheet name (max 31 chars, no special chars)"""
    name = re.sub(r"[:\\/?*\[\]]", "_", name)
    return name[:31] if len(name) > 31 else name

def extract_table_with_claude(client: Anthropic, img_path: Path, retry_count: int = 2) -> Dict[str, Any]:
    """
    Extract table using Claude API with retry logic
    """
    media_type = get_image_media_type(img_path)
    image_data = encode_image(img_path)
    
    prompt = """I need you to extract the complete table from this image with 100% accuracy.

Please follow these steps carefully:

1. EXAMINE the table structure:
   - Count the total number of columns
   - Count the total number of rows (including headers)
   - Note any merged cells, multi-line headers, or special formatting

2. EXTRACT all data systematically:
   - Start with column headers (from left to right)
   - Then extract each row from top to bottom
   - For each row, extract every cell from left to right
   - Preserve the exact text as shown (including numbers, punctuation, Japanese/Chinese characters)
   - If a cell is blank/empty, use null
   - If a cell has multiple lines of text, keep them together as one string

3. RETURN the result as JSON in this exact format:
{
  "title": "<table title if visible, else null>",
  "columns": ["col1", "col2", "col3", ...],
  "rows": [
    ["cell1", "cell2", "cell3", ...],
    ["cell4", "cell5", "cell6", ...],
    ...
  ],
  "metadata": {
    "total_columns": <number>,
    "total_rows": <number>,
    "notes": "<any observations: merged cells, unclear text, etc.>"
  }
}

CRITICAL REQUIREMENTS:
- Extract EVERY SINGLE ROW without skipping
- Extract EVERY SINGLE COLUMN without skipping
- Each row must have exactly the same number of elements as columns
- Preserve original text exactly (do not translate or modify)
- Return ONLY the JSON object, no explanations before or after

Begin the extraction now."""

    for attempt in range(retry_count + 1):
        try:
            print(f"  Attempt {attempt + 1}/{retry_count + 1}...")
            
            message = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": image_data,
                                },
                            },
                            {
                                "type": "text",
                                "text": prompt
                            }
                        ],
                    }
                ],
            )
            
            # Extract text from response
            response_text = message.content[0].text
            
            if attempt == 0:
                print(f"  Response length: {len(response_text)} chars")
            
            # Parse JSON
            json_str = extract_json_from_response(response_text)
            obj = json.loads(json_str)
            
            # Validate and normalize
            obj = validate_and_normalize_table(obj)
            
            # Print extraction info
            metadata = obj.get("metadata", {})
            cols_count = len(obj.get("columns", []))
            rows_count = len(obj.get("rows", []))
            
            print(f"  ✓ Extracted: {cols_count} columns × {rows_count} rows")
            if metadata.get("notes"):
                print(f"  Notes: {metadata['notes']}")
            
            return obj
            
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  ✗ Parsing failed: {e}")
            if attempt == retry_count:
                if 'response_text' in locals():
                    print(f"  Last response (first 500 chars):\n{response_text[:500]}")
                raise
                
        except Exception as e:
            print(f"  ✗ API error: {e}")
            if attempt == retry_count:
                raise
    
    raise RuntimeError("All extraction attempts failed")


# =========================
# MAIN
# =========================
def main():
    # Check for API key
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set. Please run:\n"
            "export ANTHROPIC_API_KEY='your-key-here'"
        )
    
    print(f"Using Claude model: {MODEL}")
    print(f"Input directory: {INPUT_DIR}")
    print(f"Output directory: {OUT_DIR}\n")
    
    # Setup
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = Anthropic(api_key=api_key)
    images = list_images(INPUT_DIR)
    
    print(f"Found {len(images)} image(s) to process\n")
    print("=" * 70)

    # Track results
    summary = []

    # Process each image
    for idx, img in enumerate(images, 1):
        print(f"\n[{idx}/{len(images)}] {img.name}")
        print(f"  Path: {img.parent}")
        print("-" * 70)

        try:
            # Extract table
            table_data = extract_table_with_claude(client, img)
            
            # Convert to DataFrame
            df = table_to_dataframe(table_data)
            title = table_data.get("title")
            metadata = table_data.get("metadata", {})
            notes = metadata.get("notes")
            
            # Output filename: same as input (e.g., Jan2017_ec.xlsx)
            out_xlsx = OUT_DIR / f"{img.stem}.xlsx"
            
            with pd.ExcelWriter(out_xlsx, engine="openpyxl") as writer:
                # Main table sheet - use simple name
                df.to_excel(writer, index=False, sheet_name="Table")
                
                # Metadata sheet
                meta_df = pd.DataFrame({
                    "Property": ["Source File", "Source Path", "Title", "Columns", "Rows", "Notes"],
                    "Value": [
                        img.name,
                        str(img.parent),
                        title or "N/A",
                        len(table_data.get("columns", [])),
                        len(table_data.get("rows", [])),
                        notes or "None"
                    ]
                })
                meta_df.to_excel(writer, index=False, sheet_name="Info")
            
            # Track success
            summary.append({
                "file": img.name,
                "year": img.parent.name,
                "status": "SUCCESS",
                "output": str(out_xlsx),
                "columns": len(table_data.get("columns", [])),
                "rows": len(table_data.get("rows", [])),
                "notes": notes
            })
            
            print(f"  ✓ Saved to: {out_xlsx}")
            
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            summary.append({
                "file": img.name,
                "year": img.parent.name,
                "status": "FAILED",
                "output": None,
                "columns": 0,
                "rows": 0,
                "notes": str(e)
            })
    
    # Print summary
    print("\n" + "=" * 70)
    print("EXTRACTION SUMMARY")
    print("=" * 70)
    
    successful = sum(1 for s in summary if s["status"] == "SUCCESS")
    failed = len(summary) - successful
    
    print(f"\nTotal: {len(summary)} | Success: {successful} | Failed: {failed}\n")
    
    # Group by year for better readability
    from itertools import groupby
    summary_sorted = sorted(summary, key=lambda x: (x["year"], x["file"]))
    
    for year, results in groupby(summary_sorted, key=lambda x: x["year"]):
        print(f"\n{year}:")
        print("-" * 70)
        for result in results:
            status_icon = "✓" if result["status"] == "SUCCESS" else "✗"
            print(f"  {status_icon} {result['file']}")
            if result["status"] == "SUCCESS":
                print(f"      {result['columns']} columns × {result['rows']} rows")
                if result["notes"]:
                    print(f"      Notes: {result['notes']}")
            else:
                print(f"      Error: {result['notes']}")
    
    print("\n" + "=" * 70)
    print(f"All outputs saved to: {OUT_DIR}")
    print("=" * 70)


if __name__ == "__main__":
    main()