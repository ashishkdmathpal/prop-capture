"""
vision_screen.py — Classify candidate pages using Groq vision.

Renders candidate pages as thumbnails (100 DPI) and sends them in batches
to llama-4-scout for fast, cheap classification.

Classifies each page as:
  unit_plan / master_plan / floor_plan / area_table /
  amenity_image / exterior_image / interior_image /
  location_map / specification_table / lifestyle_image / other
"""

import os
import re
import base64
import json
import time
import fitz  # PyMuPDF
from pathlib import Path
from dotenv import load_dotenv

load_dotenv("/root/.secrets.env")

from groq import Groq

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
BATCH_SIZE = 4   # images per API call (keep small for reliability)
SCREEN_DPI = 100  # thumbnail DPI for fast classification

CLASSIFY_PROMPT = """You are analyzing pages from an Indian real estate property brochure PDF.

I will show you {n} page images, labeled Page {page_list}.

For EACH page, classify it as exactly ONE of:
- "unit_plan": Architectural floor plan drawing of a single apartment/unit -- shows individual rooms with dimensions
- "floor_plan": Architectural drawing showing an entire building floor with MULTIPLE units laid out
- "master_plan": Site layout map showing the overall project -- bird's-eye view with building placements, amenities, towers
- "amenity_image": Photo/render of a project amenity -- swimming pool, gym, clubhouse, banquet hall, garden, play area, sky deck, terrace, games room
- "exterior_image": Photo/render of building exterior, entrance lobby exterior, tower facade, aerial view of buildings, retail podium
- "interior_image": Photo/render of apartment interior -- bedroom, living room, kitchen, dining area, bathroom
- "location_map": Map showing distances/travel times to nearby locations (schools, hospitals, malls, IT parks)
- "specification_table": Table listing specifications (flooring, bathrooms, kitchen finishes, doors, windows) or payment milestones
- "lifestyle_image": Photo showing people enjoying amenities or balcony views -- lifestyle/aspirational marketing photo with people as focus
- "area_table": A table of ONLY apartment areas/prices with NO visual architectural drawing or photo
- "other": Cover page, section divider, logos, certificates, marketing text without significant images, back page, blank pages

Return ONLY a valid JSON array (no markdown, no explanation) with one entry per page:
[{{"page": 26, "type": "unit_plan", "confidence": "high", "note": "3BHK floor plan with room labels"}}, ...]

Rules:
- If a page has BOTH a photo and some text headline, classify by the dominant visual content (the photo), not the text
- A page with a render of a swimming pool = "amenity_image", not "lifestyle_image"
- A page with a person on a balcony = "lifestyle_image"
- A page showing the full building from outside = "exterior_image"
- A page with just text (no large photo or diagram) = "other"
- When in doubt between amenity_image and exterior_image, if you can see multiple building towers, it's exterior_image
- When in doubt between unit_plan and floor_plan, prefer unit_plan if you see individual room labels
"""

VALID_TYPES = {
    "unit_plan", "master_plan", "floor_plan",
    "amenity_image", "exterior_image", "interior_image",
    "location_map", "specification_table", "lifestyle_image",
    "area_table", "other",
}
PLAN_TYPES = {"unit_plan", "master_plan", "floor_plan"}
IMAGE_TYPES = {"amenity_image", "exterior_image", "interior_image", "location_map", "lifestyle_image"}
DATA_TYPES = {"specification_table", "area_table"}
EXTRACTABLE_TYPES = PLAN_TYPES | IMAGE_TYPES | DATA_TYPES


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_page_thumbnail(
    doc: fitz.Document,
    page_num: int,  # 1-indexed
    dpi: int = SCREEN_DPI,
    pages_dir: Path | None = None,
) -> bytes:
    """
    Render a page at low DPI and return JPEG bytes.
    If pages_dir is given and a pre-rendered file exists, uses that instead.
    """
    # Try pre-rendered page first (faster)
    if pages_dir is not None:
        cached = pages_dir / f"page_{page_num}.jpg"
        if cached.exists():
            with open(cached, "rb") as f:
                return f.read()

    page = doc[page_num - 1]  # fitz is 0-indexed
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("jpeg")


# ---------------------------------------------------------------------------
# Groq API
# ---------------------------------------------------------------------------

def _call_groq_classify(client: Groq, page_nums: list[int], image_bytes_list: list[bytes]) -> list[dict]:
    """
    Send a batch of page images to Groq for classification.
    Returns list of {page, type, confidence, note}.
    """
    page_list_str = ", ".join(f"Page {p}" for p in page_nums)

    content = []
    for i, (page_num, img_bytes) in enumerate(zip(page_nums, image_bytes_list)):
        img_b64 = base64.b64encode(img_bytes).decode()
        content.append({
            "type": "text",
            "text": f"Page {page_num}:"
        })
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}
        })

    prompt = CLASSIFY_PROMPT.format(
        n=len(page_nums),
        page_list=page_list_str
    )
    content.append({"type": "text", "text": prompt})

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[{"role": "user", "content": content}],
        max_tokens=800,
        temperature=0.1,
    )

    raw = response.choices[0].message.content.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(l for l in lines if not l.startswith("```")).strip()

    try:
        results = json.loads(raw)
        if not isinstance(results, list):
            raise ValueError("Expected JSON array")
        return results
    except (json.JSONDecodeError, ValueError) as e:
        print(f"    Warning: Could not parse Groq response: {e}")
        print(f"    Raw: {raw[:300]}")
        # Fallback: mark all as unclassified
        return [{"page": p, "type": "other", "confidence": "low", "note": "parse_error"} for p in page_nums]


