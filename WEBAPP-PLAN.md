# Plan: PropCapture Web Portal

## Context

### What exists today
- **prop-capture pipeline** (`pipeline.py`) extracts unit/master/floor plans from real estate PDF brochures in ~49 seconds using a 3-step process: text screening -> Groq vision screening -> Groq vision extraction
- Output: labeled JPEG images in `unit-plan/`, `master-plan/`, `floor-plan/` directories + `plan_data.json` with structured metadata
- Tested on Godrej Skyline (50 pages, 31MB) -> found 10 unit plans, 2 master plans, 5 floor plans
- Uses Groq llama-4-scout for all vision tasks (~$0.01/run)
- All Python, dependencies: PyMuPDF, groq SDK, python-dotenv

### What the user wants
A property dealer uploads a PDF brochure -> system auto-extracts everything -> displays it as a clean, searchable property listing page (like Housing.com/99acres but auto-generated from PDFs). Reference design: breakandi.com (minimal, modern).

### What's missing from pipeline for a full listing
The current pipeline extracts ONLY plan images + plan metadata (BHK type, area, tower). A real property listing needs:
1. **Project-level metadata**: project name, developer, location, property type, RERA number, possession date, total towers/units/floors
2. **Configurations summary**: aggregated list of all configs with price ranges
3. **Amenities list**: extracted from brochure text/images
4. **Property photos**: lifestyle/exterior images from the brochure (currently ignored)
5. **Price information**: if mentioned in the brochure
6. **Contact info**: sales office, phone numbers

---

## Architecture

```
                    PROPERTY DEALER
                         |
                    [Uploads PDF]
                         |
                         v
    +------------------------------------------+
    |          FastAPI Web Server (:5050)       |
    |------------------------------------------|
    |  POST /api/upload     -> accept PDF      |
    |  GET  /api/status/:id -> job status      |
    |  GET  /api/listing/:id -> full JSON      |
    |  GET  /api/listings    -> all listings    |
    |  GET  /images/:id/:path -> serve images  |
    |  GET  /                -> SPA frontend   |
    +------------------------------------------+
           |                          |
           v                          v
    +---------------+      +------------------+
    | Pipeline      |      | SQLite Database  |
    | (background)  |      | listings.db      |
    |               |      |                  |
    | 1. Plans      |      | jobs table       |
    |    (existing)  |      | listings table   |
    | 2. Metadata   |      | configs table    |
    |    (NEW)       |      | amenities table  |
    | 3. Photos     |      +------------------+
    |    (NEW)       |
    +---------------+
           |
           v
    +---------------------+
    | output/<slug>/      |
    |   unit-plan/        |
    |   master-plan/      |
    |   floor-plan/       |
    |   photos/     (NEW) |
    |   plan_data.json    |
    |   listing.json (NEW)|
    +---------------------+
           |
           v
    +---------------------+
    | Frontend (Jinja2 +  |
    | Alpine.js + CSS)    |
    |                     |
    | / -> upload + list  |
    | /listing/:id -> page|
    +---------------------+
```

---

## Layer 1: Extraction Pipeline Enhancements

### 1A. New Module: `extract_metadata.py` — Project-Level Data Extraction

**Purpose**: Extract everything about the project that ISN'T a plan image.

**Approach**: Single Groq call using the FULL text of all pages (concatenated, truncated to fit context). This is a text-only call (no vision needed for metadata), making it cheap and fast.

**Input**: All page texts from `screen_pages()` (already extracted in Step 1).

