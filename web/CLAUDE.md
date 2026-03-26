# prop-capture-web

FastAPI web interface for the prop-capture pipeline. Upload a real estate PDF brochure and view extracted unit plans, master plans, floor plans, and property images in the browser.

## Live URL

https://prop-capture.konzult.in

## How to Run

```bash
cd /root/projects/prop-capture-web
pm2 start start.sh --name prop-capture-web --interpreter bash
pm2 restart prop-capture-web   # Restart
pm2 logs prop-capture-web      # View logs
```

Service runs on **port 5050**, proxied via Caddy at prop-capture.konzult.in.

## Architecture

```
User uploads PDF
      |
POST /upload  → saves to uploads/<safe_name>-<job_id[:8]>.pdf
      |
Background thread → calls run_pipeline() from ../prop-capture/pipeline.py
      |                 writes output to output/<job_id>/<pdf_stem>/
      |
GET /status/<job_id>  → polls every 3s, redirects when done
      |
GET /results/<job_id> → renders results.html with images + JSON
```

## Pages

| URL | Template | Purpose |
|-----|----------|---------|
| `/` | `index.html` | PDF upload form |
| `/status/<job_id>` | `processing.html` | Auto-polling wait page |
| `/results/<job_id>` | `results.html` | Extracted images + raw JSON |
| `/output/<job_id>/...` | StaticFiles | Serve extracted images |

## Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI app — all routes, job state, pipeline integration |
| `templates/index.html` | Upload form |
| `templates/processing.html` | Processing wait page (auto-refresh every 3s) |
| `templates/results.html` | Results display: image grids + JSON viewer |
| `templates/error.html` | Error page |
| `static/style.css` | Minimal CSS (~280 lines) |
| `requirements.txt` | Python dependencies |
| `start.sh` | PM2 startup script |
| `uploads/` | Temporary uploaded PDFs (gitignored) |
| `output/` | Pipeline output per job (gitignored) |

## Job State

Jobs are stored in a module-level dict (`jobs = {}`). State is lost on restart — by design for MVP. Future: persist to SQLite.

```python
jobs[job_id] = {
    "status": "processing" | "done" | "error",
    "pdf_name": "Godrej-Skyline-Brochure.pdf",
    "output_dir": "output/<job_id>/Godrej-Skyline-Brochure/",
    "result": { ...plan_data dict... },   # populated when done
    "error": "..."                         # populated on error
}
```

## Pipeline Integration

```python
import sys
sys.path.insert(0, '/root/projects/prop-capture')
from pipeline import run_pipeline

result = run_pipeline(pdf_path, output_base=f"output/{job_id}")
```

The pipeline writes images to `output/<job_id>/<pdf_stem>/unit-plan/`, `master-plan/`, etc.
Images are served at `/output/<job_id>/<pdf_stem>/unit-plan/filename.jpg`.

## Auth

Groq API key loaded from `/root/.secrets.env` via the pipeline. No additional config needed here.

## Dependencies

```
fastapi>=0.100.0
uvicorn>=0.23.0
python-multipart>=0.0.6
jinja2>=3.1.0
pymupdf>=1.23.0
groq>=0.9.0
python-dotenv>=1.0.0
```

## Deployment

- **Process manager**: PM2 (`prop-capture-web`)
- **Reverse proxy**: Caddy (`prop-capture.konzult.in → localhost:5050`)
- **Port**: 5050 (open in iptables INPUT chain)

## Caddy Config (in /etc/caddy/Caddyfile)

```
prop-capture.konzult.in {
    reverse_proxy localhost:5050
    encode gzip
}
```

## Known Limitations (MVP)

- Job state lost on restart (in-memory only)
- No file size limit enforced (large PDFs may take 60-90s)
- No authentication — anyone with the URL can upload
- Single-threaded pipeline execution per job
