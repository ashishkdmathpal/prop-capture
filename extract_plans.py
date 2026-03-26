"""
extract_plans.py — Extract structured labels from confirmed floor plan pages.

Uses OpenRouter vision (llama-4-scout) to identify BHK type, carpet area, tower, etc.
Renders confirmed pages at 150 DPI and saves labeled images to:
  output/<source>/unit-plan/<label>.jpg
  output/<source>/master-plan/<label>.jpg
  output/<source>/floor-plan/<label>.jpg
"""

import os
import re
import base64
import json
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("/root/.secrets.env")

import requests
import fitz  # PyMuPDF

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
EXTRACT_DPI = 150   # higher quality for extraction

EXTRACT_PROMPT = """You are analyzing a floor plan image from an Indian real estate brochure.
This page has been identified as a: {plan_type}

Page text extracted from PDF:
{page_text}

Extract the following information and return ONLY valid JSON (no markdown fences, no explanation):

{{
  "plan_type": "unit_plan or master_plan or floor_plan",
  "unit_type": "1BHK or 2BHK or 3BHK or 4BHK or Studio or Penthouse or null",
  "variant": "Type A or Luxury or Windsor or null (variant name if multiple exist for same BHK)",
  "carpet_area": "e.g. 750 sq ft or 125.64 sq m or null",
  "saleable_area": "e.g. 950 sq ft or null",
  "tower": "e.g. Tower 1 or Tower A or Block 1 or null",
  "series": "e.g. X01 or X02 or null (unit series code if present)",
  "floor_range": "e.g. Floors 5-20 or null",
  "label": "filename-safe label, lowercase, hyphens only, max 50 chars e.g. tower1-3bhk-luxury-x01-125sqm",
  "confidence": "high or medium or low",
  "notes": "any other relevant details or null"
}}

Rules:
- For master plans: unit_type, carpet_area, saleable_area, series are null. Label like 'master-plan-ground-level'.
- For floor plans showing multiple units: label like 'floor-plan-tower1-3bhk-typical'.
- The label must be unique, descriptive, and safe for filenames (no spaces, no special chars except hyphens).
- Use information from BOTH the image AND the page text.
- If carpet area appears in sq m, also note it (e.g. "125.64 sq m / 1352 sq ft").
"""

PLAN_TYPE_DIR_MAP = {
    "unit_plan": "unit-plan",
    "master_plan": "master-plan",
    "floor_plan": "floor-plan",
}

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_page_hires(
    doc: fitz.Document,
    page_num: int,   # 1-indexed
    dpi: int = EXTRACT_DPI,
    pages_dir: Path | None = None,
) -> bytes:
    """
    Render a page at higher DPI for extraction quality.
    Falls back to cached thumbnail only if high-res not available at same DPI.
    Always renders fresh at extract DPI for best quality.
    """
    page = doc[page_num - 1]  # fitz is 0-indexed
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("jpeg")


# ---------------------------------------------------------------------------
# OpenRouter extraction
# ---------------------------------------------------------------------------

def _call_extract(
    api_key: str,
    img_bytes: bytes,
    page_text: str,
    plan_type: str,
    page_num: int,
) -> dict:
    """
    Send one page to OpenRouter for detailed extraction.
    Returns structured plan metadata dict.
    """
    img_b64 = base64.b64encode(img_bytes).decode()

    # Truncate page text to avoid token limits
    truncated_text = page_text[:2000] if page_text else "(no text on page)"

    prompt = EXTRACT_PROMPT.format(
        plan_type=plan_type,
        page_text=truncated_text,
    )

    response = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    {"type": "text", "text": prompt},
                ]
            }],
            "max_tokens": 600,
            "temperature": 0.1,
        },
    )
    response.raise_for_status()

    raw = response.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(l for l in lines if not l.startswith("```")).strip()

    try:
        data = json.loads(raw)
        data["page_num"] = page_num
        return data
    except json.JSONDecodeError as e:
        print(f"    Warning: JSON parse error on page {page_num}: {e}")
        return {
            "page_num": page_num,
            "plan_type": plan_type,
            "unit_type": None,
            "variant": None,
            "carpet_area": None,
            "saleable_area": None,
            "tower": None,
            "series": None,
            "floor_range": None,
            "label": f"page-{page_num}-{plan_type.replace('_', '-')}",
            "confidence": "low",
            "notes": f"parse_error: {raw[:200]}",
        }


# ---------------------------------------------------------------------------
# Save helper
# ---------------------------------------------------------------------------

def _sanitize_label(label: str) -> str:
    """Make label safe for filenames."""
    label = label.lower().strip()
    label = re.sub(r"[^a-z0-9\-]", "-", label)
    label = re.sub(r"-+", "-", label)
    label = label.strip("-")
    return label[:60] or "plan"


