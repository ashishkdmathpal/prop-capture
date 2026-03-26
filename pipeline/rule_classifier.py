"""
rule_classifier.py — Rule-based page classification (zero AI, zero API calls).

Classifies PDF pages using structural analysis:
  - Text keywords (BHK, floor plan, master plan, spec, etc.)
  - Vector drawing count (architectural plans have high drawing ops)
  - Embedded image analysis (size, count, dimensions)
  - Page text length and layout heuristics

Produces the same output format as vision_screen.classify_pages() so it's
a drop-in replacement in the pipeline.
"""

import re
import time
import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# Keyword patterns (case-insensitive)
# ---------------------------------------------------------------------------

_UNIT_PLAN_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bunit plan\b", r"\bunit type\b", r"\bunit.?\d+\b", r"\bunit no\b",
        r"\b[1-5]\s*bhk\b", r"\b[1-5]\s*bed\b", r"\b[1-5]\s*bedroom\b",
        r"\bstudio\b", r"\bpenthouse\b", r"\bduplex\b", r"\bsimplex\b", r"\btriplex\b",
        r"\bcarpet area\b", r"\bsaleable area\b", r"\bbuilt.?up area\b", r"\bsuper area\b",
        r"\barea as per rera\b", r"\brera area\b", r"\barea:\s*\d+",
        r"\bsq\.?\s*ft\b", r"\bsqft\b", r"\bsq\.?\s*m\b", r"\bsq\.?\s*mt\b",
        r"\bconfiguration\b", r"\bapartment\b", r"\bflat\b", r"\bresidence\b",
    ]
]

_FLOOR_PLAN_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bfloor plan\b", r"\btypical floor\b", r"\btypical plan\b", r"\btypical layout\b",
        r"\bunit layout\b", r"\bwing layout\b", r"\btower layout\b",
        r"\bunit\s+\d+\b.*\bunit\s+\d+\b",  # multiple "UNIT N" on same page
        r"\bfloor plate\b", r"\btypical plate\b",
    ]
]

_MASTER_PLAN_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bmaster plan\b", r"\bmaster layout\b", r"\bsite plan\b", r"\bsite layout\b",
        r"\bsite map\b", r"\bground level\b", r"\bpodium level\b", r"\bsky level\b",
        r"\bterrace level\b", r"\bbasement level\b", r"\bstilt level\b",
        r"\blegend\b", r"\bentry.*exit\b", r"\blayout plan\b",
        r"\bbird.?s?\s*eye\b", r"\baerial view\b", r"\boverall layout\b",
    ]
]

_SPEC_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bspecification\b", r"\bflooring\b.*\b(vitrified|marble|ceramic|wooden|laminate)\b",
        r"\bpayment plan\b", r"\bpayment schedule\b", r"\bmilestone\b",
        r"\bconstruction linked\b", r"\bpossession linked\b",
        r"\bfittings\b", r"\bfixtures\b", r"\bfinishes\b",
        r"\bdoor\b.*\bwindow\b|\bwindow\b.*\bdoor\b",
        r"\belectric\b.*\bpoint\b", r"\bac provision\b",
        r"\bcost sheet\b", r"\bprice list\b", r"\brate card\b",
    ]
]

_LOCATION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\b\d+\s*kms?\b", r"\b\d+\s*minutes?\b", r"\b\d+\s*mins?\b",
        r"\bconnectivity\b", r"\blocation advantage\b", r"\blocation map\b",
        r"\bhospital\b", r"\bschool\b", r"\bcollege\b", r"\buniversity\b",
        r"\bit park\b", r"\btech park\b", r"\bsoftware park\b",
        r"\bairport\b", r"\brailway\b", r"\bmetro\b", r"\bhighway\b",
        r"\bmall\b", r"\bmarket\b", r"\bshopping\b",
        r"\bneighbourhood\b", r"\bneighborhood\b", r"\bsurrounding\b",
        r"\bdistance\b.*\bfrom\b",
    ]
]

_AMENITY_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bswimming pool\b", r"\bpool\b", r"\bclubhouse\b", r"\bclub house\b",
        r"\bgym\b", r"\bgymnasium\b", r"\bfitness\b",
        r"\bplay area\b", r"\bplayground\b", r"\bkids\s*play\b", r"\bchildren\b.*\barea\b",
        r"\bgarden\b", r"\blandscap\b", r"\bpark\b", r"\bopen space\b", r"\bgreen\b",
        r"\bjogging\b", r"\bwalking track\b", r"\bcycling\b",
        r"\byoga\b", r"\bmeditation\b", r"\bspa\b", r"\bsauna\b", r"\bjacuzzi\b",
        r"\bbanquet\b", r"\bmulti.?purpose\b", r"\bcommunity\b.*\bhall\b",
        r"\btennis\b", r"\bbadminton\b", r"\bbasketball\b", r"\bcricket\b",
        r"\bsky\s*deck\b", r"\brooftop\b", r"\bterrace\b.*\bgarden\b",
        r"\bsports\b", r"\barena\b", r"\bcourt\b",
    ]
]

