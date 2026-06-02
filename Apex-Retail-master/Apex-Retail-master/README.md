# Apex Retail · Store Intelligence System

> **Real-time analytics for brick-and-mortar stores, computed from raw CCTV.**

Turns CCTV footage into actionable business metrics — unique visitors,
conversion rate, funnel drop-off, queue dynamics, and anomaly alerts —
with a live dashboard that updates as people walk the floor.

```
  ┌────────────┐    ┌──────────────┐    ┌──────────┐    ┌──────────────┐
  │ CCTV clip  │───▶│ Detection    │───▶│ Events   │───▶│ Intelligence │
  │ (or synth) │    │ pipeline     │    │ stream   │    │ API + DB     │
  └────────────┘    │ (YOLOv8 +    │    │ HTTP     │    │  + dashboard │
                    │  ByteTrack + │    │ batches  │    │  (FastAPI +  │
                    │  Re-ID +     │    │          │    │   SQLite/PG) │
                    │  staff cls + │    │          │    └──────────────┘
                    │  group det)  │    │          │           │
                    └──────────────┘    └──────────┘           ▼
                                                    Live WebSocket dashboard

  North-star metric: unique_purchasers ÷ unique_visitors  (per store, per window)
```

---

## 📚 Documentation

| Document | Audience | What's inside |
|---|---|---|
| **[USAGE.md](USAGE.md)** | Operators / reviewers | Every command, every endpoint, every flag |
| **[TECHNICAL_OVERVIEW.md](TECHNICAL_OVERVIEW.md)** | Engineers | Architecture deep-dive, data model, design choices |
| **[BUSINESS_OVERVIEW.md](BUSINESS_OVERVIEW.md)** | Execs / clients | Problem, ROI, real-world impact, use-case stories |
| **[docs/DESIGN.md](docs/DESIGN.md)** | Engineers | Layer map, contracts, observability |
| **[docs/CHOICES.md](docs/CHOICES.md)** | Reviewers | 5 reasoned engineering decisions (~3,500 words) |
| **[BATCH_TRACKER.md](BATCH_TRACKER.md)** | Project leads | Per-batch deliverables and rationale |

---

## ⚡ Quick Start (zero infra — no Docker, no dataset)

**Prerequisites:** Python 3.10+ and the project's `.venv/`.

```powershell
cd "C:\path\to\Apex_Retail"
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
alembic upgrade head           # creates ./data/apex.db (SQLite)
uvicorn app.main:app --reload
```

In a second terminal:

```powershell
.venv\Scripts\Activate.ps1
python -m scripts.replay_events --speed 3
```

Open <http://localhost:8000/dashboard?store=ST1008> — you'll see KPIs, the
funnel, the per-zone heatmap, anomalies, and per-store health updating live.

