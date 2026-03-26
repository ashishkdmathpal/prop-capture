# Plan: prop-capture v2 Pipeline — Fast Plan Extraction from Real Estate PDFs

## Context

### What exists today
The current pipeline (`pipeline.py`) has a 3-step flow:
1. **Ingest** (`ingest.py`) — extracts ALL embedded images from every page + renders all 50 pages at 150 DPI. For the Godrej Skyline brochure: **606 embedded images + 50 page renders = 656 images**.
2. **Classify** (`classify.py`) — sends each of the 606 embedded images to Claude Haiku one-by-one. Sequential. No batching.
3. **Extract Data** (`extract_data.py`) — sends text + first page image to Sonnet for structured JSON.

### Why it's broken
- 606 images at ~15-20s per API call = **2.5+ hours** for one PDF
- 35/35 classified so far were "other" — the actual floor plans are **vector drawings** (PDF path objects), not embedded images. They have 0 embedded images but 4,000-138,000 drawing paths.
- Pages 20-21 (master plans) have large embedded images but the pipeline never reaches them
- The `listing_data.json` extraction also failed (returned `[object Object]` error)
- No concurrency, no batching, no pre-screening

### Critical discovery from analysis
The Godrej Skyline PDF has this structure:
- **Pages 1-19**: Marketing/amenity pages (photos, renders, text) — NOT plans
- **Page 20**: Ground level master plan (1 large embedded image, 4577x2616)
- **Page 21**: Sky level amenities plan (7 images including large overlay)
- **Page 23**: Full-page embedded master plan image (9922x7016, no text)
- **Pages 24-44**: Unit plans and floor plans — **ALL are vector drawings**, zero or near-zero embedded images
  - Pages 25, 29, 31, 34, 36, 39, 41: "overview" pages with title text like "Unit Layout Plans TOWER 1 (Luxury) 3BHK"
  - Pages 26-28, 30, 32-33, 35, 37-38, 40, 42-44: **Actual unit plan drawings** with RERA area tables + vector floor plans (4,000-138,000 drawing paths each)
- **Pages 45-50**: Specs, payment plan, RERA info

**Key insight**: Embedded image extraction is useless for the actual unit plans — they are vector drawings rendered as PDF paths. The ONLY way to capture them is **rendering the page as an image** (page render at 150-200 DPI).

## Approach

### Strategy: Text-First Funnel with Page Renders

Instead of extracting and classifying 606 embedded images, we:
1. Use **free** text extraction to narrow 50 pages down to ~21 candidates
2. Use **cheap** Haiku batch vision to confirm which ~15 pages are actual plans vs tables
3. Render only confirmed plan pages at **high DPI (200)** and extract with **Sonnet** for labeling
4. Run everything with **async concurrency**

This reduces 606 sequential API calls to ~8-12 concurrent calls total.

### Why this approach over alternatives

| Alternative | Problem |
|---|---|
| Classify all 606 embedded images | 2.5 hours, misses vector plans entirely |
| Send all 50 pages to Vision | Expensive (~$0.50 at Sonnet), slow |
| Text-only (no Vision) | Can't distinguish plan pages from area tables |
| OCR-based | PDF text extraction is already perfect via PyMuPDF |

The text-first funnel is **free for step 1**, **$0.07 for step 2**, and **$0.22 for step 3** = ~$0.30 total per PDF in under 45 seconds.

## Pipeline Architecture

