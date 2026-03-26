"""
screen.py — Fast text-based page screening to find floor plan candidates.

Returns list of {page_num, page_text, flags, score} for pages that are
likely to contain unit plans, master plans, or floor plans.
Also detects image-dominant pages (renders, photos, amenity shots).
Zero API calls — pure PyMuPDF text extraction + heuristics.
"""

import re
import time
import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# Keyword sets for scoring
# ---------------------------------------------------------------------------

UNIT_PLAN_KEYWORDS = [
    r"\bunit plan\b", r"\bfloor plan\b", r"\btypical floor\b", r"\bunit layout\b",
    r"\bunit type\b",
    r"\b1\s*bhk\b", r"\b2\s*bhk\b", r"\b3\s*bhk\b", r"\b4\s*bhk\b",
    r"\bcarpet area\b", r"\bsaleable area\b", r"\bbuilt.?up area\b",
    r"\barea as per rera\b",
    r"\bconfiguration\b",
    r"\bmaster bedroom\b", r"\bliving room\b", r"\bdining\b",
    r"\bsq\.?\s*ft\b", r"\bsq\.?\s*mt\b", r"\bsqft\b", r"\bsq\.?\s*m\b",
]

MASTER_PLAN_KEYWORDS = [
    r"\bmaster plan\b", r"\bsite plan\b", r"\bsite layout\b",
    r"\blocation plan\b", r"\bground level\b", r"\bpodium level\b",
    r"\bsky level\b", r"\bamenity\b", r"\bamenities\b",
    r"\bclubhouse\b", r"\btower [a-z0-9]\b", r"\bblock [a-z0-9]\b",
    r"\bwing [a-z0-9]\b", r"\bparking\b",
]

GENERAL_PLAN_KEYWORDS = [
    r"\blayout\b", r"\bplan\b", r"\bseries\b", r"\btower\b", r"\brera\b",
]

# Keywords for specification and location pages that the main screener misses
SPEC_KEYWORDS = [
    r"\bspecification\b", r"\bflooring\b", r"\bbathroom\b", r"\bkitchen\b",
    r"\bpayment plan\b", r"\bmilestone\b", r"\bconstruction linked\b",
]

LOCATION_KEYWORDS = [
    r"\b\d+\s*kms?\b", r"\b\d+\s*minutes?\b", r"\bit parks?\b",
    r"\bhospital\b", r"\bschool\b", r"\bmall\b", r"\bclub\b",
]

# Thresholds
CANDIDATE_SCORE_THRESHOLD = 3
MASTER_PLAN_SCORE_THRESHOLD = 3
DRAWING_COUNT_THRESHOLD = 500   # pages with >500 drawing ops are likely vector plans
IMAGE_SIZE_THRESHOLD_KB = 200   # pages with >200KB rendered image but <50 chars text
IMAGE_PAGE_TEXT_THRESHOLD = 900  # image-dominant pages have less than this many chars
IMAGE_LARGE_DIM_THRESHOLD = 700   # images larger than this (px) count as "large"
IMAGE_LARGE_SIZE_THRESHOLD = 70_000  # images totaling more than this (bytes) count as image-heavy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compile_patterns(keyword_list: list[str]) -> list[re.Pattern]:
    return [re.compile(kw, re.IGNORECASE) for kw in keyword_list]


_UNIT_PATTERNS = _compile_patterns(UNIT_PLAN_KEYWORDS)
_MASTER_PATTERNS = _compile_patterns(MASTER_PLAN_KEYWORDS)
_GENERAL_PATTERNS = _compile_patterns(GENERAL_PLAN_KEYWORDS)
_SPEC_PATTERNS = _compile_patterns(SPEC_KEYWORDS)
_LOCATION_PATTERNS = _compile_patterns(LOCATION_KEYWORDS)


def _get_drawing_count(page) -> int:
    """Count the number of drawing operations on a page (proxy for vector content)."""
    try:
        paths = page.get_drawings()
        return len(paths)
    except Exception:
        return 0


def _get_embedded_images(page) -> list[dict]:
    """Return embedded image list with dimensions where possible."""
    try:
        return page.get_images(full=True)
    except Exception:
        return []