**Extraction prompt asks for**:
```json
{
  "project_name": "Godrej Skyline",
  "developer_name": "Godrej Properties",
  "location": {
    "area": "Koregaon Park Annexe",
    "city": "Pune",
    "state": "Maharashtra",
    "pin_code": null
  },
  "property_type": "residential",
  "configurations": [
    {"type": "3BHK", "variant": "Luxury", "carpet_area_range": "125-127 sq m", "price_range": null},
    {"type": "3BHK", "variant": "Windsor", "carpet_area_range": "113-114 sq m", "price_range": null},
    {"type": "4BHK", "variant": "Luxury", "carpet_area_range": "194 sq m", "price_range": null},
    {"type": "4BHK", "variant": "Windsor", "carpet_area_range": "152-153 sq m", "price_range": null}
  ],
  "towers": ["Tower 1", "Tower 2", "Tower 3", "Tower 4"],
  "total_towers": 4,
  "total_floors": null,
  "total_units": null,
  "amenities": ["Swimming Pool", "Clubhouse", "Gym", "Jogging Track", "Kids Play Area", ...],
  "rera_number": "P52100034837",
  "rera_authority": "MahaRERA",
  "possession_date": "December 2028",
  "price_range": {"min": null, "max": null, "unit": "INR"},
  "contact": {
    "sales_office": null,
    "phone": null,
    "email": null,
    "website": null
  },
  "tagline": "Where the skyline meets your lifestyle",
  "highlights": ["Koregaon Park Annexe location", "4 towers", "3BHK & 4BHK configurations"]
}
```

**Implementation details**:
- Concatenate all page_texts, truncate to 12,000 chars (Groq context limit for text)
- Single Groq call using the TEXT model (not vision) — `llama-3.3-70b-versatile` or the same scout model in text mode
- No images needed for this extraction
- Run this IN PARALLEL with the existing plan extraction (Step 3) since they're independent
- Estimated time: 2-3 seconds, 1 API call

**Indian real estate specifics to handle**:
- RERA numbers vary by state (MahaRERA: P52100..., HRERA: RC/REP/..., UP-RERA: UPRERAPRJ...)
- Areas in sq m AND sq ft (some brochures use one, some both)
- Hindi text mixed in (project names, area names)
- "Carpet Area as per RERA" vs "Saleable Area" vs "Super Built-up Area"
- Possession dates as "December 2028" or "Q4 2028" or "30 months from booking"

### 1B. New Module: `extract_photos.py` — Lifestyle/Property Photos

**Purpose**: Extract hero images, exterior renders, amenity photos from the brochure.

**Approach**:
- Pages NOT identified as plans (the "other" pages from vision_screen) often contain property photos
- Use page rendering at 150 DPI for pages that have large embedded images but are NOT plans
- Send a small batch to Groq to classify: "exterior render", "interior render", "amenity photo", "location map", "marketing graphic", "logo/decorative"
- Save the useful ones (exterior, interior, amenity, location) to `photos/` directory

**Which pages to target**:
- From vision_screen.py, pages classified as "other" that had `_has_large_embedded_image()` = True in screen.py
- Also: pages 1-3 of any brochure (usually hero images)
- Skip: pages with mostly text, tables, payment plans, legal disclaimers

**Output**:
```
photos/
  hero-exterior-1.jpg
  hero-exterior-2.jpg
  amenity-pool.jpg
  amenity-clubhouse.jpg
  location-map.jpg
```

**Implementation details**:
- Reuse the same Groq vision batch classification pattern from vision_screen.py
- Classify into: exterior_render, interior_render, amenity_photo, location_map, marketing_graphic, other
- Keep only the first 3 categories
- 2-3 Groq batch calls, ~5 seconds
- This runs AFTER the plan pipeline completes (uses the "other" page list)

### 1C. Enhanced Pipeline Integration

**Modified `pipeline.py`** to add two new optional steps:

```
Step 1: Text screening (existing)           ~0.1s
Step 2: Vision screening (existing)         ~8s
Step 3: Plan extraction (existing)          ~27s
Step 4: Metadata extraction (NEW)           ~3s   [runs parallel with Step 3]
Step 5: Photo extraction (NEW, optional)    ~8s   [runs after Step 2]
```

New flag: `python pipeline.py input/brochure.pdf --full` to run Steps 4+5 in addition to plan extraction. Default behavior unchanged (just plan extraction).

Total time with `--full`: ~45-55 seconds (Steps 3, 4 run in parallel; Step 5 after Step 2).

**Output additions to `plan_data.json`** (or new `listing.json`):
- Add `project_metadata` key with all extracted metadata
- Add `photos` key with photo file paths and classifications
- Keep backward compatibility — existing keys unchanged

---

## Layer 2: Web API Design

### Framework: FastAPI (Python)

**Why FastAPI**:
- Pipeline is already Python — direct import, no subprocess needed
- Built-in async support for background processing
- Auto-generated API docs at /docs
- Lightweight, no ORM needed (we use raw SQLite)
- Solo founder: one language, one process

