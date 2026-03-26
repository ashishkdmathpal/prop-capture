# PropCapture Expansion Plan: All Property Images

## Context

The current pipeline extracts only floor plans (unit_plan, master_plan, floor_plan) from real estate PDF brochures. The text screener (`screen.py`) filters pages using floor-plan-specific keywords, and the vision screener (`vision_screen.py`) classifies candidates into 5 types: `unit_plan / master_plan / floor_plan / area_table / other`.

The client now wants ALL property-specific images extracted from brochures -- amenity renders, building exteriors, interior shots, location maps, specification tables, and lifestyle photos. These pages are currently being discarded by both the text screener (which only looks for plan keywords) and the vision screener (which buckets everything non-plan as `other`).

### Godrej Skyline Brochure Analysis (50 pages)

Current pipeline captures 17 pages (plans only). Here is the full breakdown by new category:

| New Category | Pages | Count | Currently captured? |
|---|---|---|---|
| unit_plan | 26,27,28,32,33,37,38,42,43,44 | 10 | Yes |
| master_plan | 20,21 | 2 | Yes |
| floor_plan | 24,30,35,40 | 4 | Yes |
| exterior_image | 2,7,8,11,18,19 | 6 | No |
| interior_image | 9,22,45 | 3 | No |
| amenity_image | 10,12,13,15,16,17 | 6 | No |
| location_map | 4 | 1 | No |
| specification_table | 5,46,47,49 | 4 | No |
| lifestyle_image | 14 | 1 | No |
| other (cover, dividers, logos) | 1,3,6,23,25,29,31,34,36,39,41,48,50 | 13 | No (correctly skipped) |

**New images to extract: ~21 pages** (exterior: 6, interior: 3, amenity: 6, location: 1, spec: 4, lifestyle: 1).

---

## Approach

The key architectural insight: the current pipeline's bottleneck is Step 1 (text screener), which filters pages using floor-plan keywords. Image-heavy pages like amenity renders and exterior shots have zero or minimal text and get filtered out before vision screening ever sees them.

**Strategy: Two-track screening.**

Instead of trying to make the text screener catch photo pages (which by definition have little text), we add a parallel "image screener" path that catches pages with large embedded images and minimal text. Both tracks feed into the vision screener, which gets an expanded classification vocabulary.

This avoids breaking the existing plan-extraction logic. Plans still flow through the same path they always did. New image types get a new, simpler labeling module.

---

## Steps

### Step 1: Update `screen.py` -- Add image-page detection track

**File:** `/root/projects/prop-capture/screen.py`

**What changes:**
- Add a new function `_is_image_page(page, doc) -> dict | None` that flags pages with:
  - A large embedded image (>1000px in either dimension or >100KB raw)
  - AND less than 500 chars of extractable text (photo pages have minimal text overlays)
  - OR page has >80% of its area covered by a single image (full-bleed renders)
- Add a new return list `image_candidates` to `screen_pages()` alongside existing `candidates` and `master_plan_candidates`
- The `all_candidates` union now includes `image_candidates`
- Each image candidate gets a `likely_type: "property_image"` for downstream routing

**Key logic:**
```python
def _is_image_page(page, doc, text: str) -> dict | None:
    """Detect pages dominated by images (renders, photos, amenity shots)."""
    text_len = len(text.strip())
    images = page.get_images(full=True)

    if not images:
        return None

    # Check for large embedded images
    has_large_image = False
    for img in images:
        width, height = img[2], img[3]
        if width > 1000 or height > 1000:
            has_large_image = True
            break
        try:
            raw = doc.extract_image(img[0])
            if raw and len(raw.get("image", b"")) > 100_000:
                has_large_image = True
                break
        except Exception:
            pass

    if not has_large_image:
        return None

    # Image pages have limited text (marketing headlines + disclaimers)
    if text_len > 500:
        return None  # Too much text -- probably a spec table or plan page

    return {
        "page_num": page_num,
        "score": 0,  # Not scored by keyword system
        "master_score": 0,
        "signals": ["image_dominant_page"],
        "text_snippet": text[:300].strip(),
        "likely_type": "property_image",
        "drawing_count": 0,
        "text_length": text_len,
    }
```