def _has_large_embedded_image(page, doc) -> bool:
    """Check if page has at least one large embedded image (likely a plan scan)."""
    images = _get_embedded_images(page)
    for img in images:
        xref = img[0]
        width = img[2]
        height = img[3]
        if width > 700 or height > 700:
            return True
        # Also check raw size
        try:
            raw = doc.extract_image(xref)
            if raw and len(raw.get("image", b"")) > 50_000:  # >50KB raw
                return True
        except Exception:
            pass
    return False


def _is_image_page(page, page_num: int, text: str, doc) -> dict | None:
    """
    Detect pages dominated by images (renders, photos, amenity shots, interiors).

    These pages are missed by the keyword screener because they have minimal text.
    Two detection patterns:
      1. Single large embedded image (>1000px or >100KB) + minimal text
      2. Many small images totaling >100KB raw (tiled/sliced photo pages) + minimal text

    Real estate brochure PDFs commonly embed photos as many small tiles (30-50 images per page)
    rather than one large image — this handles both patterns.

    Returns a candidate dict with likely_type='property_image', or None if not an image page.
    """
    text_len = len(text.strip())

    # Only catch pages with limited text — spec tables have text AND images
    # but they're already caught by keyword scoring with SPEC_KEYWORDS
    if text_len > IMAGE_PAGE_TEXT_THRESHOLD:
        return None

    images = page.get_images(full=True)
    if not images:
        return None

    signals = []

    # Pattern 1: Single large embedded image (>1000px in either dim)
    has_single_large = False
    for img in images:
        width = img[2]
        height = img[3]
        if width > IMAGE_LARGE_DIM_THRESHOLD or height > IMAGE_LARGE_DIM_THRESHOLD:
            has_single_large = True
            signals.append(f"large_single_image:{width}x{height}")
            break

    if has_single_large:
        return {
            "page_num": page_num,
            "score": 0,
            "master_score": 0,
            "signals": signals + ["image_dominant_page"],
            "text_snippet": text[:300].strip(),
            "likely_type": "property_image",
            "drawing_count": 0,
            "text_length": text_len,
        }

    # Pattern 2: Many small images with large total raw size (tiled/sliced photos)
    # These PDFs tile a single photo into 30-50 small pieces
    total_raw_size = 0
    for img in images:
        try:
            xref = img[0]
            raw = doc.extract_image(xref)
            if raw:
                total_raw_size += len(raw.get("image", b""))
        except Exception:
            pass

    # Threshold: >100KB total raw AND at least 5 images (rules out single-icon pages)
    if total_raw_size > IMAGE_LARGE_SIZE_THRESHOLD and len(images) >= 5:
        signals.append(f"tiled_image_page:{len(images)}_imgs:{total_raw_size // 1024}KB")
        return {
            "page_num": page_num,
            "score": 0,
            "master_score": 0,
            "signals": signals + ["image_dominant_page"],
            "text_snippet": text[:300].strip(),
            "likely_type": "property_image",
            "drawing_count": 0,
            "text_length": text_len,
        }

    # Pattern 3: Near-zero text + any image above 400px — clearly a visual content page
    # Catches pages where images are moderately sized but text is absent
    if text_len < 50 and total_raw_size > 20_000:
        max_dim = max((max(img[2], img[3]) for img in images), default=0)
        if max_dim > 400:
            signals.append(f"no_text_with_image:{max_dim}px:{total_raw_size // 1024}KB")
            return {
                "page_num": page_num,
                "score": 0,
                "master_score": 0,
                "signals": signals + ["image_dominant_page"],
                "text_snippet": text[:300].strip(),
                "likely_type": "property_image",
                "drawing_count": 0,
                "text_length": text_len,
            }

    return None