### File: `webapp/app.py`

**Port: 5050** (5001 taken by wa-task-extractor, 5002-5003 free but keeping margin)

### Endpoints

#### `POST /api/upload`
```
Request:
  multipart/form-data
  - file: PDF file (max 100MB)

Response (202 Accepted):
{
  "job_id": "abc123",
  "status": "processing",
  "message": "PDF uploaded, extraction starting...",
  "estimated_time_s": 55
}

Behavior:
  1. Validate file is PDF, < 100MB
  2. Generate job_id (UUID or slug from filename + timestamp)
  3. Save PDF to uploads/<job_id>/brochure.pdf
  4. Insert job record into SQLite (status=processing)
  5. Start pipeline in background thread (BackgroundTasks)
  6. Return immediately
```

#### `GET /api/status/{job_id}`
```
Response:
{
  "job_id": "abc123",
  "status": "processing" | "completed" | "failed",
  "progress": {
    "step": "plan_extraction",
    "step_num": 3,
    "total_steps": 5,
    "message": "Extracting plan labels (12/17)..."
  },
  "started_at": "2026-03-26T10:30:00",
  "completed_at": null,
  "error": null
}

Behavior:
  - Read from SQLite jobs table
  - Frontend polls this every 2-3 seconds
```

#### `GET /api/listing/{job_id}`
```
Response:
{
  "job_id": "abc123",
  "project": {
    "name": "Godrej Skyline",
    "developer": "Godrej Properties",
    "location": {"area": "Koregaon Park Annexe", "city": "Pune"},
    "property_type": "residential",
    "rera_number": "P52100034837",
    "possession_date": "December 2028",
    "tagline": "Where the skyline meets your lifestyle"
  },
  "configurations": [
    {"type": "3BHK", "variant": "Luxury", "carpet_area": "125-127 sq m", "price": null}
  ],
  "amenities": ["Swimming Pool", "Clubhouse", ...],
  "unit_plans": [
    {
      "unit_type": "3BHK",
      "variant": "Luxury",
      "carpet_area": "125.64 sq m / 1352 sq ft",
      "tower": "Tower 1",
      "series": "X01",
      "image_url": "/api/images/abc123/unit-plan/tower1-3bhk-luxury-x01-125sqm.jpg",
      "confidence": "high"
    }
  ],
  "master_plans": [...],
  "floor_plans": [...],
  "photos": [
    {"type": "exterior_render", "image_url": "/api/images/abc123/photos/hero-exterior-1.jpg"}
  ],
  "pipeline_log": {
    "total_time_s": 49,
    "total_plans_found": 17
  }
}
```

#### `GET /api/listings`
```
Response:
{
  "listings": [
    {
      "job_id": "abc123",
      "project_name": "Godrej Skyline",
      "developer": "Godrej Properties",
      "city": "Pune",
      "configs": "3BHK, 4BHK",
      "status": "completed",
      "created_at": "2026-03-26T10:30:00",
      "thumbnail_url": "/api/images/abc123/unit-plan/tower1-3bhk-luxury-x01-125sqm.jpg"
    }
  ]
}
```

#### `GET /api/images/{job_id}/{path:path}`
```
Serves static images from output/<job_id>/<path>
Uses FileResponse with proper MIME types
```

### SQLite Schema: `webapp/listings.db`

```sql
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,           -- UUID
    slug TEXT UNIQUE,              -- human-readable: godrej-skyline-2026-03-26
    pdf_filename TEXT NOT NULL,
    pdf_path TEXT NOT NULL,
    status TEXT DEFAULT 'pending', -- pending, processing, completed, failed
    progress_step TEXT,            -- current step name
    progress_pct INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE listings (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    project_name TEXT,
    developer_name TEXT,
    location_area TEXT,
    location_city TEXT,
    property_type TEXT,
    rera_number TEXT,
    possession_date TEXT,
    tagline TEXT,
    price_min REAL,
    price_max REAL,
    total_towers INTEGER,
    total_units INTEGER,
    total_floors INTEGER,
    amenities_json TEXT,           -- JSON array
    contact_json TEXT,             -- JSON object
    highlights_json TEXT,          -- JSON array
    metadata_json TEXT,            -- full raw metadata JSON
    plan_data_json TEXT,           -- full plan_data.json content
    output_dir TEXT                -- path to output directory
);

CREATE TABLE configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT REFERENCES jobs(id),
    unit_type TEXT,                -- 3BHK, 4BHK
    variant TEXT,                  -- Luxury, Windsor
    carpet_area TEXT,
    saleable_area TEXT,
    price TEXT,
    plan_count INTEGER DEFAULT 0  -- how many unit plan images for this config
);

-- Index for quick lookups
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_listings_city ON listings(location_city);
CREATE INDEX idx_configs_job ON configs(job_id);
```

