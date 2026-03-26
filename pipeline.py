"""
pipeline.py — prop-capture v2: extract all property images from real estate PDFs.

Usage:
    python pipeline.py input/brochure.pdf
    python pipeline.py input/brochure.pdf --dpi 200
    python pipeline.py input/brochure.pdf --output-dir /tmp/output

Pipeline:
    Step 1: Text screening (screen.py)         — 0 API calls, ~0.1s
    Step 2: Vision screening (vision_screen.py) — Groq llama-4-scout, ~10-15s
    Step 3a: Plan extraction (extract_plans.py) — Groq llama-4-scout, ~40s
    Step 3b: Image extraction (extract_images.py) — Groq llama-4-scout, ~50s

Output:
    output/<pdf_name>/
    ├── unit-plan/      ← individual apartment floor plans
    ├── master-plan/    ← site/project layout maps
    ├── floor-plan/     ← whole building floors (multiple units)
    ├── amenity/        ← pool, gym, clubhouse, garden, play area renders
    ├── exterior/       ← building facade, entrance, aerial renders
    ├── interior/       ← bedroom, living room, kitchen renders
    ├── location/       ← distance/connectivity maps
    ├── lifestyle/      ← lifestyle photos with people
    ├── specification/  ← spec tables, payment plans
    ├── pages/          ← reference page renders (if pre-rendered)
    └── plan_data.json  ← structured metadata for all images found
"""

import argparse
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv("/root/.secrets.env")

from screen import screen_pages
from vision_screen import classify_pages, PLAN_TYPES, IMAGE_TYPES, DATA_TYPES
from extract_plans import extract_plan_labels
from extract_images import extract_image_labels