def _score_page(page, page_num: int, text: str, doc) -> dict | None:
    """
    Score a single page. Returns a candidate dict or None if score < threshold.

    Scoring rules:
    +3: text contains explicit floor/unit/master/site plan keywords
    +2: text contains BHK config (1BHK, 2BHK, etc.)
    +2: text contains area keywords (carpet area, sq ft, RERA area)
    +1: text contains "tower" or "series"
    +2: drawing_count > DRAWING_COUNT_THRESHOLD (vector plan)
    +2: has a single large embedded image (>1500px in either dim)

    Master plan sub-score:
    +3: "ground level" / "site plan" / "master plan" / "amenities"
    +2: numbered amenity list pattern (e.g. "01. Entry Gate")
    +2: single large embedded image
    """
    score = 0
    master_score = 0
    signals = []
    text_lower = text.lower()

    # --- Unit plan keywords ---
    for pattern in _UNIT_PATTERNS:
        if pattern.search(text):
            keyword = pattern.pattern.strip(r"\b").replace(r"\b", "")
            signals.append(f"keyword:{keyword.strip()}")
            score += 1

    # Boost for explicit plan type words
    explicit_plan = re.search(
        r"\b(unit plan|floor plan|unit layout|master plan|site plan|typical floor)\b",
        text, re.IGNORECASE
    )
    if explicit_plan:
        score += 2
        signals.append("explicit_plan_word")

    # BHK config
    bhk_match = re.search(r"\b([1-4])\s*bhk\b", text, re.IGNORECASE)
    if bhk_match:
        score += 2
        signals.append(f"has_config:{bhk_match.group(0).upper().replace(' ', '')}")

    # Area keywords
    area_match = re.search(
        r"\b(carpet area|saleable area|built.?up area|area as per rera)\b",
        text, re.IGNORECASE
    )
    if area_match:
        score += 2
        signals.append("has_area_table")

    # Sq ft / numbers
    if re.search(r"\d+(\.\d+)?\s*sq\.?\s*(ft|m|mt)\b", text, re.IGNORECASE):
        score += 1
        signals.append("has_area_numbers")

    # --- Drawing count (vector content) ---
    drawing_count = _get_drawing_count(page)
    if drawing_count > DRAWING_COUNT_THRESHOLD:
        score += 2
        signals.append(f"has_vector_drawings:{drawing_count}")

    # --- Large embedded image ---
    if _has_large_embedded_image(page, doc):
        score += 2
        signals.append("has_large_image")

    # --- Master plan signals ---
    for pattern in _MASTER_PATTERNS:
        if pattern.search(text):
            master_score += 1
            signals.append(f"master:{pattern.pattern.strip(r'b').replace(r'b', '').strip()}")

    # Numbered amenity list (strong master plan signal)
    if re.search(r"\b0?[1-9]\.\s+[A-Z][a-z]", text):
        master_score += 2
        signals.append("numbered_amenity_list")

    if _has_large_embedded_image(page, doc):
        master_score += 2

    # --- Spec / location page signals (boost score so they pass threshold) ---
    spec_hits = sum(1 for p in _SPEC_PATTERNS if p.search(text))
    if spec_hits >= 2:
        score += 2
        signals.append(f"spec_keywords:{spec_hits}")

    location_hits = sum(1 for p in _LOCATION_PATTERNS if p.search(text))
    if location_hits >= 2:
        score += 2
        signals.append(f"location_keywords:{location_hits}")

    # --- Preliminary type guess ---
    likely_type = "other"
    if master_score >= MASTER_PLAN_SCORE_THRESHOLD:
        likely_type = "master_plan"
    elif score >= CANDIDATE_SCORE_THRESHOLD:
        if bhk_match or area_match:
            likely_type = "unit_plan"
        elif drawing_count > DRAWING_COUNT_THRESHOLD:
            likely_type = "floor_plan"
        else:
            likely_type = "floor_plan"

    # Filter: must meet at least one threshold
    if score < CANDIDATE_SCORE_THRESHOLD and master_score < MASTER_PLAN_SCORE_THRESHOLD:
        # Special case: image-heavy pages with little text (full-bleed plan images)
        if len(text.strip()) < 50 and _has_large_embedded_image(page, doc):
            score = CANDIDATE_SCORE_THRESHOLD
            likely_type = "master_plan"
            signals.append("image_heavy_no_text")
        else:
            return None

    return {
        "page_num": page_num,
        "score": score,
        "master_score": master_score,
        "signals": list(set(signals)),
        "text_snippet": text[:300].strip(),
        "likely_type": likely_type,
        "drawing_count": drawing_count,
        "text_length": len(text),
    }


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------