### Background Processing

**Approach**: FastAPI `BackgroundTasks` + threading

The pipeline takes ~50 seconds. Options considered:

| Option | Pros | Cons |
|--------|------|------|
| BackgroundTasks | Built-in, simple | Blocks thread |
| Celery + Redis | Production-grade | Overkill for solo founder, extra infra |
| Threading | Simple, non-blocking | Need to manage thread lifecycle |
| subprocess | Process isolation | Harder to get progress updates |

**Chosen: BackgroundTasks with threading.Thread**
- FastAPI's BackgroundTasks runs after response is sent
- Pipeline runs in a separate thread to not block the event loop
- Progress updates written to SQLite (frontend polls /api/status)
- If the server restarts during processing, job stays in "processing" status -> on startup, mark stale jobs as "failed"

**Progress reporting**:
- Modify pipeline.py to accept a `progress_callback(step, message, pct)` function
- Callback writes to SQLite jobs table
- Frontend polls every 3 seconds

---

## Layer 3: Frontend Design

### Tech Choice: Server-Side Templates + Alpine.js

**Why NOT React/Next.js**:
- Adds build step, node_modules, separate dev server
- Overkill for what is essentially 3 pages
- Solo founder cannot afford maintaining two stacks

**Why NOT pure static HTML**:
- Need templating for listing data
- Need reactivity for upload progress, tab switching, filtering

**Chosen: Jinja2 templates + Alpine.js + Tailwind CSS (CDN)**
- Jinja2 templates served by FastAPI (built-in support)
- Alpine.js for client-side interactivity (9KB, CDN, no build step)
- Tailwind CSS via CDN (no build step)
- Total: zero build process, zero node_modules
- Looks modern, works on mobile, fast to develop

### Pages

#### Page 1: Home / Upload (`/`)
```
+--------------------------------------------------+
|  PropCapture                          [Upload PDF] |
+--------------------------------------------------+
|                                                    |
|  Recent Listings                                   |
|  +------+ +------+ +------+                        |
|  |thumb | |thumb | |thumb |                         |
|  |Godrej| |Lodha | |DLF   |                         |
|  |Skyline| |Palava| |Camellias|                     |
|  |Pune  | |Mumbai| |Gurgaon|                        |
|  |3,4BHK| |2,3BHK| |4,5BHK|                         |
|  +------+ +------+ +------+                        |
|                                                    |
+--------------------------------------------------+
```

Features:
- Upload button -> modal with drag-and-drop PDF zone
- Upload progress bar (upload itself, then pipeline processing)
- Real-time status polling during processing
- Grid of completed listings with thumbnail, project name, city, configs

#### Page 2: Processing Status (`/processing/{job_id}`)
```
+--------------------------------------------------+
|  Processing: Godrej-Skyline-Brochure.pdf           |
|                                                    |
|  [=========>          ] 60%                         |
|                                                    |
|  Step 1: Text screening         DONE (0.1s)        |
|  Step 2: Vision screening       DONE (8s)           |
|  Step 3: Plan extraction        IN PROGRESS...      |
|  Step 4: Metadata extraction    PENDING              |
|  Step 5: Photo extraction       PENDING              |
|                                                    |
|  Estimated: ~20 seconds remaining                   |
+--------------------------------------------------+
```

Features:
- Polls `/api/status/{job_id}` every 3 seconds
- Shows step-by-step progress
- Auto-redirects to listing page when done

#### Page 3: Listing Page (`/listing/{job_id}`)

This is the main display page. Sections:

**3a. Hero Section**
```
+--------------------------------------------------+
|  [Hero image / exterior render]                    |
|                                                    |
|  GODREJ SKYLINE                                    |
|  by Godrej Properties                              |
|  Koregaon Park Annexe, Pune                        |
|                                                    |
|  3BHK & 4BHK | Possession: Dec 2028               |
|  RERA: P52100034837                                 |
+--------------------------------------------------+
```

**3b. Configurations Overview**
```
+--------------------------------------------------+
|  Configurations                                    |
|  +--------------------+ +--------------------+     |
|  | 3BHK Luxury        | | 3BHK Windsor       |     |
|  | 125-127 sq m       | | 113-114 sq m       |     |
|  | 1352-1365 sq ft    | | 1224-1363 sq ft    |     |
|  | 3 unit plans       | | 3 unit plans       |     |
|  +--------------------+ +--------------------+     |
|  +--------------------+ +--------------------+     |
|  | 4BHK Luxury        | | 4BHK Windsor       |     |
|  | 194 sq m           | | 152-153 sq m       |     |
|  | 2091-2397 sq ft    | | 1646-1649 sq ft    |     |
|  | 2 unit plans       | | 2 unit plans       |     |
|  +--------------------+ +--------------------+     |
+--------------------------------------------------+
```

**3c. Unit Plans Gallery (the star feature)**
```
+--------------------------------------------------+
|  Unit Plans                                        |
|                                                    |
|  [All] [3BHK] [4BHK]  <- filter tabs               |
|  [Luxury] [Windsor]   <- variant filter            |
|                                                    |
|  +------------------+ +------------------+         |
|  | [plan image]     | | [plan image]     |         |
|  | 3BHK Luxury X01  | | 3BHK Luxury X02  |         |
|  | 125.64 sq m      | | 126.73 sq m      |         |
|  | Tower 1          | | Tower 1          |         |
|  +------------------+ +------------------+         |
|  +------------------+ +------------------+         |
|  | [plan image]     | | [plan image]     |         |
|  | 4BHK Luxury X01  | | 4BHK Luxury X02  |         |
|  | 194.29 sq m      | | 194.35 sq m      |         |
|  +------------------+ +------------------+         |
|                                                    |
|  Click any plan -> lightbox with full-size image   |
+--------------------------------------------------+
```

Features:
- Tab filtering by BHK type (3BHK / 4BHK)
- Sub-filter by variant (Luxury / Windsor)
- Click to expand in lightbox overlay
- Shows carpet area, tower, series on each card
- Alpine.js handles filtering (no page reload)

**3d. Master Plan**
```
+--------------------------------------------------+
|  Master Plan                                       |
|  +----------------------------------------------+  |
|  |                                              |  |
|  |    [Ground Level Master Plan - full width]   |  |
|  |                                              |  |
|  +----------------------------------------------+  |
|                                                    |
|  +----------------------------------------------+  |
|  |    [Sky Level Amenities Plan]                 |  |
|  +----------------------------------------------+  |
+--------------------------------------------------+
```

**3e. Floor Plans**
```
+--------------------------------------------------+
|  Floor Plans                                       |
|  [Tower 1] [Tower 2] [Tower 3] [Tower 4]          |
|                                                    |
|  +----------------------------------------------+  |
|  | [Floor plan image - full width]               |  |
|  | Tower 1 - 3BHK Luxury - Typical Floor         |  |
|  +----------------------------------------------+  |
+--------------------------------------------------+
```

**3f. Amenities**
```
+--------------------------------------------------+
|  Amenities                                         |
|                                                    |
|  * Swimming Pool     * Clubhouse                   |
|  * Gymnasium         * Jogging Track               |
|  * Kids Play Area    * Yoga Deck                   |
|  * Tennis Court      * Multipurpose Hall           |
|  * Landscaped Garden * Senior Citizen Area          |
+--------------------------------------------------+
```

**3g. Specifications Table**
```
+--------------------------------------------------+
|  Specifications                                    |
|                                                    |
|  Property Type    Residential                      |
|  Developer        Godrej Properties                |
|  Location         Koregaon Park Annexe, Pune       |
|  RERA Number      P52100034837                     |
|  Possession       December 2028                   |
|  Towers           4                                |
|  Configurations   3BHK, 4BHK                       |
+--------------------------------------------------+
```