**Important:** The text threshold of 500 chars needs tuning. Some pages like the connectivity table (page 5) have lots of text AND images. Those get caught by the existing keyword screener. The image screener is specifically for pages that the keyword screener misses.

**Also add:** A new keyword category for specification/location pages that the existing screener misses:

```python
SPEC_KEYWORDS = [
    r"\bspecification\b", r"\bflooring\b", r"\bbathroom\b", r"\bkitchen\b",
    r"\bpayment plan\b", r"\bmilestone\b", r"\bconstruction linked\b",
]

LOCATION_KEYWORDS = [
    r"\b\d+\s*kms?\b", r"\b\d+\s*minutes?\b", r"\bit parks?\b",
    r"\bhospital\b", r"\bschool\b", r"\bmall\b", r"\bclub\b",
]
```

These can boost the score for pages that contain spec/location content, ensuring they get sent to vision screening.

**Return structure update:**
```python
return {
    "candidates": candidates,                # existing
    "master_plan_candidates": master_plan_candidates,  # existing
    "image_candidates": image_candidates,    # NEW
    "all_candidates": all_candidates,        # union of all three
    "total_pages": total_pages,
    "screening_time_ms": elapsed_ms,
    "page_texts": page_texts,
}
```

### Step 2: Update `vision_screen.py` -- Expanded classification

**File:** `/root/projects/prop-capture/vision_screen.py`

**What changes:**

1. **Expand `VALID_TYPES` and `PLAN_TYPES`:**
```python
VALID_TYPES = {
    "unit_plan", "master_plan", "floor_plan",        # existing plan types
    "amenity_image", "exterior_image", "interior_image",  # new image types
    "location_map", "specification_table",                # new non-image types
    "lifestyle_image",                                    # new
    "area_table",                                         # existing
    "other",                                              # existing
}

PLAN_TYPES = {"unit_plan", "master_plan", "floor_plan"}  # unchanged

IMAGE_TYPES = {"amenity_image", "exterior_image", "interior_image",
               "location_map", "lifestyle_image"}

DATA_TYPES = {"specification_table", "area_table"}
```

2. **Update `CLASSIFY_PROMPT` with new categories:**

```
You are analyzing pages from an Indian real estate property brochure PDF.

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
```

3. **Export new type sets** so `pipeline.py` can use them:
```python
# Existing
PLAN_TYPES = {"unit_plan", "master_plan", "floor_plan"}
# New
IMAGE_TYPES = {"amenity_image", "exterior_image", "interior_image", "location_map", "lifestyle_image"}
DATA_TYPES = {"specification_table", "area_table"}
EXTRACTABLE_TYPES = PLAN_TYPES | IMAGE_TYPES | DATA_TYPES
```

4. **Increase `max_tokens`** from 500 to 800 (more categories = longer responses).

### Step 3: Create `extract_images.py` -- New labeling module for non-plan images

**New file:** `/root/projects/prop-capture/extract_images.py`

This is a simpler version of `extract_plans.py` tailored for property images. It does NOT need carpet area, BHK type, series, etc. It needs:

- **For amenity_image:** What amenity is shown (swimming-pool, gym, clubhouse, banquet-hall, sky-deck, garden, play-area, games-room, yoga-deck, etc.)
- **For exterior_image:** What view (tower-exterior, entrance-lobby, aerial-view, retail-podium, night-view)
- **For interior_image:** What room (master-bedroom, living-room, kitchen, dining-area, bathroom)
- **For location_map:** Label as "location-distance-map" or "connectivity-map"
- **For lifestyle_image:** Label as "lifestyle-balcony-view", "lifestyle-terrace", etc.
- **For specification_table:** Label as "specifications-t1-t2" or "payment-plan", etc.