```
INPUT: PDF brochure (20-100 pages)
          |
          v
    +--------------+
    | STEP 0: LOAD |  PyMuPDF open, extract all page text
    | (0 API cost) |  ~1 second
    +--------------+
          |
          v
    +--------------------+
    | STEP 1: TEXT SCREEN |  Keyword scoring per page
    | (0 API cost)       |  Keywords: floor plan, unit plan, master plan, site plan,
    | ~0.1 seconds       |  layout, 1BHK-4BHK, carpet area, RERA, tower
    +--------------------+  Also: check drawing count (>1000 = likely vector plan)
          |                 Also: check for large embedded images (>500KB = likely plan)
          |
          v
    Candidate pages (typically 15-25 of 50)
    + Master plan candidates (pages with numbered amenity lists,
      "ground level", "site plan", or single large image)
          |
          v
    +-------------------------+
    | STEP 2: VISION SCREEN   |  Render candidates at 72 DPI (small thumbnails)
    | Haiku batched (10/call)  |  Send 10 thumbnails per API call in parallel
    | ~$0.07, ~8 seconds      |  Ask: "Which of these pages contain floor plans,
    +-------------------------+  unit plans, or master plans? Return page numbers
          |                      and type for each."
          v
    Confirmed plan pages (typically 10-20)
    Tagged: {page_num, type: unit_plan|floor_plan|master_plan}
          |
          v
    +----------------------------+
    | STEP 3: HIGH-RES EXTRACT   |  Render confirmed pages at 200 DPI
    | Sonnet, 1 call per page    |  Send page image + page text to Sonnet
    | Async concurrent (5 at a   |  Ask: extract plan type, config, tower, area,
    | time)                      |  series, and generate a filename
    | ~$0.22, ~15 seconds        |
    +----------------------------+
          |
          v
    +----------------------------+
    | STEP 4: SAVE & ORGANIZE    |  Save high-res page renders as named files:
    | (0 API cost)               |  master-plan/ground-level-amenities.jpg
    | ~1 second                  |  unit-plan/tower1-3bhk-x01-carpet-125sqm.jpg
    +----------------------------+  unit-plan/tower2-4bhk-x02-carpet-175sqm.jpg
          |                        floor-plan/tower3-4bhk-typical-floor.jpg
          v
    +----------------------------+
    | STEP 5: STRUCTURED DATA    |  Same as current extract_data.py
    | Sonnet, 1 call             |  Send full text + first page image
    | ~$0.05, ~5 seconds         |  Extract: project name, developer, configs,
    +----------------------------+  pricing, RERA, amenities, etc.
          |
          v
    OUTPUT: output/<name>/
      ├── master-plan/   (named images)
      ├── unit-plan/     (named images)
      ├── floor-plan/    (named images)
      ├── pages/         (all 50 page renders at 72 DPI for reference)
      ├── listing_data.json
      └── pipeline_log.json (timing, costs, decisions)
```

## Detailed Steps

### Step 1: Create `screen.py` — Text + Heuristic Pre-Screening

**New file: `/root/projects/prop-capture/screen.py`**

This module takes a PDF path and returns scored candidate pages with zero API calls.

```python
# Function signatures:

def screen_pages(pdf_path: str) -> dict:
    """
    Analyze every page via text extraction + structural heuristics.
    Returns:
    {
        "candidates": [
            {
                "page_num": 26,
                "score": 7,
                "signals": ["has_config:3bhk", "has_area_table", "has_vector_drawings:4521", "has_rera"],
                "text_snippet": "UNIT TYPE - 3 BHK Luxury | SERIES - X01...",
                "likely_type": "unit_plan",  # preliminary guess
                "embedded_images": 0,
                "drawing_count": 4521,
            },
            ...
        ],
        "master_plan_candidates": [...],  # separate track
        "total_pages": 50,
        "screening_time_ms": 150,
    }
    """

def _score_page(page, page_num: int, text: str) -> dict | None:
    """Score a single page. Returns candidate dict or None if score < threshold."""
    # Scoring rules:
    # +3: text contains "floor plan", "unit plan", "master plan", "site plan", "unit layout"
    # +2: text contains BHK config (1bhk, 2bhk, 3bhk, 4bhk)
    # +2: text contains area keywords (carpet area, area as per rera, sq.ft, sq.mt)
    # +1: text contains "tower"
    # +2: drawing_count > 1000 (vector plan)
    # +2: has single large embedded image (>500KB or >2000x2000)
    # +1: text contains numbered list of amenities (master plan signal)
    #
    # MASTER PLAN signals (separate track):
    # +3: text has "ground level" or "site plan" or "master plan"
    # +2: text has numbered amenity list (01. Entry, 02. Guard House...)
    # +2: single large embedded image on page
    #
    # Threshold for candidate: score >= 3
    # Threshold for master plan: master_score >= 3
```

**Scoring rationale based on actual Godrej Skyline analysis:**
- Pages 26-28, 32-33, 37-38, 42-44 (actual unit plans): score 4-5 (config + area + tower + drawings)
- Pages 25, 29, 31, 34, 36, 39, 41 (plan overview/title pages): score 6+ (plan keyword + config + tower)
- Pages 20-21 (master plans): caught by master plan track (numbered amenities + large image)
- Page 23 (master plan image, no text): caught by large embedded image heuristic
- Marketing pages 1-19: score 0-1, filtered out

### Step 2: Create `vision_screen.py` — Batch Vision Confirmation

**New file: `/root/projects/prop-capture/vision_screen.py`**

Takes candidate pages from Step 1, renders at 72 DPI, sends to Haiku in batches for confirmation.