**3h. Photo Gallery** (if photos extracted)
```
+--------------------------------------------------+
|  Gallery                                           |
|  +--------+ +--------+ +--------+ +--------+      |
|  |exterior| |interior| |amenity | |location|       |
|  | render | | render | | photo  | |  map   |       |
|  +--------+ +--------+ +--------+ +--------+      |
+--------------------------------------------------+
```

### Component Structure (Alpine.js)

```javascript
// Main listing page component
Alpine.data('listing', () => ({
    data: {},                    // full listing JSON
    activeTab: 'all',           // unit plan BHK filter
    activeVariant: 'all',       // variant filter
    activeTower: 'all',         // floor plan tower filter
    lightboxImage: null,        // currently displayed image

    get filteredPlans() {
        return this.data.unit_plans.filter(p => {
            if (this.activeTab !== 'all' && p.unit_type !== this.activeTab) return false;
            if (this.activeVariant !== 'all' && p.variant !== this.activeVariant) return false;
            return true;
        });
    },

    get bhkTypes() {
        return [...new Set(this.data.unit_plans.map(p => p.unit_type))];
    },

    get variants() {
        return [...new Set(this.data.unit_plans.map(p => p.variant))];
    }
}));

// Upload component
Alpine.data('upload', () => ({
    uploading: false,
    processing: false,
    progress: 0,
    status: null,
    jobId: null,

    async uploadFile(file) { ... },
    async pollStatus() { ... }
}));
```

---

## Layer 4: File/Folder Structure

```
/root/projects/prop-capture/
├── CLAUDE.md                   # Updated with webapp docs
├── WEBAPP-PLAN.md              # This plan
├── ARCHITECTURE.md             # Existing pipeline architecture
│
├── pipeline.py                 # Existing (enhanced with progress callback)
├── screen.py                   # Existing
├── vision_screen.py            # Existing
├── extract_plans.py            # Existing
├── extract_metadata.py         # NEW: project-level data extraction
├── extract_photos.py           # NEW: lifestyle photo extraction
├── requirements.txt            # Updated with fastapi, uvicorn, etc.
│
├── webapp/
│   ├── app.py                  # FastAPI application
│   ├── db.py                   # SQLite database helpers
│   ├── models.py               # Pydantic models for API
│   ├── pipeline_runner.py      # Background pipeline execution wrapper
│   │
│   ├── templates/
│   │   ├── base.html           # Base template (head, nav, footer)
│   │   ├── index.html          # Home page with upload + listing grid
│   │   ├── processing.html     # Processing status page
│   │   └── listing.html        # Full listing display page
│   │
│   └── static/
│       ├── style.css           # Custom CSS (minimal, Tailwind handles most)
│       └── app.js              # Alpine.js component definitions
│
├── input/                      # PDF input directory
├── output/                     # Pipeline output directory
│   └── <slug>/
│       ├── unit-plan/
│       ├── master-plan/
│       ├── floor-plan/
│       ├── photos/             # NEW
│       ├── plan_data.json
│       └── listing.json        # NEW: full listing data
│
└── uploads/                    # NEW: uploaded PDFs (web interface)
    └── <job_id>/
        └── brochure.pdf
```

---

## Layer 5: Integration Design

### How webapp calls the pipeline

**Direct Python import, not subprocess.**

```python
# webapp/pipeline_runner.py

import sys
sys.path.insert(0, '/root/projects/prop-capture')

from pipeline import run_pipeline
from extract_metadata import extract_project_metadata
from extract_photos import extract_photos

def run_full_pipeline(job_id: str, pdf_path: str, output_dir: str, db):
    """
    Runs the full pipeline with progress reporting.
    Called in a background thread.
    """
    def update_progress(step, message, pct):
        db.update_job_progress(job_id, step, message, pct)

    try:
        # Step 1-3: Existing plan extraction
        update_progress("plan_extraction", "Extracting plans from PDF...", 10)
        plan_result = run_pipeline(
            pdf_path=pdf_path,
            output_base=output_dir,
            verbose=False
        )
        update_progress("plan_extraction", "Plans extracted", 60)

        # Step 4: Metadata extraction (uses page_texts from plan_result)
        update_progress("metadata", "Extracting project details...", 65)
        metadata = extract_project_metadata(pdf_path)
        update_progress("metadata", "Metadata extracted", 80)

        # Step 5: Photo extraction
        update_progress("photos", "Finding property photos...", 85)
        photos = extract_photos(pdf_path, output_dir, plan_result)
        update_progress("photos", "Photos extracted", 95)

        # Save combined listing.json
        listing_data = {**plan_result, "project_metadata": metadata, "photos": photos}
        # ... save to disk and DB

        update_progress("complete", "Done!", 100)
        db.complete_job(job_id)

    except Exception as e:
        db.fail_job(job_id, str(e))
```