def run_pipeline(
    pdf_path: str,
    output_base: str = "output",
    dpi: int = 150,
    verbose: bool = True,
    step_callback=None,  # callable(step: int, label: str, detail: str = "")
) -> dict:
    """
    Full pipeline: PDF -> text screen -> vision screen -> plan + image extraction.

    Args:
        pdf_path:      Path to the input PDF file
        output_base:   Base directory for output (default: "output")
        dpi:           DPI for high-res renders in step 3 (default: 150)
        verbose:       Print progress to stdout
        step_callback: Optional callable(step_num: int, label: str) called at each step start

    Returns:
        Summary dict with all image counts, file paths, and pipeline timing.
    """
    pdf_path = str(pdf_path)
    pdf_stem = Path(pdf_path).stem
    out_dir = Path(output_base) / pdf_stem
    out_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = out_dir / "pages"

    t_pipeline_start = time.time()
    log = {
        "pdf": pdf_path,
        "output_dir": str(out_dir),
        "steps": {},
    }

    print(f"\n{'='*60}")
    print(f"prop-capture pipeline v2 (all property images)")
    print(f"Input:  {pdf_path}")
    print(f"Output: {out_dir}")
    print(f"{'='*60}\n")

    # ------------------------------------------------------------------
    # Step 1: Text screening
    # ------------------------------------------------------------------
    def _progress(step, label, detail=""):
        if step_callback:
            step_callback(step, label, detail)

    _progress(1, "Scanning all pages", f"Analyzing {Path(pdf_path).stem}")
    print("Step 1/3: Text screening...")
    t1 = time.time()

    screen_result = screen_pages(pdf_path)
    all_candidates = screen_result["all_candidates"]
    page_texts = screen_result["page_texts"]
    candidate_page_nums = sorted(set(c["page_num"] for c in all_candidates))

    elapsed1 = time.time() - t1
    log["steps"]["text_screen"] = {
        "time_s": round(elapsed1, 2),
        "total_pages": screen_result["total_pages"],
        "candidates": len(candidate_page_nums),
        "pages_scanned": candidate_page_nums,
    }

    print(f"  {screen_result['total_pages']} pages scanned in {elapsed1:.1f}s")
    print(f"  {len(candidate_page_nums)} candidate pages: {candidate_page_nums}")

    if not candidate_page_nums:
        print("  No candidate pages found — nothing to process.")
        return {"error": "no_candidates", "log": log}

    # ------------------------------------------------------------------
    # Step 2: Vision screening (Groq)
    # ------------------------------------------------------------------
    _progress(1, "Scanning all pages", f"Found {len(candidate_page_nums)} pages with content")
    _progress(2, "Classifying pages", f"Analyzing {len(candidate_page_nums)} pages with AI vision")
    print("\nStep 2/3: Classifying pages with Groq...")
    t2 = time.time()

    pages_dir_arg = pages_dir if pages_dir.exists() else None
    classified = classify_pages(
        pdf_path=pdf_path,
        candidate_pages=candidate_page_nums,
        pages_dir=pages_dir_arg,
        verbose=verbose,
    )

    plan_pages = [p for p in classified if p.get("type") in PLAN_TYPES]
    image_pages = [p for p in classified if p.get("type") in IMAGE_TYPES]
    data_pages = [p for p in classified if p.get("type") in DATA_TYPES]
    other_pages = [p for p in classified if p.get("type") not in PLAN_TYPES | IMAGE_TYPES | DATA_TYPES]

    elapsed2 = time.time() - t2
    log["steps"]["vision_screen"] = {
        "time_s": round(elapsed2, 2),
        "classified": len(classified),
        "plan_pages": len(plan_pages),
        "image_pages": len(image_pages),
        "data_pages": len(data_pages),
        "plan_page_nums": [p["page"] for p in plan_pages],
        "image_page_nums": [p["page"] for p in image_pages + data_pages],
        "other_page_nums": [p["page"] for p in other_pages],
    }

    print(f"\n  Classification in {elapsed2:.1f}s:")
    print(f"  Plan pages:   {[p['page'] for p in plan_pages]}")
    print(f"  Image pages:  {[p['page'] for p in image_pages]} ({[p.get('type') for p in image_pages]})")
    print(f"  Data pages:   {[p['page'] for p in data_pages]} ({[p.get('type') for p in data_pages]})")
    if other_pages:
        print(f"  Other (skip): {[p['page'] for p in other_pages]}")

    if not plan_pages and not image_pages and not data_pages:
        print("  Nothing extractable found.")
        return {"error": "no_extractable_pages", "log": log}

    # ------------------------------------------------------------------
    # Step 3: Plan + Image extraction (parallel)
    # ------------------------------------------------------------------
    from concurrent.futures import ThreadPoolExecutor

    all_image_pages = image_pages + data_pages

    _progress(2, "Classifying pages", f"Found {len(plan_pages)} plans, {len(all_image_pages)} images")
    _progress(3, "Extracting details", f"{len(plan_pages)} plans + {len(all_image_pages)} images in parallel")
    print(f"\nStep 3: Extracting plans ({len(plan_pages)} pages) + images ({len(all_image_pages)} pages) in parallel (DPI={dpi})...")
    t3 = time.time()

    plan_results = []
    image_results = []

    def _run_plans():
        if not plan_pages:
            return []
        return extract_plan_labels(
            pdf_path=pdf_path,
            confirmed_pages=plan_pages,
            output_dir=out_dir,
            page_texts=page_texts,
            dpi=dpi,
            verbose=verbose,
        )

    def _run_images():
        if not all_image_pages:
            return []
        return extract_image_labels(
            pdf_path=pdf_path,
            confirmed_pages=all_image_pages,
            output_dir=out_dir,
            page_texts=page_texts,
            dpi=dpi,
            verbose=verbose,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        plan_future = executor.submit(_run_plans)
        image_future = executor.submit(_run_images)
        plan_results = plan_future.result()
        image_results = image_future.result()

    total_extracted = len([r for r in plan_results if r.get("file_path")]) + len([r for r in image_results if r.get("file_path")])
    _progress(3, "Extracting details", f"Done — {total_extracted} images extracted")
    elapsed3 = time.time() - t3

    log["steps"]["plan_extraction"] = {
        "time_s": round(elapsed3, 2),
        "pages_processed": len(plan_pages),
        "images_saved": len([r for r in plan_results if r.get("file_path")]),
    }
    log["steps"]["image_extraction"] = {
        "time_s": round(elapsed3, 2),
        "pages_processed": len(all_image_pages),
        "images_saved": len([r for r in image_results if r.get("file_path")]),
        }

    # ------------------------------------------------------------------
    # Save plan_data.json
    # ------------------------------------------------------------------
    total_time = time.time() - t_pipeline_start
    log["total_time_s"] = round(total_time, 2)

    unit_plans = [r for r in plan_results if r.get("plan_type") == "unit_plan"]
    master_plans = [r for r in plan_results if r.get("plan_type") == "master_plan"]
    floor_plans = [r for r in plan_results if r.get("plan_type") == "floor_plan"]

    amenity_images = [r for r in image_results if r.get("image_type") == "amenity_image"]
    exterior_images = [r for r in image_results if r.get("image_type") == "exterior_image"]
    interior_images = [r for r in image_results if r.get("image_type") == "interior_image"]
    location_images = [r for r in image_results if r.get("image_type") == "location_map"]
    lifestyle_images = [r for r in image_results if r.get("image_type") == "lifestyle_image"]
    specification_tables = [r for r in image_results if r.get("image_type") == "specification_table"]

    summary = {
        "pdf_source": pdf_stem,
        "total_pages": screen_result["total_pages"],
        "pages_scanned": candidate_page_nums,
        "confirmed_plan_pages": [p["page"] for p in plan_pages],
        "confirmed_image_pages": [p["page"] for p in all_image_pages],
        # Plan arrays (existing structure unchanged)
        "unit_plans": unit_plans,
        "master_plans": master_plans,
        "floor_plans": floor_plans,
        # New image arrays
        "amenity_images": amenity_images,
        "exterior_images": exterior_images,
        "interior_images": interior_images,
        "location_images": location_images,
        "lifestyle_images": lifestyle_images,
        "specification_tables": specification_tables,
        # Totals
        "total_plans_found": len(plan_results),
        "total_images_found": len(image_results),
        "pipeline_log": log,
    }

    # Write timing stats for dynamic time display in web UI
    _append_timing_stats(
        total_time_s=total_time,
        total_pages=screen_result["total_pages"],
    )

    plan_data_file = out_dir / "plan_data.json"
    with open(plan_data_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"Done!")
    print(f"{'='*60}")
    print(f"  Unit plans:      {len(unit_plans)}")
    print(f"  Master plans:    {len(master_plans)}")
    print(f"  Floor plans:     {len(floor_plans)}")
    print(f"  Amenity images:  {len(amenity_images)}")
    print(f"  Exterior images: {len(exterior_images)}")
    print(f"  Interior images: {len(interior_images)}")
    print(f"  Location maps:   {len(location_images)}")
    print(f"  Specs/tables:    {len(specification_tables)}")
    print(f"  Lifestyle:       {len(lifestyle_images)}")
    print(f"  Total plans:     {len(plan_results)}")
    print(f"  Total images:    {len(image_results)}")
    print(f"  Grand total:     {len(plan_results) + len(image_results)}")
    print(f"  Time:            {total_time:.1f}s")
    print(f"  Output:          {out_dir}/")
    print(f"  plan_data:       {plan_data_file}")
    print(f"{'='*60}\n")

    return summary


def _append_timing_stats(total_time_s: float, total_pages: int):
    """Append timing data to web app's timing_stats.json for dynamic estimates."""
    import datetime
    stats_file = Path("/root/projects/prop-capture-web/timing_stats.json")
    try:
        if stats_file.exists():
            with open(stats_file, "r") as f:
                stats = json.load(f)
        else:
            stats = {"runs": []}

        stats["runs"].append({
            "pdf_pages": total_pages,
            "total_time_s": round(total_time_s, 1),
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        })

        # Keep only last 20 runs
        stats["runs"] = stats["runs"][-20:]

        with open(stats_file, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        # Non-fatal — don't crash the pipeline for timing stats
        print(f"  (timing stats write failed: {e})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="prop-capture: extract unit/master/floor plans from real estate PDFs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline.py input/brochure.pdf
  python pipeline.py input/brochure.pdf --dpi 200
  python pipeline.py input/brochure.pdf --output-dir /tmp/results
        """,
    )
    parser.add_argument("pdf", help="Path to input PDF file")
    parser.add_argument("--output-dir", default="output", help="Output base directory (default: output)")
    parser.add_argument("--dpi", type=int, default=150, help="DPI for plan image renders (default: 150)")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = parser.parse_args()

    if not args.pdf.lower().endswith(".pdf"):
        print("Error: input must be a PDF file.")
        sys.exit(1)

    pdf_file = Path(args.pdf)
    if not pdf_file.exists():
        print(f"Error: file not found: {args.pdf}")
        sys.exit(1)

    run_pipeline(
        pdf_path=args.pdf,
        output_base=args.output_dir,
        dpi=args.dpi,
        verbose=not args.quiet,
    )