```python
# Function signatures:

async def vision_screen_pages(
    pdf_path: str,
    candidates: list[dict],
    batch_size: int = 10,
    dpi: int = 72,
) -> list[dict]:
    """
    Send candidate page thumbnails to Claude Haiku in batches.
    Returns confirmed plan pages with vision-assigned types.

    Each batch call sends N page images + prompt asking:
    "For each page, classify as: unit_plan, floor_plan, master_plan, area_table, or not_a_plan.
     Return JSON array with page_num, type, and brief description."

    Uses AsyncAnthropic for concurrent batch calls.
    """

def _render_page_thumbnail(doc, page_num: int, dpi: int = 72) -> bytes:
    """Render a page at low DPI and return JPEG bytes."""

def _build_batch_prompt(page_nums: list[int]) -> str:
    """Build the classification prompt for a batch of page thumbnails."""
    # Key prompt content:
    # - Distinguish between: actual floor plan drawing vs area/pricing table
    # - Distinguish between: unit plan (single unit) vs floor plan (whole floor with multiple units)
    # - Identify master plans (site layout, amenity maps)
    # - Pages with ONLY tables of numbers + no visual plan = "area_table" (skip)
    # - Pages with architectural drawings = plan (keep)
```

**Batch strategy:**
- 21 candidates / 10 per batch = 3 API calls (run in parallel)
- Each call: 10 images at 72 DPI (~200KB each) + prompt
- Expected latency: ~3-5 seconds (all parallel)
- Expected result: ~15 confirmed plans, ~6 area-only tables filtered out

### Step 3: Create `extract_plans.py` — High-Res Extraction + Labeling

**New file: `/root/projects/prop-capture/extract_plans.py`**

Takes confirmed pages, renders at 200 DPI, sends to Sonnet for detailed labeling.

```python
# Function signatures:

async def extract_and_label_plans(
    pdf_path: str,
    confirmed_pages: list[dict],  # from vision_screen
    output_dir: str,
    dpi: int = 200,
    max_concurrent: int = 5,
) -> list[dict]:
    """
    For each confirmed plan page:
    1. Render at 200 DPI
    2. Send image + page text to Sonnet
    3. Get back: type, config, tower, series, carpet_area, description, suggested_filename
    4. Save the high-res render with the suggested filename

    Returns list of extracted plan metadata.

    Uses asyncio.Semaphore(max_concurrent) to limit parallel Sonnet calls.
    """

async def _extract_single_plan(
    client: AsyncAnthropic,
    page_image_bytes: bytes,
    page_text: str,
    page_num: int,
    preliminary_type: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """
    Send one page to Sonnet for detailed extraction.

    Prompt asks for JSON:
    {
        "type": "unit_plan" | "floor_plan" | "master_plan",
        "config": "3BHK" | "4BHK" | null,
        "tower": "Tower 1" | "Tower 3 (#Windsor)" | null,
        "series": "X01" | "X02" | null,
        "carpet_area_sqm": 125.64,
        "carpet_area_sqft": 1352,
        "unit_facing": "East" | null,
        "description": "3BHK Luxury unit plan, Tower 1, Series X01, East facing",
        "suggested_filename": "tower1-3bhk-luxury-x01-east-125sqm"
    }
    """

def _save_plan_image(
    image_bytes: bytes,
    plan_info: dict,
    output_dir: str,
) -> str:
    """Save the high-res page render with a descriptive filename."""
    # Filename pattern:
    # unit-plan/tower1-3bhk-luxury-x01-east-125sqm.jpg
    # floor-plan/tower2-4bhk-luxury-typical-floor.jpg
    # master-plan/ground-level-amenities.jpg
    # master-plan/sky-level-amenities.jpg
```

**Concurrency approach:**
- asyncio.Semaphore(5) limits to 5 parallel Sonnet calls
- 15 pages / 5 concurrent = 3 waves of ~3-5 seconds each
- Total: ~10-15 seconds for all plan extraction

### Step 4: Rewrite `pipeline.py` — New Orchestrator

**Modified file: `/root/projects/prop-capture/pipeline.py`**

Complete rewrite of the main pipeline to use the new 5-step flow.

```python
# New pipeline flow:

async def run_pipeline(pdf_path: str, output_dir: str = "output") -> dict:
    """
    Full pipeline: PDF -> screened pages -> confirmed plans -> labeled images + data JSON

    Steps:
    0. Open PDF, extract text, render reference thumbnails
    1. Text-based screening (screen.py) — 0 API calls
    2. Vision screening (vision_screen.py) — 2-3 Haiku calls
    3. Plan extraction + labeling (extract_plans.py) — 10-15 Sonnet calls
    4. Save organized images
    5. Structured data extraction (extract_data.py) — 1 Sonnet call

    Returns pipeline result dict with timing, costs, and file paths.
    """

# CLI entry point wraps async:
if __name__ == "__main__":
    import asyncio
    asyncio.run(run_pipeline(args.input, args.output_dir))
```