> Want to run it on **real CCTV** instead of synthetic? Add
> `pip install -r requirements-pipeline.txt` (~250 MB — torch + cv2 +
> ultralytics) and use `python -m pipeline.run --detector yolo --video <path>`.
> Full guide in [USAGE.md](USAGE.md#real-cctv-mode).

---

## 🧪 Tests

```powershell
pytest -v                                       # ~165 tests, ≤ 6s
coverage run -m pytest && coverage report       # ≥ 70%
```

---

## 🏛️ Tech Stack

| Layer | Tools | Why |
|---|---|---|
| **API** | FastAPI · Pydantic v2 · Uvicorn | Async, OpenAPI free, type-safe DTOs |
| **DB** | SQLAlchemy 2.0 · Alembic · SQLite (dev) / Postgres (prod) | One ORM, two backends, idempotent migrations |
| **Detection** | YOLOv8n · ByteTrack (Ultralytics) · OpenCV | Best speed/accuracy on 1080p15fps CPU |
| **Re-ID** | Cosine on 64-D RGB-tile descriptors (pure Python) | No torchreid, no extra weights |
| **Staff classifier** | Color-histogram heuristic + VLM-stub | Pragmatic for Purplle's dark-uniform reality |
| **Observability** | structlog (JSON) · per-request `trace_id` · per-store STALE_FEED | Fits any log aggregator |
| **Dashboard** | Vanilla JS + Chart.js (CDN) · WebSocket pub-sub | Zero build step, instant load |
| **Tests** | pytest · pytest-asyncio · httpx mock transport | Fast, hermetic, no infra in CI |

---

## 🗂️ Project Structure

```
apex_retail/
├── app/                          # Intelligence API (Clean Architecture)
│   ├── api/                      # FastAPI routers
│   │   ├── routes_health.py      # /health (per-store stale-feed)
│   │   ├── routes_events.py      # POST /events/ingest
│   │   ├── routes_stores.py      # /stores/{id}/{metrics,funnel,heatmap,anomalies}
│   │   └── routes_dashboard.py   # /dashboard + /ws/stores/{id}
│   ├── core/                     # config, logging, middleware, errors
│   ├── domain/                   # entities + pure rules
│   ├── services/                 # use-cases (ingestion, sessions, anomalies, …)
│   ├── infra/                    # DB, repos, loaders
│   ├── schemas/                  # Pydantic DTOs
│   ├── static/dashboard.html     # Vanilla JS + Chart.js dashboard
│   └── main.py                   # FastAPI app factory
├── pipeline/                     # Detection pipeline
│   ├── detector.py · event_builder.py · synthetic_backend.py · yolo_backend.py
│   ├── reid.py · staff_classifier.py · emitter.py · run.py
│   ├── layouts/brigade_road.py   # Real Purplle Brigade Road, Bangalore (8 zones)
│   └── scenarios/brigade.py      # 5-archetype 90-second demo scenario
├── tests/                        # pytest (~165 tests, ≥ 70% coverage)
├── scripts/                      # CLI utilities (load_pos, load_layout, replay_events)
├── docs/
│   ├── DESIGN.md                 # architecture + AI-assisted decisions
│   └── CHOICES.md                # 5 decisions, fully reasoned
├── data/                         # CCTV clips, POS, layout (gitignored)
├── docker/Dockerfile.api · docker/entrypoint.sh
├── docker-compose.yml
├── alembic.ini · migrations/
├── requirements.txt              # API + tests (light)
├── requirements-pipeline.txt     # Optional: torch + cv2 for real-video YOLO
├── pyproject.toml
└── .env.example
```

---

## 🌐 API at a Glance

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + per-store stale-feed status |
| `POST` | `/events/ingest` | Idempotent batch ingest (≤ 500 events) |
| `GET` | `/stores/{id}/metrics` | Conversion, visitors, dwell, billing stats |
| `GET` | `/stores/{id}/Metrics` | Capital-M alias (rubric compatibility) |
| `GET` | `/stores/{id}/funnel` | Entry → Browse → Billing → Purchase |
| `GET` | `/stores/{id}/heatmap` | Per-zone visit share + dwell + intensity |
| `GET` | `/stores/{id}/anomalies` | Queue spikes / conversion drops / dead zones |
| `GET` | `/dashboard?store={id}` | Live web UI |
| `WS` | `/ws/stores/{id}` | Pushes `store_changed` pings to the dashboard |

OpenAPI spec: <http://localhost:8000/docs> while the API is running.

---

## 🤝 Contributing

1. Branch from `main`, work in `app/` or `pipeline/`.
2. Add a test in `tests/` for every behaviour change.
3. Run `pytest -v && coverage run -m pytest && coverage report` (must stay ≥ 70%).
4. Update `docs/DESIGN.md` if the architecture changes; `docs/CHOICES.md`
   if a meaningful trade-off is made.
5. Open a PR; CI runs the same `pytest` matrix.

Code style: `ruff` is configured in `pyproject.toml` (line-length 100, target
3.10+). Run `ruff check .` before pushing.

---

## 📄 License & Data

Code: internal / proprietary (no public license declared). Challenge
artefacts (CCTV, POS, layout) live under `data/` and are **gitignored** — the
repository never redistributes them.
