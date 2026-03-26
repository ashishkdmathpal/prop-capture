# prop-capture

Extracts all property images from real estate PDF brochures: unit plans, master plans, floor plans, amenity renders, exteriors, interiors, location maps, spec tables, and lifestyle photos.
Saves labeled images + structured JSON ready for a property dealer to use.

## How to Run

```bash
# Standard run
python pipeline.py input/brochure.pdf

# Higher quality renders (slower, larger files)
python pipeline.py input/brochure.pdf --dpi 200

# Custom output directory
python pipeline.py input/brochure.pdf --output-dir /tmp/results
```

Drop the PDF into `input/` first. Output goes to `output/<pdf_name>/`.

## Architecture (4 steps)

```
PDF
 |
 v
Step 1: Text screening (screen.py)
  Two-track screening:
  Track A (keyword): PyMuPDF text extraction + keyword scoring
    - BHK configs, carpet area, floor plan, master plan keywords
    - Spec/location keywords (new): flooring, payment plan, kms/minutes
    - Vector drawing count (vector plans score high)
  Track B (image): Detects image-dominant pages the keyword track misses
    - Single large embedded image (>1000px) + minimal text
    - OR many small tiled images (>5 images, >70KB total) + minimal text
    - These photo pages have near-zero text (amenity renders, exteriors, etc.)
  Zero API calls, ~10 seconds
  Output: candidate page list (typically 40-48 pages out of 50)
 |
 v
Step 2: Vision screening (vision_screen.py)
  - Renders candidates as 100 DPI thumbnails
  - Sends in batches of 4 to OpenRouter for classification
  - Classifies 11 types:
      Plans:  unit_plan / master_plan / floor_plan
      Images: amenity_image / exterior_image / interior_image /
              location_map / lifestyle_image
      Data:   specification_table / area_table
      Other:  other
  - Output: confirmed pages with type labels, split into plan/image/data tracks
 |
 v
Step 3a: Plan extraction (extract_plans.py)
  - Renders confirmed plan pages at 150 DPI (high quality)
  - Sends each image + page text to OpenRouter
  - Extracts: plan type, BHK config, carpet area, tower, series, variant
  - Saves labeled images to output/unit-plan/, master-plan/, floor-plan/
  - Output: structured metadata per plan
 |
 v
Step 3b: Image extraction (extract_images.py)
  - Renders confirmed image+data pages at 150 DPI
  - Sends each image + page text to OpenRouter
  - Extracts: label, description, amenities_shown (for amenity), confidence
  - Saves labeled images to output/amenity/, exterior/, interior/, location/, lifestyle/, specification/
  - Output: structured metadata per image
 |
 v
plan_data.json + all labeled images
```

## Models

All vision tasks use **llama-4-scout via OpenRouter** (`meta-llama/llama-4-scout-17b-16e-instruct`).
- Step 2 (vision screen): batch classification, max_tokens=800
- Step 3a (plan extraction): detailed labeling per page, max_tokens=600
- Step 3b (image extraction): label + description per page, max_tokens=400

## Auth

OpenRouter API key loaded from `/root/.secrets.env` as `OPENROUTER_API_KEY`.

```bash
# Verify key is present
grep OPENROUTER_API_KEY /root/.secrets.env
```

## Output Structure

```
output/<pdf_name>/
├── unit-plan/          ← individual apartment floor plans, labeled JPEGs
├── master-plan/        ← site layout maps
├── floor-plan/         ← whole building floors (multiple units)
├── amenity/            ← pool, gym, clubhouse, garden, play area renders
├── exterior/           ← building facade, entrance lobby, aerial renders
├── interior/           ← bedroom, living room, kitchen renders
├── location/           ← distance/connectivity maps
├── lifestyle/          ← lifestyle photos with people
├── specification/      ← spec tables, payment plans
├── pages/              ← reference page renders (if pre-rendered)
└── plan_data.json      ← structured metadata for all images found
```

### plan_data.json format

```json
{
  "pdf_source": "Godrej-Skyline-Brochure",
  "total_pages": 50,
  "candidate_pages": [1, 2, 3, ...],
  "confirmed_plan_pages": [20, 21, 24, ...],
  "confirmed_image_pages": [2, 7, 8, ...],
  "unit_plans": [{ "page_num": 26, "plan_type": "unit_plan", "unit_type": "3BHK", ... }],
  "master_plans": [...],
  "floor_plans": [...],
  "amenity_images": [{ "page_num": 16, "image_type": "amenity_image", "label": "infinity-edge-swimming-pool", "description": "...", "amenities_shown": ["pool"], "confidence": "high", "file_path": "..." }],
  "exterior_images": [...],
  "interior_images": [...],
  "location_images": [...],
  "lifestyle_images": [...],
  "specification_tables": [...],
  "total_plans_found": 16,
  "total_images_found": 20,
  "pipeline_log": { "total_time_s": 79.0, "steps": {...} }
}
```

## Performance

Benchmarked on Godrej Skyline Brochure (50 pages, 31MB PDF):

| Step | Time | API Calls |
|------|------|-----------|
| Text screen | ~10s | 0 |
| Vision screen (48 candidates) | ~23s | 12 OpenRouter calls |
| Plan extraction (16 pages) | ~22s | 16 OpenRouter calls |
| Image extraction (20 pages) | ~22s | 20 OpenRouter calls |
| **Total** | **~79s** | **48 OpenRouter calls** |

Extracted: 16 plans + 20 property images = 36 pages total (was 16 with plans-only).
Estimated cost: ~$0.02 per run (OpenRouter pricing).

## Files

| File | Purpose |
|------|---------|
| `pipeline.py` | Main entry point — orchestrates all steps |
| `screen.py` | Two-track page screener: keyword + image-dominant detection (0 API cost) |
| `vision_screen.py` | OpenRouter vision classifier — 11 types including amenity/exterior/interior |
| `extract_plans.py` | OpenRouter extractor — labels and saves floor plan images |
| `extract_images.py` | OpenRouter extractor — labels and saves property photo images |
| `requirements.txt` | Python dependencies |
| `ARCHITECTURE.md` | Design rationale and architecture detail |
| `EXPANSION-PLAN.md` | Plan for the scope expansion (2026-03) |
| `input/` | Drop PDFs here |
| `output/` | Results written here |

## Setup

```bash
pip install -r requirements.txt
# Ensure OPENROUTER_API_KEY is in /root/.secrets.env
python pipeline.py input/your-brochure.pdf
```
