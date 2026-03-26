# PropCapture

Extracts unit plans, master plans, floor plans, and property images from real estate PDF brochures using Groq vision AI.

## How it works
1. Upload a PDF brochure at [prop-capture.konzult.in](https://prop-capture.konzult.in)
2. Pipeline scans all pages using text + image heuristics (zero API cost)
3. AI classifies each page: unit plan, master plan, floor plan, amenity, exterior, interior, location, spec table
4. Extracts labeled images with structured metadata (BHK type, carpet area, tower, etc.)
5. Results displayed as image gallery + downloadable JSON

## Stack
- **PDF processing:** PyMuPDF (text extraction, page rendering)
- **AI vision:** Groq llama-4-scout (classification + metadata extraction)
- **Web app:** FastAPI + Jinja2
- **Cost:** ~$0.01 per PDF run

## Setup
```bash
pip install -r requirements.txt
```

Set your Groq API key in `.env` (see `.env.example`):
```
GROQ_API_KEY=your_groq_api_key_here
```

## Run
```bash
# Web app
cd web && pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 5050

# CLI
python pipeline.py path/to/brochure.pdf
```

## Structure
```
pipeline.py             Main orchestrator
screen.py               Step 1: page scanning (zero API)
vision_screen.py        Step 2: AI classification
extract_plans.py        Step 3a: plan metadata extraction
extract_images.py       Step 3b: image metadata extraction
web/                    FastAPI web application
```