**Prompt for image labeling:**
```
You are analyzing an image page from an Indian real estate property brochure.
This page has been identified as a: {image_type}

Page text extracted from PDF:
{page_text}

Extract the following and return ONLY valid JSON (no markdown):
{{
  "image_type": "{image_type}",
  "label": "descriptive-filename-safe-label-max-50-chars",
  "description": "one line description of what the image shows",
  "amenities_shown": ["pool", "gym"] or null (only for amenity_image),
  "confidence": "high or medium or low"
}}

Label rules:
- For amenity_image: label like "swimming-pool", "sky-gym", "clubhouse-banquet"
- For exterior_image: label like "tower-exterior-day", "entrance-lobby", "aerial-view"
- For interior_image: label like "master-bedroom-render", "living-dining-room"
- For location_map: label like "distance-map" or "connectivity-table"
- For specification_table: label like "specs-tower1-tower2" or "payment-plan"
- For lifestyle_image: label like "balcony-lifestyle-view"
```

**Directory mapping:**
```python
IMAGE_TYPE_DIR_MAP = {
    "amenity_image": "amenity",
    "exterior_image": "exterior",
    "interior_image": "interior",
    "location_map": "location",
    "lifestyle_image": "lifestyle",
    "specification_table": "specification",
}
```

**Function signature:**
```python
def extract_image_labels(
    pdf_path: str,
    confirmed_pages: list[dict],   # [{page, type, confidence, note}, ...]
    output_dir: str | Path,
    page_texts: dict | None = None,
    dpi: int = 150,
    verbose: bool = True,
) -> list[dict]:
```

This mirrors `extract_plan_labels()` but uses the simpler image prompt and saves to the new directories.

### Step 4: Update `pipeline.py` -- Wire in the new modules

**File:** `/root/projects/prop-capture/pipeline.py`

**Changes:**

1. **Import new types and module:**
```python
from vision_screen import classify_pages, PLAN_TYPES, IMAGE_TYPES, DATA_TYPES, EXTRACTABLE_TYPES
from extract_plans import extract_plan_labels
from extract_images import extract_image_labels
```

2. **After vision screening, split results into three tracks:**
```python
plan_pages = [p for p in classified if p.get("type") in PLAN_TYPES]
image_pages = [p for p in classified if p.get("type") in IMAGE_TYPES]
data_pages = [p for p in classified if p.get("type") in DATA_TYPES]
```

3. **Step 3 becomes two parallel extraction passes:**
```python
# Step 3a: Extract plan labels (existing logic, unchanged)
plan_results = extract_plan_labels(
    pdf_path=pdf_path,
    confirmed_pages=plan_pages,
    output_dir=out_dir,
    page_texts=page_texts,
    dpi=dpi,
    verbose=verbose,
)

# Step 3b: Extract image labels (new)
image_results = extract_image_labels(
    pdf_path=pdf_path,
    confirmed_pages=image_pages + data_pages,
    output_dir=out_dir,
    page_texts=page_texts,
    dpi=dpi,
    verbose=verbose,
)
```

4. **Update the summary/JSON output:**
```python
summary = {
    "pdf_source": pdf_stem,
    "total_pages": screen_result["total_pages"],
    "candidate_pages": candidate_page_nums,
    "confirmed_plan_pages": [p["page"] for p in plan_pages],
    "confirmed_image_pages": [p["page"] for p in image_pages + data_pages],
    # Existing plan arrays
    "unit_plans": [r for r in plan_results if r.get("plan_type") == "unit_plan"],
    "master_plans": [r for r in plan_results if r.get("plan_type") == "master_plan"],
    "floor_plans": [r for r in plan_results if r.get("plan_type") == "floor_plan"],
    # New image arrays
    "amenity_images": [r for r in image_results if r.get("image_type") == "amenity_image"],
    "exterior_images": [r for r in image_results if r.get("image_type") == "exterior_image"],
    "interior_images": [r for r in image_results if r.get("image_type") == "interior_image"],
    "location_images": [r for r in image_results if r.get("image_type") == "location_map"],
    "lifestyle_images": [r for r in image_results if r.get("image_type") == "lifestyle_image"],
    "specification_tables": [r for r in image_results if r.get("image_type") == "specification_table"],
    # Totals
    "total_plans_found": len(plan_results),
    "total_images_found": len(image_results),
    "pipeline_log": log,
}
```