def screen_pages(pdf_path: str) -> dict:
    """
    Analyze every page via text extraction + structural heuristics.

    Two-track screening:
      Track A (keyword): scores pages by plan/spec/location keywords and vector content
      Track B (image):   detects pages dominated by large embedded images (renders, photos)

    Returns:
    {
        "candidates": [...],              # keyword-scored plan pages
        "master_plan_candidates": [...],  # pages likely to be master/site plans
        "image_candidates": [...],        # image-dominant pages (renders, photos)
        "all_candidates": [...],          # union of all three lists (deduplicated)
        "total_pages": 50,
        "screening_time_ms": 150,
        "page_texts": {1: "...", 2: "...", ...}  # all page texts for reuse
    }
    """
    t0 = time.time()

    doc = fitz.open(pdf_path)
    total_pages = len(doc)

    candidates = []
    master_plan_candidates = []
    image_candidates = []
    page_texts = {}

    # Track which page_nums are already in keyword candidates (to avoid double-adding)
    keyword_page_nums = set()

    for i in range(total_pages):
        page = doc[i]
        page_num = i + 1
        text = page.get_text("text").strip()
        page_texts[page_num] = text

        # Track A: keyword/heuristic scoring
        result = _score_page(page, page_num, text, doc)
        if result is not None:
            keyword_page_nums.add(page_num)
            if result["master_score"] >= MASTER_PLAN_SCORE_THRESHOLD:
                master_plan_candidates.append(result)
            if result["score"] >= CANDIDATE_SCORE_THRESHOLD:
                candidates.append(result)

        # Track B: image-dominant page detection (only for pages NOT already caught by keyword track)
        if page_num not in keyword_page_nums:
            img_result = _is_image_page(page, page_num, text, doc)
            if img_result is not None:
                image_candidates.append(img_result)

    doc.close()

    # Sort by score descending
    candidates.sort(key=lambda x: x["score"], reverse=True)
    master_plan_candidates.sort(key=lambda x: x["master_score"], reverse=True)
    image_candidates.sort(key=lambda x: x["page_num"])

    # Union of all three candidate lists (deduplicated, preserving best score info)
    page_map = {}
    for c in candidates + master_plan_candidates + image_candidates:
        pn = c["page_num"]
        if pn not in page_map or c["score"] > page_map[pn]["score"]:
            page_map[pn] = c
    all_candidates = sorted(page_map.values(), key=lambda x: x["page_num"])

    elapsed_ms = int((time.time() - t0) * 1000)

    return {
        "candidates": candidates,
        "master_plan_candidates": master_plan_candidates,
        "image_candidates": image_candidates,
        "all_candidates": all_candidates,
        "total_pages": total_pages,
        "screening_time_ms": elapsed_ms,
        "page_texts": page_texts,
    }


# ---------------------------------------------------------------------------
# CLI for quick testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python screen.py <pdf_path>")
        sys.exit(1)

    result = screen_pages(sys.argv[1])
    print(f"\nText screening complete in {result['screening_time_ms']}ms")
    print(f"Total pages: {result['total_pages']}")
    print(f"Candidates (unit/floor plans): {len(result['candidates'])}")
    print(f"Master plan candidates: {len(result['master_plan_candidates'])}")
    print(f"Image candidates (renders/photos): {len(result['image_candidates'])}")
    print(f"All candidates: {len(result['all_candidates'])}")

    print("\n--- Candidate pages ---")
    for c in result["all_candidates"]:
        print(
            f"  Page {c['page_num']:>2}: score={c['score']}, master_score={c['master_score']}, "
            f"type={c['likely_type']}, drawings={c['drawing_count']}"
        )
        print(f"    signals: {c['signals']}")
        if c["text_snippet"]:
            print(f"    text: {c['text_snippet'][:100]!r}")
