# prop-capture

Extracts unit plans, master plans, and floor plans from real estate PDF brochures.
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

## Architecture (3 steps)

```
PDF
 |
 v
Step 1: Text screening (screen.py)
  - PyMuPDF extracts text from every page
  - Keyword scoring: BHK configs, carpet area, floor plan, master plan, etc.
  - Vector drawing count (vector plans score high)
  - Large embedded image detection
  - Zero API calls, ~0.1 seconds
  - Output: candidate page list (typically 15-25 pages out of 50)
 |
 v
Step 2: Vision screening (vision_screen.py)
  - Renders candidates as 100 DPI thumbnails
  - Sends in batches of 4 to Groq for classification
  - Classifies: unit_plan / master_plan / floor_plan / area_table / other
  - Filters out area tables and marketing pages
  - Output: confirmed plan pages with type labels
 |
 v
Step 3: Plan extraction (extract_plans.py)
  - Renders confirmed pages at 150 DPI (high quality)
  - Sends each image + page text to Groq
  - Extracts: plan type, BHK config, carpet area, tower, series, variant
  - Saves labeled images to output subdirectories
  - Output: plan_data.json + named image files
```

## Models

All vision tasks use **Groq llama-4-scout** (`meta-llama/llama-4-scout-17b-16e-instruct`).
- Step 2 (vision screen): batch classification, max_tokens=500
- Step 3 (extraction): detailed labeling per page, max_tokens=600

## Auth

Groq API key loaded from `/root/.secrets.env` as `GROQ_API_KEY`.

```bash
# Verify key is present
grep GROQ_API_KEY /root/.secrets.env
```

## Output Structure

```
output/<pdf_name>/
├── unit-plan/          ← individual apartment floor plans, labeled JPEGs
│   ├── tower1-3bhk-luxury-x01-125sqm.jpg
│   └── tower2-4bhk-windsor-x01-153sqm.jpg
├── master-plan/        ← site layout maps
│   ├── master-plan-ground-level.jpg
│   └── master-plan-sky-level.jpg
├── floor-plan/         ← whole building floors (multiple units)
│   └── tower1-3bhk-luxury.jpg
├── pages/              ← reference page renders (if pre-rendered)
└── plan_data.json      ← structured metadata for all plans found
```

### plan_data.json format

```json
{
  "pdf_source": "Godrej-Skyline-Brochure",
  "total_pages": 50,
  "candidate_pages": [20, 21, 23, 25, 26, ...],
  "confirmed_plan_pages": [20, 21, 23, 26, 27, ...],
  "unit_plans": [
    {
      "page_num": 26,
      "plan_type": "unit_plan",
      "unit_type": "3BHK",
      "variant": "Luxury",
      "carpet_area": "125.64 sq m",
      "saleable_area": null,
      "tower": "Tower 1",
      "series": "X01",
      "floor_range": null,
      "label": "tower1-3bhk-luxury-x01-125sqm",
      "confidence": "high",
      "file_path": "output/Godrej-Skyline-Brochure/unit-plan/tower1-3bhk-luxury-x01-125sqm.jpg"
    }
  ],
  "master_plans": [...],
  "floor_plans": [...],
  "total_plans_found": 18,
  "pipeline_log": { "total_time_s": 47.3, "steps": {...} }
}
```

## Performance

Benchmarked on Godrej Skyline Brochure (50 pages, 31MB PDF):

| Step | Time | API Calls |
|------|------|-----------|
| Text screen | ~0.1s | 0 |
| Vision screen (21 candidates) | ~8s | 6 Groq calls |
| Plan extraction (15 pages) | ~40s | 15 Groq calls |
| **Total** | **~50s** | **21 calls** |

Estimated cost: ~$0.01 per run (Groq pricing).

## Files

| File | Purpose |
|------|---------|
| `pipeline.py` | Main entry point — orchestrates all 3 steps |
| `screen.py` | Text + heuristic page screener (0 API cost) |
| `vision_screen.py` | Groq vision classifier for candidate pages |
| `extract_plans.py` | Groq extractor — labels and saves plan images |
| `requirements.txt` | Python dependencies |
| `ARCHITECTURE.md` | Design rationale and architecture detail |
| `input/` | Drop PDFs here |
| `output/` | Results written here |

## Setup

```bash
pip install -r requirements.txt
# Ensure GROQ_API_KEY is in /root/.secrets.env
python pipeline.py input/your-brochure.pdf
```