_ROOM_LABELS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\bmaster bedroom\b", r"\bbedroom\s*\d?\b", r"\bbed\s*room\b",
        r"\bliving\b.*\bdining\b", r"\bliving room\b", r"\bdining room\b", r"\bdrawing room\b",
        r"\bkitchen\b", r"\bmodular kitchen\b", r"\bpantry\b",
        r"\bbathroom\b", r"\bwashroom\b", r"\btoilet\b", r"\bwc\b", r"\bpowder room\b",
        r"\butility\b", r"\bservice\b.*\barea\b", r"\bdomestic help\b", r"\bservant\b",
        r"\bfoyer\b", r"\bentrance\b.*\blobby\b",
        r"\bdeck\b", r"\bbalcony\b", r"\bterrace\b", r"\bverandah\b", r"\bsit.?out\b",
        r"\blobby\b", r"\blift\b", r"\bstaircase\b", r"\bpassage\b", r"\bcorridor\b",
        r"\bstudy\b", r"\bpooja\b", r"\bstore\b", r"\bwic\b", r"\bwardrobe\b",
    ]
]

# Plan types (must match vision_screen.py constants)
PLAN_TYPES = {"unit_plan", "master_plan", "floor_plan"}
IMAGE_TYPES = {"amenity_image", "exterior_image", "interior_image", "location_map", "lifestyle_image"}
DATA_TYPES = {"specification_table", "area_table"}


# ---------------------------------------------------------------------------
# Structural analysis helpers
# ---------------------------------------------------------------------------

def _get_page_signals(doc, page, page_num, text):
    """Analyze a single page and return classification signals."""
    text_len = len(text.strip())
    drawings = len(page.get_drawings())
    images = page.get_images(full=True)

    # Image analysis
    max_img_dim = 0
    total_img_bytes = 0
    large_image_count = 0
    for img in images:
        w, h = img[2], img[3]
        max_img_dim = max(max_img_dim, w, h)
        if w > 400 or h > 400:
            large_image_count += 1
        try:
            raw = doc.extract_image(img[0])
            if raw:
                total_img_bytes += len(raw.get("image", b""))
        except Exception:
            pass

    # Keyword matching
    unit_hits = sum(1 for p in _UNIT_PLAN_PATTERNS if p.search(text))
    floor_hits = sum(1 for p in _FLOOR_PLAN_PATTERNS if p.search(text))
    master_hits = sum(1 for p in _MASTER_PLAN_PATTERNS if p.search(text))
    spec_hits = sum(1 for p in _SPEC_PATTERNS if p.search(text))
    location_hits = sum(1 for p in _LOCATION_PATTERNS if p.search(text))
    amenity_hits = sum(1 for p in _AMENITY_PATTERNS if p.search(text))
    room_hits = sum(1 for p in _ROOM_LABELS if p.search(text))

    return {
        "text_len": text_len,
        "drawings": drawings,
        "image_count": len(images),
        "max_img_dim": max_img_dim,
        "total_img_bytes": total_img_bytes,
        "large_image_count": large_image_count,
        "unit_hits": unit_hits,
        "floor_hits": floor_hits,
        "master_hits": master_hits,
        "spec_hits": spec_hits,
        "location_hits": location_hits,
        "amenity_hits": amenity_hits,
        "room_hits": room_hits,
    }


