# PropCapture

Extracts unit plans, master plans, and property images from real estate PDF brochures using Groq vision AI.

## How it works
1. Upload a PDF brochure at [https://prop-capture.konzult.in](https://prop-capture.konzult.in)
2. Pipeline screens all pages using text matching + Groq vision
3. Extracts labeled images: unit plans (3BHK/4BHK with carpet areas), master plans, property photos
4. Results displayed as structured JSON + image gallery

## Stack
- PyMuPDF for PDF rendering
- Groq llama-4-scout for vision classification and extraction
- FastAPI + Jinja2 for web interface
- ~$0.01 per PDF, ~50 seconds per 50-page brochure

## Run locally
See `pipeline/` and `web/` for setup instructions.

## Structure
```
prop-capture/
├── pipeline/   ← PDF screening + Groq vision extraction scripts
└── web/        ← FastAPI web interface (upload, job queue, results)
```