def _save_plan_image(
    img_bytes: bytes,
    plan_info: dict,
    output_dir: Path,
    used_labels: set,
) -> str:
    """
    Save the rendered page as a labeled image.
    Returns the saved file path.
    """
    plan_type = plan_info.get("plan_type", "floor_plan")
    subdir_name = PLAN_TYPE_DIR_MAP.get(plan_type, "floor-plan")
    subdir = output_dir / subdir_name
    subdir.mkdir(parents=True, exist_ok=True)

    raw_label = plan_info.get("label") or f"page-{plan_info.get('page_num', 0)}"
    label = _sanitize_label(raw_label)

    # Ensure uniqueness
    base_label = label
    counter = 2
    while label in used_labels:
        label = f"{base_label}-{counter}"
        counter += 1
    used_labels.add(label)

    filepath = subdir / f"{label}.jpg"
    with open(filepath, "wb") as f:
        f.write(img_bytes)

    return str(filepath)


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_plan_labels(
    pdf_path: str,
    confirmed_pages: list[dict],  # from vision_screen: [{page, type, confidence, note}, ...]
    output_dir: str | Path,
    page_texts: dict | None = None,  # {page_num: text} from screen.py (optional)
    dpi: int = EXTRACT_DPI,
    verbose: bool = True,
) -> list[dict]:
    """
    For each confirmed plan page:
    1. Render at high DPI
    2. Send image + page text to OpenRouter
    3. Get back structured metadata
    4. Save the image with a descriptive filename

    Args:
        pdf_path: Path to PDF
        confirmed_pages: Pages classified as unit_plan/master_plan/floor_plan
        output_dir: Base output directory (will create subfolders)
        page_texts: Pre-extracted text per page (from screen.py)
        dpi: Render DPI (default 150)
        verbose: Print progress

    Returns:
        List of extraction results with file paths added.
    """
    if not confirmed_pages:
        return []

    from concurrent.futures import ThreadPoolExecutor, as_completed

    output_dir = Path(output_dir)
    api_key = os.getenv("OPENROUTER_API_KEY")
    doc = fitz.open(pdf_path)
    MAX_WORKERS = 4

    if verbose:
        print(f"  Extracting labels from {len(confirmed_pages)} plan pages at {dpi} DPI (parallel={MAX_WORKERS})...")

    # Phase 1: Render all pages upfront
    rendered = {}
    render_errors = []
    for page_info in confirmed_pages:
        page_num = page_info.get("page")
        try:
            rendered[page_num] = render_page_hires(doc, page_num, dpi=dpi)
        except Exception as e:
            print(f"  RENDER ERROR page {page_num}: {e}")
            render_errors.append({
                "page_num": page_num,
                "plan_type": page_info.get("type", "floor_plan"),
                "error": str(e),
                "label": f"page-{page_num}-error",
                "file_path": None,
            })
    doc.close()

    # Phase 2: Parallel API calls
    def _extract_one(page_info):
        page_num = page_info.get("page")
        plan_type = page_info.get("type", "floor_plan")
        img_bytes = rendered.get(page_num)
        if img_bytes is None:
            return page_info, None, None

        t0 = time.time()
        try:
            extracted = _call_extract(api_key, img_bytes,
                                      (page_texts or {}).get(page_num, ""),
                                      plan_type, page_num)
        except Exception as e:
            extracted = {
                "page_num": page_num,
                "plan_type": plan_type,
                "label": f"page-{page_num}-{plan_type.replace('_', '-')}",
                "confidence": "low",
                "notes": f"api_error: {str(e)[:100]}",
            }
        elapsed = time.time() - t0
        return page_info, extracted, elapsed

    api_results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_extract_one, pi): pi for pi in confirmed_pages if pi.get("page") in rendered}
        for future in as_completed(futures):
            page_info, extracted, elapsed = future.result()
            if extracted is not None:
                api_results.append((page_info, extracted, elapsed))

    # Phase 3: Sequential save (for label uniqueness) — maintain page order
    api_results.sort(key=lambda x: x[0].get("page", 0))
    results = list(render_errors)
    used_labels = set()

    for page_info, extracted, elapsed in api_results:
        page_num = page_info.get("page")
        plan_type = page_info.get("type", "floor_plan")
        img_bytes = rendered[page_num]

        try:
            file_path = _save_plan_image(img_bytes, extracted, output_dir, used_labels)
            extracted["file_path"] = file_path
        except Exception as e:
            print(f"  SAVE ERROR page {page_num}: {e}")
            extracted["file_path"] = None

        if verbose:
            label = extracted.get("label", "?")
            conf = extracted.get("confidence", "?")
            print(f"  Page {page_num} ({plan_type}) -> {label} [{conf}] ({elapsed:.1f}s)")

        extracted["vision_type"] = plan_type
        extracted["vision_confidence"] = page_info.get("confidence")
        extracted["vision_note"] = page_info.get("note")
        results.append(extracted)

    if verbose:
        saved = [r for r in results if r.get("file_path")]
        print(f"  Saved {len(saved)}/{len(confirmed_pages)} plan images")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python extract_plans.py <pdf_path> <output_dir> [page_nums...]")
        print("  e.g.: python extract_plans.py input/listing.pdf output/listing 26 27 28")
        sys.exit(1)

    pdf = sys.argv[1]
    out = sys.argv[2]
    pages = [int(x) for x in sys.argv[3:]] if len(sys.argv) > 3 else [26, 27, 28]

    confirmed = [{"page": p, "type": "unit_plan", "confidence": "high", "note": "cli test"} for p in pages]
    results = extract_plan_labels(pdf, confirmed, out, verbose=True)

    print("\n--- Extraction Results ---")
    for r in results:
        print(f"  Page {r.get('page_num')}: {r.get('label')} -> {r.get('file_path')}")
