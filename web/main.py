"""
main.py — PropCapture web app
FastAPI app that accepts PDF uploads, runs the prop-capture pipeline,
and displays extracted plans and all property images (amenity, exterior, interior, etc.).
"""

import sys
import os
import uuid
import json
import time
import threading
import traceback
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Bootstrap — add pipeline to sys.path and load env
# ---------------------------------------------------------------------------
sys.path.insert(0, "/root/projects/prop-capture")

from dotenv import load_dotenv
load_dotenv("/root/.secrets.env")

from pipeline import run_pipeline

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
UPLOADS_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"
TIMING_STATS_FILE = BASE_DIR / "timing_stats.json"
UPLOADS_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


def _get_timing_estimate() -> str:
    """Read timing_stats.json and compute a dynamic time estimate string."""
    try:
        if TIMING_STATS_FILE.exists():
            with open(TIMING_STATS_FILE) as f:
                stats = json.load(f)
            runs = stats.get("runs", [])
            if runs:
                # Use last 10 runs
                recent = runs[-10:]
                avg = sum(r["total_time_s"] for r in recent) / len(recent)
                lo = int(avg * 0.7)
                hi = int(avg * 1.3)
                return f"Typically {lo}–{hi} seconds depending on PDF size"
    except Exception:
        pass
    return "Typically 60–120 seconds"

app = FastAPI(title="PropCapture")

app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ---------------------------------------------------------------------------
# In-memory job store (MVP — no DB needed)
# ---------------------------------------------------------------------------
jobs: dict = {}
# job_id -> {
#   status: "processing" | "done" | "error",
#   pdf_name: str,
#   result: dict | None,
#   error: str | None,
#   output_dir: str | None,
#   started_at: float,
#   finished_at: float | None,
# }


# ---------------------------------------------------------------------------
# Background pipeline runner
# ---------------------------------------------------------------------------
def run_pipeline_thread(job_id: str, pdf_path: str, pdf_name: str):
    """Run the pipeline in a background thread and update the jobs dict."""
    try:
        # output_base is the web app's output/<job_id>/ directory.
        # Pipeline writes to output_base/<pdf_stem>/ — the pdf_stem is derived from
        # the saved upload filename.  We need to know pdf_stem to build correct URLs.
        pdf_stem = Path(pdf_path).stem
        output_base = str(OUTPUT_DIR / job_id)

        result = run_pipeline(pdf_path=pdf_path, output_base=output_base)

        # Fix file_path values so they become web-accessible URLs.
        # Pipeline returns absolute paths like:
        #   /root/projects/prop-capture-web/output/<job_id>/<pdf_stem>/unit-plan/foo.jpg
        # We want URL:  /output/<job_id>/<pdf_stem>/unit-plan/foo.jpg

        job_out_abs = OUTPUT_DIR / job_id / pdf_stem  # absolute path to job output

        for plan_list in (
            "unit_plans", "master_plans", "floor_plans",
            "amenity_images", "exterior_images", "interior_images",
            "location_images", "lifestyle_images", "specification_tables",
        ):
            for plan in result.get(plan_list, []):
                if plan.get("file_path"):
                    fp = Path(plan["file_path"])
                    # Make relative to OUTPUT_DIR (which is mounted at /output)
                    try:
                        rel = fp.relative_to(OUTPUT_DIR)
                        plan["file_path"] = f"/output/{rel}"
                    except ValueError:
                        # Fallback: extract last 2 path components (subdir/filename)
                        # and place under /output/<job_id>/<pdf_stem>/
                        parts = fp.parts
                        plan["file_path"] = f"/output/{job_id}/{pdf_stem}/{parts[-2]}/{parts[-1]}"

        # Save updated plan_data.json with corrected file_paths
        plan_data_file = job_out_abs / "plan_data.json"
        if plan_data_file.parent.exists():
            with open(plan_data_file, "w") as f:
                json.dump(result, f, indent=2, ensure_ascii=False)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = result
        jobs[job_id]["output_dir"] = str(OUTPUT_DIR / job_id)
        jobs[job_id]["finished_at"] = time.time()

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["error_traceback"] = traceback.format_exc()
        jobs[job_id]["finished_at"] = time.time()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    # Validate file type
    if not file.filename.lower().endswith(".pdf"):
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Please upload a PDF file."},
            status_code=400,
        )

    job_id = str(uuid.uuid4())
    pdf_name = file.filename

    # Save uploaded file preserving the original stem so pipeline output paths are meaningful.
    # Sanitize filename: keep alphanumeric, hyphens, underscores, dots only.
    import re as _re
    safe_stem = _re.sub(r"[^a-zA-Z0-9_\-]", "-", Path(pdf_name).stem)[:60]
    upload_path = UPLOADS_DIR / f"{safe_stem}-{job_id[:8]}.pdf"
    content = await file.read()
    with open(upload_path, "wb") as f:
        f.write(content)

    # Register job
    jobs[job_id] = {
        "status": "processing",
        "pdf_name": pdf_name,
        "result": None,
        "error": None,
        "error_traceback": None,
        "output_dir": None,
        "started_at": time.time(),
        "finished_at": None,
    }

    # Start pipeline in background thread
    thread = threading.Thread(
        target=run_pipeline_thread,
        args=(job_id, str(upload_path), pdf_name),
        daemon=True,
    )
    thread.start()

    return RedirectResponse(url=f"/status/{job_id}", status_code=303)


@app.get("/status/{job_id}", response_class=HTMLResponse)
async def status(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job:
        return HTMLResponse("<h1>Job not found</h1>", status_code=404)

    if job["status"] == "done":
        return RedirectResponse(url=f"/results/{job_id}")

    if job["status"] == "error":
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "job_id": job_id,
                "pdf_name": job["pdf_name"],
                "error": job["error"],
                "error_traceback": job.get("error_traceback", ""),
            },
        )

    # Still processing
    elapsed = round(time.time() - job["started_at"])
    return templates.TemplateResponse(
        "processing.html",
        {
            "request": request,
            "job_id": job_id,
            "pdf_name": job["pdf_name"],
            "elapsed": elapsed,
            "timing_estimate": _get_timing_estimate(),
        },
    )


@app.get("/results/{job_id}", response_class=HTMLResponse)
async def results(request: Request, job_id: str):
    job = jobs.get(job_id)
    if not job:
        return HTMLResponse("<h1>Job not found</h1>", status_code=404)

    if job["status"] == "processing":
        return RedirectResponse(url=f"/status/{job_id}")

    if job["status"] == "error":
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "job_id": job_id,
                "pdf_name": job["pdf_name"],
                "error": job["error"],
                "error_traceback": job.get("error_traceback", ""),
            },
        )

    result = job["result"]
    elapsed = round(job["finished_at"] - job["started_at"])

    # Gather BHK types for filter tabs
    bhk_types = sorted(set(
        p.get("unit_type") for p in result.get("unit_plans", [])
        if p.get("unit_type")
    ))

    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "job_id": job_id,
            "pdf_name": job["pdf_name"],
            "result": result,
            "elapsed": elapsed,
            "bhk_types": bhk_types,
            "json_data": json.dumps(result, indent=2, ensure_ascii=False),
        },
    )


@app.get("/api/status/{job_id}")
async def api_status(job_id: str):
    """JSON endpoint for polling job status."""
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "status": job["status"],
        "pdf_name": job["pdf_name"],
        "elapsed": round(time.time() - job["started_at"]),
    })
