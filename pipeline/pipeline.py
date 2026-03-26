"""
pipeline.py — prop-capture v2: extract unit/master/floor plans from real estate PDFs.

Usage:
    python pipeline.py input/brochure.pdf
    python pipeline.py input/brochure.pdf --dpi 200
    python pipeline.py input/brochure.pdf --output-dir /tmp/output

Pipeline:
    Step 1: Text screening (screen.py)       — 0 API calls, ~0.1s
    Step 2: Vision screening (vision_screen.py) — Groq llama-4-scout, ~5-10s
    Step 3: Plan extraction (extract_plans.py)  — Groq llama-4-scout, ~20-40s

Output:
    output/<pdf_name>/
    ├── unit-plan/      ← individual apartment floor plans
    ├── master-plan/    ← site/project layout maps
    ├── floor-plan/     ← whole building floors (multiple units)
    ├── pages/          ← reference page renders (if pre-rendered)
    └── plan_data.json  ← structured metadata for all plans found
"""

import argparse
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv("/root/.secrets.env")

from screen import screen_pages
from vision_screen import classify_pages, PLAN_TYPES
from extract_plans import extract_plan_labels


def run_pipeline(
    pdf_path: str,
    output_base: str = "output",
    dpi: int = 150,
    verbose: bool = True,
) -> dict:
    """
    Full v2 pipeline: PDF -> text screen -> vision screen -> plan extraction.

    Args:
        pdf_path:    Path to the input PDF file
        output_base: Base directory for output (default: "output")
        dpi:         DPI for high-res plan renders in step 3 (default: 150)
        verbose:     Print progress to stdout

    Returns:
        Summary dict with plan counts, file paths, and pipeline timing.
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
    print(f"prop-capture pipeline")
    print(f"Input:  {pdf_path}")
    print(f"Output: {out_dir}")
    print(f"{'='*60}\n")

    # ------------------------------------------------------------------
    # Step 1: Text screening
    # ------------------------------------------------------------------
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
        "candidate_pages": candidate_page_nums,
    }

    print(f"  {screen_result['total_pages']} pages scanned in {elapsed1:.1f}s")
    print(f"  {len(candidate_page_nums)} candidate pages: {candidate_page_nums}")

    if not candidate_page_nums:
        print("  No candidate pages found — nothing to process.")
        return {"error": "no_candidates", "log": log}

    # ------------------------------------------------------------------
    # Step 2: Vision screening (Groq)
    # ------------------------------------------------------------------
    print("\nStep 2/3: Vision screening with Groq...")
    t2 = time.time()

    pages_dir_arg = pages_dir if pages_dir.exists() else None
    classified = classify_pages(
        pdf_path=pdf_path,
        candidate_pages=candidate_page_nums,
        pages_dir=pages_dir_arg,
        verbose=verbose,
    )

    plan_pages = [p for p in classified if p.get("type") in PLAN_TYPES]
    non_plan = [p for p in classified if p.get("type") not in PLAN_TYPES]

    elapsed2 = time.time() - t2
    log["steps"]["vision_screen"] = {
        "time_s": round(elapsed2, 2),
        "classified": len(classified),
        "plan_pages": len(plan_pages),
        "plan_page_nums": [p["page"] for p in plan_pages],
        "non_plan_page_nums": [p["page"] for p in non_plan],
    }

    print(f"\n  {len(plan_pages)}/{len(candidate_page_nums)} confirmed plan pages in {elapsed2:.1f}s")
    print(f"  Plan pages: {[p['page'] for p in plan_pages]}")
    if non_plan:
        print(f"  Filtered out: {[p['page'] for p in non_plan]} ({[p.get('type') for p in non_plan]})")

    if not plan_pages:
        print("  No plan pages confirmed — nothing to extract.")
        return {"error": "no_plan_pages", "log": log}

    # ------------------------------------------------------------------
    # Step 3: Plan extraction (Groq)
    # ------------------------------------------------------------------
    print(f"\nStep 3/3: Extracting plan labels from {len(plan_pages)} pages (DPI={dpi})...")
    t3 = time.time()

    results = extract_plan_labels(
        pdf_path=pdf_path,
        confirmed_pages=plan_pages,
        output_dir=out_dir,
        page_texts=page_texts,
        dpi=dpi,
        verbose=verbose,
    )

    elapsed3 = time.time() - t3
    log["steps"]["plan_extraction"] = {
        "time_s": round(elapsed3, 2),
        "pages_processed": len(plan_pages),
        "images_saved": len([r for r in results if r.get("file_path")]),
    }

    # ------------------------------------------------------------------
    # Save plan_data.json
    # ------------------------------------------------------------------
    total_time = time.time() - t_pipeline_start
    log["total_time_s"] = round(total_time, 2)

    unit_plans = [r for r in results if r.get("plan_type") == "unit_plan"]
    master_plans = [r for r in results if r.get("plan_type") == "master_plan"]
    floor_plans = [r for r in results if r.get("plan_type") == "floor_plan"]

    summary = {
        "pdf_source": pdf_stem,
        "total_pages": screen_result["total_pages"],
        "candidate_pages": candidate_page_nums,
        "confirmed_plan_pages": [p["page"] for p in plan_pages],
        "unit_plans": unit_plans,
        "master_plans": master_plans,
        "floor_plans": floor_plans,
        "total_plans_found": len(results),
        "pipeline_log": log,
    }

    plan_data_file = out_dir / "plan_data.json"
    with open(plan_data_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    print(f"Done!")
    print(f"{'='*60}")
    print(f"  Unit plans:   {len(unit_plans)}")
    print(f"  Master plans: {len(master_plans)}")
    print(f"  Floor plans:  {len(floor_plans)}")
    print(f"  Total:        {len(results)}")
    print(f"  Time:         {total_time:.1f}s")
    print(f"  Output:       {out_dir}/")
    print(f"  plan_data:    {plan_data_file}")
    print(f"{'='*60}\n")

    return summary


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