5. **Update the final summary print:**
```python
print(f"  Unit plans:     {len(unit_plans)}")
print(f"  Master plans:   {len(master_plans)}")
print(f"  Floor plans:    {len(floor_plans)}")
print(f"  Amenity images: {len(amenity_images)}")
print(f"  Exterior images:{len(exterior_images)}")
print(f"  Interior images:{len(interior_images)}")
print(f"  Location maps:  {len(location_images)}")
print(f"  Specs/tables:   {len(spec_tables)}")
print(f"  Total:          {len(plan_results) + len(image_results)}")
```

### Step 5: Update `prop-capture-web/main.py` -- Pass new data to template

**File:** `/root/projects/prop-capture-web/main.py`

**Changes:**

1. **In `run_pipeline_thread()`, fix file paths for ALL new image arrays** (same logic as existing plan arrays):
```python
for plan_list in ("unit_plans", "master_plans", "floor_plans",
                   "amenity_images", "exterior_images", "interior_images",
                   "location_images", "lifestyle_images", "specification_tables"):
    for plan in result.get(plan_list, []):
        if plan.get("file_path"):
            # ... existing URL fixing logic ...
```

2. **In `/results/` route, pass the new data to the template:**
The `result` dict already gets passed as-is, so no change needed for template data. But add image type counts to the summary bar context if desired.

### Step 6: Update `prop-capture-web/templates/results.html` -- New sections

**File:** `/root/projects/prop-capture-web/templates/results.html`

**Changes:**

1. **Update summary bar** with new image counts:
```html
<div class="stat">
  <span class="stat-num">{{ (result.amenity_images|default([])) | length + (result.exterior_images|default([])) | length + (result.interior_images|default([])) | length }}</span>
  <span class="stat-label">Property Images</span>
</div>
```

2. **Add new sections after Floor Plans, before JSON section.** Each section follows the same pattern as the existing Floor Plans section. Example for Amenity Images:

```html
<!-- Amenity Images Section -->
<div class="section">
  <div class="section-header">
    <h2 class="section-title">Amenity Images</h2>
    <span class="badge">{{ (result.amenity_images|default([])) | length }}</span>
  </div>
  {% if result.amenity_images %}
  <div class="plans-grid">
    {% for img in result.amenity_images %}
    <div class="plan-card">
      {% if img.file_path %}
      <img src="{{ img.file_path }}" alt="{{ img.label or 'amenity' }}" onclick="openLightbox('{{ img.file_path }}')" loading="lazy" />
      {% endif %}
      <div class="plan-info">
        <span class="plan-type-chip chip-amenity">Amenity</span>
        <div class="plan-label">{{ img.label or 'unlabeled' }}</div>
        {% if img.description %}
        <div class="plan-meta">{{ img.description }}</div>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <p class="empty-state">No amenity images found.</p>
  {% endif %}
</div>
```

Repeat for: Exterior Images, Interior Images, Location Maps, Specifications, Lifestyle Images.

3. **Add CSS chip colors for new types:**

**File:** `/root/projects/prop-capture-web/static/style.css`

```css
.chip-amenity { background: #dcfce7; color: #166534; }
.chip-exterior { background: #e0e7ff; color: #3730a3; }
.chip-interior { background: #fce7f3; color: #9d174d; }
.chip-location { background: #ffedd5; color: #9a3412; }
.chip-spec { background: #f1f5f9; color: #475569; }
.chip-lifestyle { background: #fef3c7; color: #92400e; }
```

### Step 7: Update `CLAUDE.md` and documentation

**File:** `/root/projects/prop-capture/CLAUDE.md`

Update the architecture diagram, output structure, and performance table to reflect:
- New Step 3b (image extraction)
- New output directories (amenity/, exterior/, interior/, location/, lifestyle/, specification/)
- New JSON fields
- Updated performance estimates

---

## Output Structure (Updated)

```
output/<pdf_name>/
+-- unit-plan/           (existing)
+-- master-plan/         (existing)
+-- floor-plan/          (existing)
+-- amenity/             NEW - pool, gym, clubhouse, garden, play area renders
+-- exterior/            NEW - building facade, entrance, aerial renders
+-- interior/            NEW - bedroom, living room, kitchen renders
+-- location/            NEW - distance/connectivity maps
+-- lifestyle/           NEW - lifestyle photos with people
+-- specification/       NEW - spec tables, payment plans
+-- pages/               (existing - reference renders)
+-- plan_data.json       (extended with new arrays)
```

