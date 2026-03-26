"""
extract_images.py — Extract structured labels from confirmed property image pages.

Uses OpenRouter vision (llama-4-scout) to identify amenities, exteriors, interiors, etc.
Renders confirmed pages at 150 DPI and saves labeled images to:
  output/<source>/amenity/<label>.jpg
  output/<source>/exterior/<label>.jpg
  output/<source>/interior/<label>.jpg
  output/<source>/location/<label>.jpg
  output/<source>/lifestyle/<label>.jpg
  output/<source>/specification/<label>.jpg
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

IMAGE_TYPE_DIR_MAP = {
    "amenity_image": "amenity",
    "exterior_image": "exterior",
    "interior_image": "interior",
    "location_map": "location",
    "lifestyle_image": "lifestyle",
    "specification_table": "specification",
}

EXTRACT_IMAGE_PROMPT = """You are analyzing an image page from an Indian real estate property brochure.
This page has been identified as a: {image_type}

Page text extracted from PDF:
{page_text}

Extract the following and return ONLY valid JSON (no markdown fences, no explanation):
{{
  "image_type": "{image_type}",
  "label": "descriptive-filename-safe-label-max-50-chars",
  "description": "one line description of what the image shows",
  "amenities_shown": ["pool", "gym"] or null (only for amenity_image),
  "confidence": "high or medium or low"
}}