**Key changes from current pipeline.py:**
- Remove dependency on `ingest.py` for PDF processing (inline the minimal parts needed)
- Remove dependency on `classify.py` (replaced by vision_screen.py)
- Keep `extract_data.py` for Step 5 (structured data), but fix the bug where content was sent as `[object Object]`
- Add `--fast` flag that skips Vision screening (uses text-only for speed, less accurate)
- Add `--verbose` flag for detailed logging
- Add pipeline_log.json output with per-step timing and cost tracking

### Step 5: Fix `extract_data.py` Bug

**Modified file: `/root/projects/prop-capture/extract_data.py`**

The current `listing_data.json` shows:
```json
{"_raw_response": "It looks like your message didn't come through correctly — I received `[object Object]`..."}
```

This suggests the content list was malformed. The fix:
- Ensure content blocks are properly structured `[{"type": "image", ...}, {"type": "text", ...}]`
- The bug is likely in `extract_listing_data_from_ingest()` where it builds the content list — the `ingested` dict may be passing incorrect image paths or the base64 encoding is failing silently
- Add explicit error handling and validation before sending to Claude

### Step 6: Update `requirements.txt`

No new dependencies needed. Current `anthropic>=0.25.0` already includes `AsyncAnthropic`. `asyncio` is stdlib.

### Step 7: Update `CLAUDE.md`

Update the project's CLAUDE.md to reflect the new pipeline architecture, commands, and expected performance.

## File Changes Summary

| File | Action | Description |
|---|---|---|
| `screen.py` | **NEW** | Text + heuristic pre-screening (0 API cost) |
| `vision_screen.py` | **NEW** | Batch Haiku vision confirmation (async) |
| `extract_plans.py` | **NEW** | High-res Sonnet extraction + labeling (async) |
| `pipeline.py` | **REWRITE** | New async orchestrator with 5-step flow |
| `extract_data.py` | **FIX** | Fix [object Object] bug in content construction |
| `requirements.txt` | **NO CHANGE** | All deps already present |
| `CLAUDE.md` | **UPDATE** | Document new pipeline |
| `ingest.py` | **KEEP** | Still used for non-PDF inputs (images, text) |
| `classify.py` | **KEEP** | Still used for non-PDF inputs, deprecated for PDF |
| `extract.py` | **DEPRECATE** | No longer needed (was standalone image extractor) |
| `run_via_cli.py` | **DEPRECATE** | Replaced by new pipeline |
| `run_with_proxy.py` | **DEPRECATE** | Replaced by new pipeline |

## Dependencies & Order

```
Step 1: screen.py         (no dependencies, implement first)
    |
Step 2: vision_screen.py  (depends on screen.py output format)
    |
Step 3: extract_plans.py  (depends on vision_screen.py output format)
    |
Step 4: pipeline.py       (depends on all 3 new modules)
    |
Step 5: extract_data.py   (bug fix, independent of above)
    |
Step 6: CLAUDE.md         (after everything works)
```

Steps 1-3 can be tested independently with hardcoded inputs before wiring into pipeline.py.

Step 5 (extract_data.py fix) is independent and can happen in parallel with Steps 1-3.

## Expected Performance

### Godrej Skyline Brochure (50 pages, 31MB)

| Step | Time | API Calls | Cost |
|---|---|---|---|
| 0. Load + text extract | ~1s | 0 | $0.00 |
| 1. Text screening | ~0.1s | 0 | $0.00 |
| 2. Vision screening (21 candidates) | ~5s | 3 Haiku calls (parallel) | ~$0.07 |
| 3. Plan extraction (15 pages) | ~15s | 15 Sonnet calls (5 concurrent) | ~$0.22 |
| 4. Save images | ~2s | 0 | $0.00 |
| 5. Structured data | ~5s | 1 Sonnet call | ~$0.05 |
| **TOTAL** | **~28 seconds** | **19 calls** | **~$0.34** |

### Comparison with current pipeline

| Metric | Current | New |
|---|---|---|
| Time | 2.5+ hours (estimated) | ~28 seconds |
| API calls | 606 | 19 |
| Cost | ~$0.50 (606 Haiku calls) | ~$0.34 |
| Accuracy | 0% (all classified as "other") | Expected 95%+ |
| Finds vector plans | NO | YES (page renders) |

## Risks & Edge Cases