---

## JSON Schema (Updated)

```json
{
  "pdf_source": "Godrej-Skyline-Brochure",
  "total_pages": 50,
  "candidate_pages": [2, 4, 5, 7, 8, 9, ...],
  "confirmed_plan_pages": [20, 21, 24, 26, 27, ...],
  "confirmed_image_pages": [2, 4, 5, 7, 8, 9, ...],

  "unit_plans": [ /* existing structure unchanged */ ],
  "master_plans": [ /* existing structure unchanged */ ],
  "floor_plans": [ /* existing structure unchanged */ ],

  "amenity_images": [
    {
      "page_num": 16,
      "image_type": "amenity_image",
      "label": "infinity-edge-swimming-pool",
      "description": "Infinity edge swimming pool with palm trees and water features",
      "amenities_shown": ["swimming-pool", "water-feature"],
      "confidence": "high",
      "file_path": "output/.../amenity/infinity-edge-swimming-pool.jpg"
    }
  ],
  "exterior_images": [
    {
      "page_num": 18,
      "image_type": "exterior_image",
      "label": "tower-full-building-view",
      "description": "Full building exterior render with retail podium at dusk",
      "confidence": "high",
      "file_path": "output/.../exterior/tower-full-building-view.jpg"
    }
  ],
  "interior_images": [
    {
      "page_num": 22,
      "image_type": "interior_image",
      "label": "master-bedroom-render",
      "description": "Luxury master bedroom with marble wall and balcony view",
      "confidence": "high",
      "file_path": "output/.../interior/master-bedroom-render.jpg"
    }
  ],
  "location_images": [
    {
      "page_num": 4,
      "image_type": "location_map",
      "label": "distance-map-pune",
      "description": "Concentric distance map showing travel times from project site",
      "confidence": "high",
      "file_path": "output/.../location/distance-map-pune.jpg"
    }
  ],
  "lifestyle_images": [ /* same structure */ ],
  "specification_tables": [ /* same structure */ ],

  "total_plans_found": 16,
  "total_images_found": 21,
  "pipeline_log": { /* existing structure */ }
}
```

---

## Expected Output: Godrej Skyline Brochure

| Category | Expected Count | Pages |
|---|---|---|
| unit_plan | 10 | 26,27,28,32,33,37,38,42,43,44 |
| master_plan | 2 | 20,21 |
| floor_plan | 4 | 24,30,35,40 |
| **amenity_image** | **6** | 10,12,13,15,16,17 |
| **exterior_image** | **6** | 2,7,8,11,18,19 |
| **interior_image** | **3** | 9,22,45 |
| **location_map** | **1** | 4 |
| **specification_table** | **4** | 5,46,47,49 |
| **lifestyle_image** | **1** | 14 |
| other (skipped) | 13 | 1,3,6,23,25,29,31,34,36,39,41,48,50 |

**Total extracted: 37 pages** (up from 16 currently).
**New images: 21** across 6 new categories.

---

## Dependencies & Implementation Order

```
Step 1 (screen.py)  ----+
                        |
Step 2 (vision_screen)--+---> Step 4 (pipeline.py) ---> Step 5 (web main.py) ---> Step 7 (docs)
                        |
Step 3 (extract_images) +---> Step 6 (results.html + CSS)
```

**Order:**
1. Steps 1, 2, 3 can be done in parallel (no dependencies between them)
2. Step 4 depends on Steps 1, 2, 3 being complete
3. Steps 5 and 6 can be done in parallel after Step 4
4. Step 7 (docs) is last

**Recommended serial order for a single agent:**
1. `screen.py` (add image page detection)
2. `vision_screen.py` (expand classification)
3. `extract_images.py` (create new module)
4. `pipeline.py` (wire everything together)
5. Test: run `python pipeline.py input/Godrej-Skyline-Brochure.pdf` and verify output
6. `results.html` + `style.css` (web UI update)
7. `main.py` (web backend fix for file paths)
8. Test: upload PDF via web app
9. `CLAUDE.md` (documentation)

---

## Performance Impact Estimate