Label rules:
- For amenity_image: label like "swimming-pool", "sky-gym", "clubhouse-banquet", "garden-play-area"
- For exterior_image: label like "tower-exterior-day", "entrance-lobby", "aerial-view", "night-view"
- For interior_image: label like "master-bedroom-render", "living-dining-room", "kitchen-render"
- For location_map: label like "distance-map" or "connectivity-table"
- For specification_table: label like "specs-flooring-bathroom" or "payment-plan"
- For lifestyle_image: label like "balcony-lifestyle-view", "terrace-lifestyle"
- The label must be filename-safe: lowercase, hyphens only, no spaces, max 50 chars
"""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_page_hires(
    doc: fitz.Document,
    page_num: int,   # 1-indexed
    dpi: int = EXTRACT_DPI,
) -> bytes:
    """Render a page at higher DPI for extraction quality."""
    page = doc[page_num - 1]  # fitz is 0-indexed
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("jpeg")


# ---------------------------------------------------------------------------
# OpenRouter extraction
# ---------------------------------------------------------------------------

def _call_extract_image(
    api_key: str,
    img_bytes: bytes,
    page_text: str,
    image_type: str,
    page_num: int,
) -> dict:
    """
    Send one image page to OpenRouter for labeling.
    Returns structured image metadata dict.
    """
    img_b64 = base64.b64encode(img_bytes).decode()

    # Truncate page text to avoid token limits
    truncated_text = page_text[:2000] if page_text else "(no text on page)"

    prompt = EXTRACT_IMAGE_PROMPT.format(
        image_type=image_type,
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
            "max_tokens": 400,
            "temperature": 0.1,
        },
    )
    response.raise_for_status()

    raw = response.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(l for l in lines if not l.startswith("```")).strip()

    try:
        data = json.loads(raw)
        data["page_num"] = page_num
        # Normalize image_type — use what API returned, fall back to what we sent
        if "image_type" not in data or not data["image_type"]:
            data["image_type"] = image_type
        return data
    except json.JSONDecodeError as e:
        print(f"    Warning: JSON parse error on page {page_num}: {e}")
        return {
            "page_num": page_num,
            "image_type": image_type,
            "label": f"page-{page_num}-{image_type.replace('_', '-')}",
            "description": None,
            "amenities_shown": None,
            "confidence": "low",
            "_parse_error": raw[:200],
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
    return label[:60] or "image"


def _save_image(
    img_bytes: bytes,
    image_info: dict,
    output_dir: Path,
    used_labels: set,
) -> str:
    """
    Save the rendered page as a labeled image.
    Returns the saved file path.
    """
    image_type = image_info.get("image_type", "other")
    # Map image_type to output subdirectory
    subdir_name = IMAGE_TYPE_DIR_MAP.get(image_type, "other")
    subdir = output_dir / subdir_name
    subdir.mkdir(parents=True, exist_ok=True)

    raw_label = image_info.get("label") or f"page-{image_info.get('page_num', 0)}"
    label = _sanitize_label(raw_label)

    # Ensure uniqueness across all image types
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

def extract_image_labels(
    pdf_path: str,
    confirmed_pages: list[dict],   # [{page, type, confidence, note}, ...]
    output_dir: str | Path,
    page_texts: dict | None = None,  # {page_num: text} from screen.py (optional)
    dpi: int = EXTRACT_DPI,
    verbose: bool = True,
) -> list[dict]:
    """
    For each confirmed image page (amenity, exterior, interior, location, lifestyle, spec):
    1. Render at high DPI
    2. Send image + page text to OpenRouter for labeling
    3. Get back structured metadata (label, description, amenities_shown, confidence)
    4. Save the image with a descriptive filename in the appropriate subdirectory

    Args:
        pdf_path: Path to PDF
        confirmed_pages: Pages classified as image/data types (from vision_screen)
        output_dir: Base output directory (will create subfolders per image type)
        page_texts: Pre-extracted text per page (from screen.py)
        dpi: Render DPI (default 150)
        verbose: Print progress

    Returns:
        List of extraction results with file paths added.
    """
    if not confirmed_pages:
        return []

    output_dir = Path(output_dir)
    api_key = os.getenv("OPENROUTER_API_KEY")
    doc = fitz.open(pdf_path)

    if verbose:
        print(f"  Extracting labels from {len(confirmed_pages)} image pages at {dpi} DPI...")

    results = []
    used_labels = set()

    for i, page_info in enumerate(confirmed_pages):
        page_num = page_info.get("page")
        image_type = page_info.get("type", "other")

        if verbose:
            print(f"  [{i+1}/{len(confirmed_pages)}] Page {page_num} ({image_type})...", end=" ", flush=True)

        t0 = time.time()

        # Render at high res
        try:
            img_bytes = render_page_hires(doc, page_num, dpi=dpi)
        except Exception as e:
            print(f"RENDER ERROR: {e}")
            results.append({
                "page_num": page_num,
                "image_type": image_type,
                "error": str(e),
                "label": f"page-{page_num}-error",
                "file_path": None,
            })
            continue

        # Extract labels from OpenRouter
        try:
            extracted = _call_extract_image(
                api_key, img_bytes,
                (page_texts or {}).get(page_num, ""),
                image_type, page_num
            )
        except Exception as e:
            print(f"API ERROR: {e}")
            extracted = {
                "page_num": page_num,
                "image_type": image_type,
                "label": f"page-{page_num}-{image_type.replace('_', '-')}",
                "description": None,
                "amenities_shown": None,
                "confidence": "low",
                "_api_error": str(e)[:100],
            }

        # Save the image — graceful degradation if save fails
        try:
            file_path = _save_image(img_bytes, extracted, output_dir, used_labels)
            extracted["file_path"] = file_path
        except Exception as e:
            print(f"SAVE ERROR: {e}")
            extracted["file_path"] = None

        elapsed = time.time() - t0

        if verbose:
            label = extracted.get("label", "?")
            conf = extracted.get("confidence", "?")
            print(f"-> {label} [{conf}] ({elapsed:.1f}s)")

        # Merge vision screen context into result
        extracted["vision_confidence"] = page_info.get("confidence")
        extracted["vision_note"] = page_info.get("note")

        results.append(extracted)

    doc.close()

    if verbose:
        saved = [r for r in results if r.get("file_path")]
        print(f"  Saved {len(saved)}/{len(confirmed_pages)} image files")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python extract_images.py <pdf_path> <output_dir> [page_nums...]")
        print("  e.g.: python extract_images.py input/listing.pdf output/listing 10 16 22")
        sys.exit(1)

    pdf = sys.argv[1]
    out = sys.argv[2]
    pages = [int(x) for x in sys.argv[3:]] if len(sys.argv) > 3 else [10, 16, 22]

    confirmed = [{"page": p, "type": "amenity_image", "confidence": "high", "note": "cli test"} for p in pages]
    results = extract_image_labels(pdf, confirmed, out, verbose=True)

    print("\n--- Extraction Results ---")
    for r in results:
        print(f"  Page {r.get('page_num')}: {r.get('label')} -> {r.get('file_path')}")