# ---------------------------------------------------------------------------
# Main classification function
# ---------------------------------------------------------------------------

def classify_pages(
    pdf_path: str,
    candidate_pages: list[int],  # 1-indexed page numbers
    dpi: int = SCREEN_DPI,
    pages_dir: Path | None = None,
    verbose: bool = True,
) -> list[dict]:
    """
    Render and classify candidate pages using Groq vision.

    Args:
        pdf_path: Path to the PDF file
        candidate_pages: List of 1-indexed page numbers to classify
        dpi: DPI for rendering (default 100 for thumbnails)
        pages_dir: Directory with pre-rendered page images (optional speedup)
        verbose: Print progress messages

    Returns:
        List of classification dicts:
        [{page: 26, type: "unit_plan", confidence: "high", note: "..."}, ...]
        All input pages are returned (unclassified ones get type="other").
    """
    if not candidate_pages:
        return []

    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    doc = fitz.open(pdf_path)

    if verbose:
        print(f"  Vision screening {len(candidate_pages)} candidate pages in batches of {BATCH_SIZE}...")

    # Render all thumbnails up front
    t0 = time.time()
    rendered = {}
    for pn in candidate_pages:
        try:
            rendered[pn] = render_page_thumbnail(doc, pn, dpi=dpi, pages_dir=pages_dir)
        except Exception as e:
            print(f"    Warning: Could not render page {pn}: {e}")
            rendered[pn] = None

    render_time = time.time() - t0
    if verbose:
        print(f"  Rendered {len(rendered)} thumbnails in {render_time:.1f}s")

    doc.close()

    # Process in batches
    all_results = []
    batch_num = 0

    for i in range(0, len(candidate_pages), BATCH_SIZE):
        batch_pages = candidate_pages[i:i + BATCH_SIZE]
        batch_images = []
        valid_pages = []

        for pn in batch_pages:
            if rendered.get(pn) is not None:
                batch_images.append(rendered[pn])
                valid_pages.append(pn)
            else:
                # Page failed to render — mark as other
                all_results.append({
                    "page": pn,
                    "type": "other",
                    "confidence": "low",
                    "note": "render_failed"
                })

        if not valid_pages:
            continue

        batch_num += 1
        t_batch = time.time()

        try:
            batch_results = _call_groq_classify(client, valid_pages, batch_images)

            # Normalize and validate results
            # Handle both int keys (28) and string keys ("Page 28" or "28")
            result_map = {}
            for r in batch_results:
                if not isinstance(r, dict):
                    continue
                raw_page = r.get("page")
                # Normalize to int: "Page 28" -> 28, "28" -> 28, 28 -> 28
                if isinstance(raw_page, str):
                    raw_page = re.sub(r"[^0-9]", "", raw_page)
                    try:
                        raw_page = int(raw_page)
                    except (ValueError, TypeError):
                        continue
                if isinstance(raw_page, (int, float)):
                    result_map[int(raw_page)] = r

            for pn in valid_pages:
                if pn in result_map:
                    r = result_map[pn]
                    # Normalize type
                    if r.get("type") not in VALID_TYPES:
                        r["type"] = "other"
                    r["page"] = pn  # ensure int
                    all_results.append(r)
                else:
                    all_results.append({
                        "page": pn,
                        "type": "other",
                        "confidence": "low",
                        "note": "missing_from_response"
                    })

            batch_time = time.time() - t_batch
            if verbose:
                classified = {r["page"]: r["type"] for r in batch_results if isinstance(r, dict)}
                print(f"  Batch {batch_num}: pages {valid_pages} -> {classified} ({batch_time:.1f}s)")

        except Exception as e:
            print(f"    Error in batch {batch_num}: {e}")
            for pn in valid_pages:
                all_results.append({
                    "page": pn,
                    "type": "other",
                    "confidence": "low",
                    "note": f"api_error: {str(e)[:100]}"
                })

    # Sort by page number
    all_results.sort(key=lambda x: x.get("page", 0))

    if verbose:
        plan_pages = [r for r in all_results if r["type"] in PLAN_TYPES]
        image_pages = [r for r in all_results if r["type"] in IMAGE_TYPES]
        data_pages = [r for r in all_results if r["type"] in DATA_TYPES]
        print(f"  Vision screen complete: {len(plan_pages)} plan, {len(image_pages)} image, {len(data_pages)} data pages / {len(candidate_pages)} candidates")

    return all_results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python vision_screen.py <pdf_path> [page_nums...]")
        print("  e.g.: python vision_screen.py input/listing.pdf 20 21 26 27 28")
        sys.exit(1)

    pdf = sys.argv[1]
    pages = [int(x) for x in sys.argv[2:]] if len(sys.argv) > 2 else list(range(1, 51))

    results = classify_pages(pdf, pages, verbose=True)
    print("\n--- Classification Results ---")
    for r in results:
        print(f"  Page {r['page']:>2}: {r['type']:<12} ({r.get('confidence','?')}) — {r.get('note','')}")

    plans = [r for r in results if r["type"] in PLAN_TYPES]
    images = [r for r in results if r["type"] in IMAGE_TYPES]
    data = [r for r in results if r["type"] in DATA_TYPES]
    print(f"\nPlan pages: {[r['page'] for r in plans]}")
    print(f"Image pages: {[r['page'] for r in images]}")
    print(f"Data pages: {[r['page'] for r in data]}")