### Risk 1: PDFs where plans are NOT vector drawings
Some brochures embed floor plans as raster images instead of vector paths. The drawing_count heuristic won't catch these.
**Mitigation:** The text keyword scoring still works. And the Vision screening (Step 2) uses page renders which show everything regardless of whether it's vector or raster.

### Risk 2: Pages with plan images but no text
Page 23 in Godrej Skyline is a full-bleed image with zero text.
**Mitigation:** Add heuristic for pages with a single large embedded image (>1MB or >3000px in either dimension) and minimal/no text. Flag these as master plan candidates.

### Risk 3: Text keywords matching non-plan pages
"layout" appears on amenity description pages (pages 10, 12, 13, 14).
**Mitigation:** The scoring threshold (>= 3) already filters these out because they don't have BHK configs or area tables. "layout" alone scores only +3 if it matches "layout plan" or "unit layout", but generic "layout" in body text shouldn't match the specific keyword patterns.

### Risk 4: Haiku misclassifying in batch mode
Batch classification with 10 images might confuse Haiku.
**Mitigation:** Each image is labeled with its page number in the prompt. Use structured JSON output. If accuracy is poor, reduce batch size to 5.

### Risk 5: Sonnet labeling extraction failures
Sonnet might return malformed JSON.
**Mitigation:** Wrap in try/catch, fall back to generic filename based on page text. Use `max_tokens=500` to keep responses focused.

### Risk 6: Very large PDFs (100+ pages)
100 pages at 72 DPI = ~20MB of thumbnails for screening.
**Mitigation:** Text screening eliminates most pages before Vision. Even with 40 candidates, that's only 4 Haiku batch calls. Still under 60 seconds.

### Risk 7: API key expired or rate limited
**Mitigation:** Pipeline should handle API errors gracefully, save partial results, and allow resuming from the last successful step.

### Edge Case: Duplex/simplex unit variations
Some brochures show the same plan in different orientations or mirror variants.
**Handling:** Extract all variants. The Sonnet labeling prompt should note "mirrored" or "alternate" in the description.

### Edge Case: Combined floor plan + area table on same page
Some pages have both the visual plan AND the area table.
**Handling:** Still classify as plan (it IS a plan). The area table is bonus metadata.

## Verification

1. **Run on Godrej Skyline PDF** — should complete in under 45 seconds
2. **Check output/Godrej-Skyline-Brochure/unit-plan/** — should contain ~12 labeled unit plan images
3. **Check output/Godrej-Skyline-Brochure/master-plan/** — should contain 2-3 master plan images
4. **Check output/Godrej-Skyline-Brochure/floor-plan/** — should contain ~4 floor plan images
5. **Check listing_data.json** — should have valid structured JSON (not [object Object] error)
6. **Check pipeline_log.json** — should show per-step timing and costs
7. **Visual inspection** — open the saved images, verify they are actual plans (not tables or marketing pages)
8. **Filename quality** — filenames should be descriptive and human-readable

## Prompt Templates

### Vision Screening Prompt (Haiku, Step 2)
```
I'm showing you {N} pages from a real estate brochure PDF, numbered as labeled.

For each page, classify it as ONE of:
- "unit_plan": Architectural floor plan drawing of a single apartment/unit with rooms, dimensions
- "floor_plan": Architectural drawing showing an entire building floor with multiple units
- "master_plan": Site layout map showing building placement, amenities, landscaping across the project
- "area_table": Table of areas/prices/specifications with numbers but NO architectural drawing
- "other": Marketing page, photo, decorative content, or anything else

Return ONLY a JSON array:
[{"page": 26, "type": "unit_plan", "has_drawing": true}, ...]

Important: A page with ONLY numbers/tables and NO visual floor plan drawing = "area_table", not a plan.
```

### Plan Extraction Prompt (Sonnet, Step 3)
```
This is page {page_num} from a real estate brochure PDF. It contains a {preliminary_type}.

Page text: {page_text}

Extract the following as JSON:
{
  "type": "unit_plan" | "floor_plan" | "master_plan",
  "config": "3BHK" | "4BHK" | null,
  "variant": "Luxury" | "Windsor" | null,
  "tower": "Tower 1" | "Tower 2" | null,
  "series": "X01" | "X02" | null,
  "carpet_area_sqm": number | null,
  "carpet_area_sqft": number | null,
  "total_area_sqft": number | null,
  "unit_facing": "East" | "West" | "North" | "South" | null,
  "description": "Brief human-readable description",
  "suggested_filename": "lowercase-hyphenated-descriptive-name"
}

For master plans, set config/tower/series to null and describe the plan level (ground/sky/podium).
The suggested_filename should be unique and descriptive, max 50 chars, no extension.
```