### Progress updates during 50-second wait

The existing `pipeline.py` prints progress to stdout. For the web version:
1. **Modify `run_pipeline()`** to accept an optional `progress_callback` parameter
2. **Callback is called** at key points: after each step starts/completes, after each page is processed
3. **Callback writes to SQLite** jobs table
4. **Frontend polls** `/api/status/{job_id}` every 3 seconds
5. **UI shows**: step name, current progress, estimated remaining time

### File path handling

Currently `plan_data.json` has relative paths like `output/Godrej-Skyline-Brochure/unit-plan/...`. The web API needs to:
1. Store the absolute output directory path in the listings DB
2. When serving `/api/listing/{job_id}`, rewrite file_path values to `/api/images/{job_id}/unit-plan/...`
3. The `/api/images/` endpoint maps job_id to output directory and serves files

---

## Implementation Order

### Phase 1: Core API + Listing Display (MVP) — ~4 hours

**Goal**: Upload a PDF, run existing pipeline, display results as a listing page.

1. **`webapp/db.py`** — SQLite schema + helper functions (30 min)
2. **`webapp/models.py`** — Pydantic models for API responses (20 min)
3. **`webapp/app.py`** — FastAPI app with all endpoints (1 hour)
4. **`webapp/pipeline_runner.py`** — Background pipeline wrapper (30 min)
5. **`webapp/templates/base.html`** — Base template with Tailwind + Alpine (20 min)
6. **`webapp/templates/listing.html`** — Full listing page with all sections (1.5 hours)
7. **`webapp/templates/index.html`** — Home page with upload + listing grid (30 min)
8. **`webapp/templates/processing.html`** — Processing status page (20 min)
9. **`webapp/static/app.js`** — Alpine.js components (30 min)
10. **Test end-to-end** with Godrej Skyline PDF (30 min)

### Phase 2: Enhanced Extraction — ~2 hours

11. **`extract_metadata.py`** — Project metadata extraction module (1 hour)
12. **`extract_photos.py`** — Photo extraction module (45 min)
13. **Integrate into pipeline_runner.py** (15 min)

### Phase 3: Polish + Deploy — ~1 hour

14. **`requirements.txt`** update (5 min)
15. **PM2 or systemd setup** (15 min)
16. **Caddy reverse proxy** if needed (10 min)
17. **CLAUDE.md** update (10 min)
18. **Test with 2-3 different brochures** (30 min)

**Total estimated: ~7 hours**

---

## Deployment Plan

### Process Management: PM2

```bash
# Start the webapp
cd /root/projects/prop-capture
pm2 start "uvicorn webapp.app:app --host 0.0.0.0 --port 5050" --name prop-capture-web
pm2 save
```

**Why PM2 over systemd**: Consistent with other Node/Python apps on this server (sitegen, konzult-tracker, etc.). Easier to manage alongside existing processes.

### Port: 5050

Available ports checked:
- 3000: Docker (VoicERA frontend)
- 3100: Paperclip
- 4000: aditya-expense
- 5001: wa-task-extractor
- 8000: Docker (VoicERA backend)
- **5050: FREE** — chosen for prop-capture web

### Caddy (optional, for public access)

If the dealer needs public access (e.g., prop.breakandi.com):
```
prop.breakandi.com {
    reverse_proxy localhost:5050
}
```

For now, internal-only at `http://157.119.41.234:5050` is sufficient for v1. Add iptables rule if needed to restrict access.

### Dependencies to add to requirements.txt

```
# Existing
pymupdf>=1.23.0
groq>=0.9.0
python-dotenv>=1.0.0

# New for webapp
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
python-multipart>=0.0.9    # for file uploads
jinja2>=3.1.0              # for templates
aiofiles>=24.0.0           # for static file serving
```