| Metric | Current | After Expansion |
|---|---|---|
| Text screening | ~0.1s (50 pages) | ~0.1s (unchanged) |
| Vision screening candidates | 21-27 pages | 35-40 pages |
| Vision screening API calls | 6 (batches of 4) | 10 (batches of 4) |
| Vision screening time | ~8s | ~14s |
| Plan extraction (Step 3a) | 15 pages, ~40s | 16 pages, ~40s (unchanged) |
| Image extraction (Step 3b) | N/A | ~21 pages, ~50s |
| **Total time** | **~50s** | **~105s** |
| **Total API calls** | **21** | **~47** |
| **Estimated cost** | ~$0.01 | ~$0.02 |

The main cost increase comes from Step 3b (image labeling). This could be optimized later by batching image labels (4 at a time like vision screening) since they need much less output per page than plans.

---

## Risks & Edge Cases

### 1. Regression on plan extraction
**Risk:** Changing the text screener might accidentally exclude pages that currently pass.
**Mitigation:** The changes to `screen.py` are purely additive -- new `image_candidates` list is a UNION with existing candidates, never subtraction. Run existing pipeline first, save output, then run expanded pipeline and verify all 16 existing plans are still captured.

### 2. Full-bleed images with no text
**Risk:** Pages like the bedroom render (page 22) or living room (page 45) have almost zero extractable text. The text screener may not catch them.
**Mitigation:** The new `_is_image_page()` function specifically targets these. It checks embedded image dimensions/size, not text content.

### 3. Section divider pages
**Risk:** Pages like "TOWER 1 (Luxury) 3BHK Floor plan" (page 23) have a decorative background image + text. Could be misclassified as `interior_image` or `lifestyle_image`.
**Mitigation:** The vision screener prompt explicitly calls out "section divider" in the `other` category. These pages have centered short text with watercolor/gradient backgrounds and no photo content.

### 4. Vision model confusion between categories
**Risk:** The swim pool page (page 16) with elderly people walking nearby could be classified as `lifestyle_image` instead of `amenity_image`.
**Mitigation:** The prompt rules state: "A page with a render of a swimming pool = amenity_image, not lifestyle_image". Test and tune.

### 5. Increased API usage
**Risk:** Nearly doubling the number of API calls.
**Mitigation:** Groq pricing is very cheap (~$0.01 additional per run). Not a concern.

### 6. Text threshold for image detection
**Risk:** Setting the text threshold too high in `_is_image_page()` might accidentally include specification tables (which have lots of text but also images).
**Mitigation:** Spec tables are already caught by the existing keyword screener (they contain words like "flooring", "bathroom"). The image screener is a fallback, not the primary path for text-heavy pages. Use 500 chars as initial threshold, tune based on results.

### 7. Specification tables vs area tables
**Risk:** The existing `area_table` type and new `specification_table` might overlap.
**Mitigation:** `area_table` = a table of apartment areas/prices (carpet area, RERA numbers). `specification_table` = material specs (flooring type, bathroom fittings). The vision prompt makes this distinction clear.

---

## Verification

1. **Run the expanded pipeline on Godrej Skyline and verify:**
   - All 16 existing plan images still extracted correctly (no regression)
   - ~21 new images extracted to correct subdirectories
   - `plan_data.json` contains all new arrays
   - Labels make sense (e.g., pool image labeled "swimming-pool", not "garden")

2. **Upload via web app and verify:**
   - All new sections appear in the results page
   - Images display correctly with labels
   - Lightbox works for new images
   - No broken image paths

3. **Edge case test:** Find a brochure with fewer amenity images or different layout to verify the screener doesn't false-positive on text-heavy marketing pages.

---

## Questions for User Before Implementation

1. **Should specification tables (flooring, bathroom specs, payment plan) be extracted?** They are useful context but not "property images" in the visual sense. The plan includes them but they could be excluded to keep it focused.

2. **Should the web results page group amenity/exterior/interior together as "Property Gallery" or show them as separate sections?** Current plan: separate sections. Alternative: one combined gallery with type filter tabs (like BHK filter on unit plans).

3. **DPI for property images:** Plan images use 150 DPI for detail. Should property photos use the same, or is 100 DPI sufficient (smaller files, faster processing)?