def _classify_one(signals):
    """Classify a page based on its signals. Returns (type, confidence, note)."""
    s = signals

    # --- Text-based classification (high confidence when text is present) ---
    if s["text_len"] > 10:
        # Master plan: explicit keywords
        if s["master_hits"] >= 2:
            return "master_plan", "high", f"master keywords: {s['master_hits']}"

        # Floor plan: shows multiple units on one page
        if s["floor_hits"] >= 1 and s["room_hits"] >= 3:
            return "floor_plan", "high", f"floor plan keywords + {s['room_hits']} room labels"

        if s["floor_hits"] >= 1:
            return "floor_plan", "medium", f"floor plan keywords: {s['floor_hits']}"

        # Unit plan: BHK/area keywords + room labels
        if s["unit_hits"] >= 2 and s["room_hits"] >= 2:
            return "unit_plan", "high", f"unit keywords: {s['unit_hits']}, rooms: {s['room_hits']}"

        if s["unit_hits"] >= 2:
            return "unit_plan", "medium", f"unit keywords: {s['unit_hits']}"

        # Specification table
        if s["spec_hits"] >= 2:
            return "specification_table", "high", f"spec keywords: {s['spec_hits']}"

        # Location map
        if s["location_hits"] >= 2:
            return "location_map", "high", f"location keywords: {s['location_hits']}"

        # Amenity-related text with image
        if s["amenity_hits"] >= 1 and s["large_image_count"] >= 1:
            return "amenity_image", "medium", f"amenity keywords + large image"

        # Room labels suggest a plan even without explicit "floor plan" text
        if s["room_hits"] >= 4:
            return "unit_plan", "medium", f"room labels: {s['room_hits']}"

        # Has plan-like text but not enough to classify specifically
        if s["unit_hits"] >= 1 or s["room_hits"] >= 2:
            return "unit_plan", "low", f"weak plan signals"

    # --- Structural classification (for pages with little/no text) ---

    # High vector drawing count = architectural plan
    if s["drawings"] > 200 and s["large_image_count"] >= 1:
        return "floor_plan", "medium", f"vector-heavy ({s['drawings']} drawings) + image"

    if s["drawings"] > 100 and s["large_image_count"] >= 1:
        return "unit_plan", "medium", f"moderate vectors ({s['drawings']}) + image"

    # Large colorful image with few drawings = photo/render or master plan
    if s["large_image_count"] >= 1 and s["max_img_dim"] > 700:
        if s["drawings"] > 15:
            # Has both image and drawings — likely a plan rendered as image
            return "floor_plan", "low", f"large image ({s['max_img_dim']}px) + drawings"
        else:
            # Pure image page — likely a property photo or master plan render
            return "exterior_image", "low", f"large image ({s['max_img_dim']}px), minimal vectors"

    # Multiple images = tiled photo page
    if s["image_count"] >= 5 and s["total_img_bytes"] > 50_000:
        return "exterior_image", "low", f"tiled images ({s['image_count']} imgs, {s['total_img_bytes']//1024}KB)"

    # Single moderate image with no text = likely a visual page
    if s["large_image_count"] >= 1 and s["text_len"] < 50 and s["total_img_bytes"] > 20_000:
        return "exterior_image", "low", f"image page ({s['max_img_dim']}px)"

    return "other", "high", "no relevant content detected"


# ---------------------------------------------------------------------------
# Main classification function (drop-in replacement for vision_screen.classify_pages)
# ---------------------------------------------------------------------------

def classify_pages_rules(
    pdf_path: str,
    candidate_pages: list[int],
    dpi: int = 100,
    pages_dir=None,
    verbose: bool = True,
) -> list[dict]:
    """
    Classify pages using rule-based heuristics (zero API calls).

    Returns same format as vision_screen.classify_pages():
    [{page: 26, type: "unit_plan", confidence: "high", note: "..."}, ...]
    """
    if not candidate_pages:
        return []

    doc = fitz.open(pdf_path)
    t0 = time.time()

    if verbose:
        print(f"  Rule-based classification of {len(candidate_pages)} pages...")

    results = []
    for pn in candidate_pages:
        page = doc[pn - 1]
        text = page.get_text().strip()
        signals = _get_page_signals(doc, page, pn, text)
        page_type, confidence, note = _classify_one(signals)

        results.append({
            "page": pn,
            "type": page_type,
            "confidence": confidence,
            "note": note,
        })

    doc.close()

    elapsed = time.time() - t0

    if verbose:
        from vision_screen import PLAN_TYPES, IMAGE_TYPES, DATA_TYPES
        plan_pages = [r for r in results if r["type"] in PLAN_TYPES]
        image_pages = [r for r in results if r["type"] in IMAGE_TYPES]
        data_pages = [r for r in results if r["type"] in DATA_TYPES]
        other_pages = [r for r in results if r["type"] not in PLAN_TYPES | IMAGE_TYPES | DATA_TYPES]
        print(f"  Rule-based classification done in {elapsed:.1f}s (0 API calls)")
        print(f"  Plans: {len(plan_pages)}, Images: {len(image_pages)}, Data: {len(data_pages)}, Other: {len(other_pages)}")

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python rule_classifier.py <pdf_path> [page_nums...]")
        sys.exit(1)

    pdf = sys.argv[1]
    doc = fitz.open(pdf)
    pages = [int(x) for x in sys.argv[2:]] if len(sys.argv) > 2 else list(range(1, doc.page_count + 1))
    doc.close()

    results = classify_pages_rules(pdf, pages, verbose=True)
    print("\n--- Rule-Based Classification ---")
    for r in results:
        print(f"  Page {r['page']:>2}: {r['type']:<20} ({r['confidence']}) — {r.get('note','')}")