---

## Risks & Edge Cases

### Risk 1: Pipeline crashes during background execution
**Impact**: Job stuck in "processing" forever.
**Mitigation**:
- Wrap entire pipeline in try/except in pipeline_runner.py
- On exception, mark job as "failed" with error message
- On webapp startup, mark any "processing" jobs as "failed" (stale from crash)

### Risk 2: Large PDFs (100+ pages, 50+ MB)
**Impact**: Upload timeout, memory issues during rendering.
**Mitigation**:
- Set upload limit to 100MB in FastAPI
- Pipeline already handles large PDFs efficiently (text screening eliminates most pages)
- Add a timeout on pipeline execution (5 minutes max)

### Risk 3: Concurrent uploads
**Impact**: Multiple pipeline threads running simultaneously could exhaust Groq rate limits.
**Mitigation**:
- v1: Simple threading, one at a time is fine for a single dealer
- v2 (if needed): Add a queue (just a Python queue.Queue, not Celery)

### Risk 4: Hindi/regional text in brochures
**Impact**: Metadata extraction might miss Hindi project names or locations.
**Mitigation**: Groq's llama models handle Hindi well. The metadata extraction prompt should explicitly mention "Extract text regardless of language — Hindi, Marathi, or English."

### Risk 5: No price in brochure
**Impact**: Most Indian real estate brochures intentionally omit pricing.
**Mitigation**: Price fields are nullable throughout. The listing page gracefully hides the price section if null.

### Risk 6: Brochure without floor plans
**Impact**: Pipeline finds 0 plans, listing page is empty.
**Mitigation**:
- Metadata extraction still works (project name, amenities, etc.)
- Listing page shows "No unit plans found in this brochure" gracefully
- Photo extraction still captures hero images

### Edge Case: Same project uploaded twice
**Handling**: Each upload gets a unique job_id. Duplicate detection is NOT needed for v1 — the dealer knows what they're uploading.

### Edge Case: Brochure is a scan (image-only PDF)
**Handling**: Text screening finds no text, but image detection still catches large images. Vision screening classifies them. Metadata extraction will fail (no text) — this is acceptable for v1, noted as a limitation.

---

## Verification Plan

1. **Pipeline integration test**: Upload Godrej Skyline PDF via API, verify plan_data.json matches existing output
2. **Metadata extraction test**: Run extract_metadata.py standalone on the Godrej Skyline text, verify project name = "Godrej Skyline", RERA number extracted, amenities list populated
3. **Listing page visual check**: Open /listing/{job_id} in browser, verify all sections render with real data
4. **Unit plan filtering**: Click 3BHK tab -> only 3BHK plans shown. Click 4BHK -> only 4BHK. Click All -> all shown
5. **Image serving**: All images load in the listing page (no broken images)
6. **Upload flow**: Upload a new PDF from the home page, see processing status, get redirected to listing
7. **Error handling**: Upload a non-PDF file -> error message. Upload a 200MB file -> rejection
8. **Mobile responsiveness**: Open listing page on phone (or Chrome DevTools mobile view) -> all sections readable
9. **Multiple listings**: Upload 2-3 different brochures, home page shows all listings in a grid

---

## What's Missing from Current Pipeline (Gaps Analysis)

| Gap | Severity | Fix Effort | In This Plan? |
|-----|----------|------------|---------------|
| No project name/developer extraction | Critical | `extract_metadata.py` (1h) | Yes |
| No amenities extraction | High | Part of metadata extraction | Yes |
| No RERA/possession extraction | High | Part of metadata extraction | Yes |
| No property photos | Medium | `extract_photos.py` (45min) | Yes |
| No price extraction | Low | Part of metadata (usually null) | Yes |
| No location/city extraction | Critical | Part of metadata extraction | Yes |
| Plans missing tower assignment | Medium | Already in plan_data (some null) | Existing |
| No OCR for scanned PDFs | Low (rare) | v2 enhancement | No |
| No multi-language support | Low | Groq handles Hindi | Partial |
| No comparison between projects | Low | v2 feature | No |
| No PDF download/share | Low | v2 feature | No |
