# WebsiteLead Hunter — Automated Client Acquisition Platform

An automated client acquisition platform designed for freelance web developers to discover local businesses, audit their digital presence in real time, calculate transparent opportunity scores, and generate tailored sales pitches (Email, WhatsApp, Project Scope & INR Price Estimates).

---

## Key Highlights & Architectural Principles

- **Real Business Discovery**: Queries live OpenStreetMap Overpass API (with automatic fallback mirrors) and Nominatim geocoding. No fake data generators or static mocks.
- **Automated Website Auditing**: Non-invasive HTTP checks, redirect inspection, SSL verification, title/viewport analysis, contact detection, and mobile usability heuristics. Clearly distinguishes **[Verified]** findings from **[Inferred]** opportunities.
- **Transparent Lead Scoring (0–100)**: Documented score factors with evidence tracking (absence of website, broken URLs, missing CTAs, active business proofs, contactability).
- **Personalized Sales Packages**: Deterministic template engine (plus optional Gemini API integration) that drafts tailored first-contact emails, concise WhatsApp messages, follow-up drafts, category-specific feature recommendations, and realistic freelance INR price ranges.
- **Persistent Job Queue & State Recovery**: SQLite-backed job queue with start, pause, resume, cancel controls, live progress tracking, and server crash/restart recovery.
- **Kanban Sales Pipeline & Table**: Dual-view interface with drag-free stage advancement (`New` → `Qualified` → `Contacted` → `Replied` → `Meeting` → `Won` → `Lost`), notes activity log, search, filtering, and 1-click CSV export.

---

## Tech Stack

| Layer | Technologies |
|---|---|
| **Frontend** | React 19, TypeScript, Tailwind CSS v4, Lucide Icons, Vite |
| **Backend** | Python 3.13, FastAPI, Uvicorn, HTTPX, BeautifulSoup4, Pydantic v2 |
| **Database** | SQLite (WAL mode, Foreign Keys enabled) |
| **Data Sources** | OpenStreetMap Overpass API, Nominatim Geocoding |
| **AI (Optional)**| Google GenAI SDK (`google-genai`) with Gemini 2.5 Flash |

---

## Directory Structure

```
websitelead-hunter/
├── backend/
│   ├── app/
│   │   ├── api/             # FastAPI routers (jobs, leads, export, discovery)
│   │   ├── audit/           # WebsiteAuditor & HTML heuristics
│   │   ├── discovery/       # Overpass & Nominatim providers with provenance
│   │   ├── pitch/           # Deterministic templates & optional Gemini LLM provider
│   │   ├── queue/           # Persistent background JobWorker with pause/resume/cancel
│   │   ├── scoring/         # Transparent 0-100 LeadScorer
│   │   ├── database.py      # SQLite connection & schema initialization
│   │   ├── schemas.py       # Pydantic v2 request/response models
│   │   └── main.py          # FastAPI application entry point
│   ├── tests/               # 25 automated unit & integration tests
│   ├── requirements.txt     # Python dependencies
│   └── .env.example         # Environment template with safe defaults
├── frontend/
│   ├── src/
│   │   ├── components/      # UI components (DiscoveryPanel, Kanban, LeadTable, Modal, etc.)
│   │   ├── services/        # Type-safe API client
│   │   ├── types/           # TypeScript domain definitions
│   │   ├── App.tsx          # Main dashboard coordinator
│   │   └── index.css        # Tailwind CSS styles
│   ├── package.json
│   └── vite.config.ts       # Vite proxy configuration
├── run_backend.py           # Backend startup runner
├── README.md                # Project documentation
└── .gitignore
```

---

## Quickstart Guide

### 1. Prerequisites
- **Python 3.10+** (tested on Python 3.13)
- **Node.js 18+** (tested on Node v24)
- **npm**

### 2. Backend Setup
```bash
# Navigate to backend directory
cd backend

# (Optional) create virtual environment
python -m venv venv
# Windows:
.\venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy environment variables
copy .env.example .env

# Run backend server
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
The backend API documentation is available at `http://127.0.0.1:8000/docs`.

### 3. Frontend Setup
```bash
# In a new terminal, navigate to frontend
cd frontend

# Install dependencies
npm install

# Start Vite dev server
npm run dev
```
Open `http://localhost:5173` in your browser.

---

## Verification & Test Suite

All 25 automated tests pass with real live queries and comprehensive mocks:

```bash
cd backend
$env:PYTHONPATH="."
pytest -v
```

### Verified Gates Summary:
- **Milestone A (Inspection & Scaffolding)**: Inspected system environment, established clean repository structure, initialized Git.
- **Milestone B (Real Discovery)**: Live geocoding via Nominatim, multi-endpoint Overpass failover (`overpass.openstreetmap.fr`, `overpass-api.de`, etc.), normalized deduplication, and field-level provenance tracking.
- **Milestone C (Website Auditing)**: Tested live reachable sites, unreachable/broken URLs, and missing websites. Verified heuristic detection for mobile viewports, CTAs, SSL, and menu tags.
- **Milestone D (Scoring, Pitches & Persistence)**: 0-100 score breakdown, deterministic sales packages with INR benchmarks, and SQLite ACID persistence.
- **Milestone E (Dashboard, Kanban & Export)**: Full React TypeScript dashboard, interactive Kanban pipeline, detailed inspection modal, and CSV streaming export.
- **Milestone F (Resilience & Security)**: Tested Nominatim rate-limiting compliance (1 req/sec), crash recovery (in-flight running jobs safely transitioned to paused upon reboot), job pause/resume/cancel controls, and credential security.

---

## Safety, Outreach & Privacy Compliance

- **No Bulk Automated Blasting**: The platform prepares copyable drafts and WhatsApp Web links for manual review. It never performs automated unsolicited mass messaging.
- **OpenStreetMap Usage**: Complies with Nominatim and Overpass acceptable use policies: custom User-Agent headers, rate limiting, and explicit disclaimers on voluntary community coverage.
- **No Data Fabrication**: Messages and scores are grounded solely in verified findings. Missing data is treated neutrally and never claimed as proof of inactivity.
- **Data Deletion**: Complete support for single-lead deletion and bulk database clearing.
