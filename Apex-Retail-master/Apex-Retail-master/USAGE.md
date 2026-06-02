# USAGE.md · Operating the Apex Retail System

> Every command, every endpoint, every flag. If you're doing it for the first
> time, follow Section 1 top-to-bottom — it's the 5-minute happy path.

---

## Table of contents

- [1. First-run setup](#1-first-run-setup)
- [2. Running the pipeline](#2-running-the-pipeline)
- [3. Querying the API](#3-querying-the-api)
- [4. The live dashboard](#4-the-live-dashboard)
- [5. Real CCTV mode](#5-real-cctv-mode)
- [6. Loading POS data](#6-loading-pos-data)
- [7. Health & anomalies](#7-health--anomalies)
- [8. Tests & coverage](#8-tests--coverage)
- [9. Configuration reference](#9-configuration-reference)
- [10. Operational recipes](#10-operational-recipes)
- [11. Troubleshooting](#11-troubleshooting)

---

## 1. First-run setup

### Prerequisites
- **Python ≥ 3.10** (3.11 ideal — bundled in the repo's `.venv/`)
- **No Docker required** for local dev (SQLite default).
  Docker only needed for the rubric's acceptance gate.

### Setup
```powershell
cd "C:\path\to\Apex_Retail"
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
alembic upgrade head        # creates ./data/apex.db (SQLite)
```

### Sanity check
```powershell
pytest -v
```
Expected: ~165 passed, 1 skipped (`test_sample_events.py` waits for an
external dataset). Anything else → see [Troubleshooting](#11-troubleshooting).

### Boot the API
```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Verify:
```powershell
curl http://localhost:8000/health
```
Expected: `{"status":"ok", ..., "stores":[]}` — empty stores list because
the DB is fresh.

OpenAPI: <http://localhost:8000/docs>

---

## 2. Running the pipeline

The pipeline transforms detections into events and POSTs them to
`/events/ingest`. There are two backends:

| Backend | Use when | Extra deps? |
|---|---|---|
| `synthetic` (default) | Demo, tests, CI | None |
| `yolo` | Real video | `pip install -r requirements-pipeline.txt` (~250 MB) |

### 2.1 Synthetic mode (no infra)

**Dry run — print events to stdout, no API call:**
```powershell
python -m pipeline.run --detector synthetic --dry-run > events.jsonl
```

**Live run — POST to your local API:**
```powershell
python -m pipeline.run --detector synthetic --api-url http://localhost:8000
```

**Useful flags:**
```
--store ST1008                 # store_id (default = Brigade Road)
--camera CAM_FLOOR_01          # camera_id
--start-time 2026-04-10T11:25:00Z   # ISO-8601 UTC; aligns events with POS rows
--batch-size 200               # events per /events/ingest call
--min-dwell-s 2.0              # suppress DWELL events below this threshold
--track-timeout-s 5.0          # absent this long → emit EXIT
--no-reid                      # disable Re-ID (REENTRY events) for A/B
```

### 2.2 Real-time-paced replay (for the dashboard demo)

`replay_events` matches wall-clock to event timestamps so the dashboard
updates progressively rather than all at once.

```powershell
python -m scripts.replay_events --speed 1     # real-time
python -m scripts.replay_events --speed 3     # 3× faster
python -m scripts.replay_events --speed 6     # 90s scenario in 15s
```

---

## 3. Querying the API

All `GET` endpoints accept optional `since` and `until` query params (ISO-8601):

```powershell
curl "http://localhost:8000/stores/ST1008/metrics?since=2026-04-10T11:00:00Z&until=2026-04-10T20:00:00Z"
```

### 3.1 `/metrics`
```powershell
curl http://localhost:8000/stores/ST1008/metrics
```
Returns: `unique_visitors`, `staff_count`, `total_purchases`,
`gross_basket_inr`, `conversion_rate`, `avg_dwell_seconds`, billing-queue
stats, and `data_confidence` ("low" when < 20 customer sessions).

The `/Metrics` (capital M) alias returns identical data — kept for
compatibility with the rubric's wording.

### 3.2 `/funnel`
```powershell
curl http://localhost:8000/stores/ST1008/funnel
```
4 stages: `entry → browse → billing → purchase`. Each carries `sessions`
and `drop_off_from_previous` (0..1). Session-based, no double-counting.

### 3.3 `/heatmap`
```powershell
curl http://localhost:8000/stores/ST1008/heatmap
```
Per-zone `visits`, `visit_share`, `avg_dwell_s`, `total_dwell_s`,
`intensity` (normalised 0..1 — busiest zone = 1.0).

### 3.4 `/anomalies`
```powershell
curl http://localhost:8000/stores/ST1008/anomalies
```
Returns sorted (critical → warn → info) array of:
- `queue_spike` — billing depth ≥ 7 for ≥ 60s
- `conversion_drop` — current ≤ 50 % of baseline (with denominator floors)
- `dead_zone` — zone with zero customer visits while others were busy

Each anomaly has `severity`, `message`, `suggested_action`, and a `details` dict.

### 3.5 `POST /events/ingest`
```powershell
$body = @{
  events = @(
    @{
      event_id   = "demo-1"
      event_type = "ENTRY"
      store_id   = "ST1008"
      camera_id  = "CAM_FLOOR_01"
      timestamp  = "2026-04-10T11:25:00+00:00"
      confidence = 0.95
    }
  )
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method POST `
  -Uri http://localhost:8000/events/ingest `
  -ContentType application/json `
  -Body $body
```

Returns 202 with per-event `accepted` / `duplicate` / `rejected` results.
Re-sending the same `event_id` returns `duplicate` — guaranteed idempotent.

Errors: 413 `BATCH_TOO_LARGE`, 422 `VALIDATION_ERROR`, 503
`DEPENDENCY_UNAVAILABLE` (DB unreachable — retry the same batch).

---

## 4. The live dashboard

```
http://localhost:8000/dashboard?store=ST1008
```

What you'll see:
- 4 KPI cards (conversion / unique visitors / purchases / billing queue)
- Funnel bar chart (Entry → Browse → Billing → Purchase)
- Per-zone heatmap (sorted by visit share)
- Anomaly feed (severity-coloured)
- Per-store health table (with STALE_FEED status)

Connection states (top-right pill):
- **live** · WebSocket connected, real-time updates
- **reconnecting…** · server restart or network blip; auto-retries every 2s
- **error** · WebSocket failed; heartbeat polling every 5s still works

Switch stores via URL: `?store=<store_id>`.

---

## 5. Real CCTV mode

```powershell
pip install -r requirements-pipeline.txt
```
Pulls `torch` (~200 MB), `ultralytics`, `opencv-python-headless`. **First run**
of YOLO downloads `yolov8n.pt` (~6 MB) from GitHub.

> **Lilly proxy / corporate network warning:** if `pip install` or the
> weights download fails, see [Troubleshooting · Section 11.4](#114-corporate-proxy-blocks-pip--github).

### 5.1 Single camera
```powershell
python -m pipeline.run --detector yolo `
  --video "data/CCTV Footage/CAM 1.mp4" `
  --camera CAM_FLOOR_01 `
  --store ST1008 `
  --api-url http://localhost:8000
```

### 5.2 All cameras (one after another)
```powershell
foreach ($i in 1..5) {
  python -m pipeline.run --detector yolo `
    --video "data/CCTV Footage/CAM $i.mp4" `
    --camera "CAM_FLOOR_0$i" `
    --store ST1008 `
    --api-url http://localhost:8000
}
```

### 5.3 Dry-run (no API call)
Same flags, add `--dry-run` — events go to stdout instead of being POSTed.

### 5.4 YOLO-specific flags
```
--weights yolov8n.pt          # default; you can swap a custom model path
--device cpu                  # or "cuda:0" if you have a GPU
--start-time 2026-04-10T11:25:00Z   # override clip wall-clock
```

---

## 6. Loading POS data

The system ships **two POS loaders**:

### 6.1 Real Purplle export (38 columns, DD-MM-YYYY, IST)
```powershell
python -m scripts.load_pos --path "data/provided_context/Brigade_Bangalore_10_April_26 (1)bc6219c.csv"
```

Auto-detected via header sniffing (presence of `order_id` + `total_amount`).
Aggregates multi-line orders by `order_id`, sums `total_amount`, converts
IST → UTC.

### 6.2 Canonical 4-column form
```
store_id,txn_id,timestamp,basket_inr
ST1008,T-001,2026-04-10T11:25:00Z,499.50
```
```powershell
python -m scripts.load_pos --path my_pos.csv
```

Both loaders are **idempotent** — re-running the same file inserts 0 new rows.

### 6.3 Force a parser (skip auto-detect)
```powershell
python -m scripts.load_pos --path my.csv --format purplle
python -m scripts.load_pos --path my.csv --format canonical
```

### 6.4 Loading store layouts
```powershell
python -m scripts.load_layout --path data/store_layout.json
```
Format spec: top-level object keyed by `store_id`, each entry has `zones`
with `zone_id`, `polygon`, `is_billing`. See `pipeline/layouts/brigade_road.py`
for a hand-coded example.

---

## 7. Health & anomalies

### 7.1 `/health`
```powershell
curl http://localhost:8000/health
```

Returns per-store last-event timestamps + lag in seconds + a `stale` flag
when `lag > STALE_FEED_THRESHOLD_MINUTES`.

`status` field:
- `ok` — Postgres up + no stale feeds
- `degraded` — Postgres unreachable OR ≥1 store stale
- `down` — never set (the API returning 200 means it's running)

### 7.2 Force a STALE_FEED for testing
Edit `.env`:
```
STALE_FEED_THRESHOLD_MINUTES=1
```
Restart `uvicorn`, run the pipeline once, wait 70 seconds, then
`curl /health` — the store will show `status: "stale_feed"`.

### 7.3 Trigger a queue-spike anomaly
```powershell
$base = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$events = 0..14 | ForEach-Object {
  $ts = (Get-Date).ToUniversalTime().AddSeconds($_*0.5).ToString("yyyy-MM-ddTHH:mm:ssZ")
  @{
    event_id="flood-$_"; event_type="BILLING_QUEUE_JOIN"
    store_id="ST1008"; camera_id="CAM_FLOOR_01"; timestamp=$ts
    track_id="T-$_"; person_id="P-$_"
    zone_id="Z_BILLING"; duration_s=0.0
    confidence=0.9; is_staff=$false
  }
}
$body = @{ events = $events } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method POST -Uri http://localhost:8000/events/ingest `
  -ContentType application/json -Body $body

curl http://localhost:8000/stores/ST1008/anomalies
```
Expected: `queue_spike` with `severity: "critical"`.

---

## 8. Tests & coverage

```powershell
pytest -v                                       # full suite
pytest tests/test_routes_stores.py -v           # one file
pytest -k "anomaly" -v                          # by keyword

coverage run -m pytest && coverage report       # ≥ 70% required
coverage run -m pytest && coverage html         # browseable report at htmlcov/index.html
```

Test layout: see [README.md → Tests](README.md#tests).

---

## 9. Configuration reference

All settings flow through `app/core/config.py` (pydantic-settings) ←
`.env` ← OS env vars. See [.env.example](.env.example) for defaults.

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `local` | `local` / `dev` / `prod` |
| `LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR |
| `LOG_JSON` | `false` | `true` → JSON logs (prod); `false` → human-readable |
| `API_HOST` | `0.0.0.0` | uvicorn bind |
| `API_PORT` | `8000` | uvicorn port |
| `DB_DRIVER` | `sqlite` | `sqlite` or `postgresql+psycopg` |
| `DB_NAME` | `./data/apex.db` | SQLite path or Postgres database name |
| `DB_HOST` `DB_PORT` `DB_USER` `DB_PASSWORD` | unset for SQLite | Postgres connection |
| `EVENT_INGEST_BATCH_MAX` | `500` | Max events per ingest call (413 above this) |
| `SESSION_GAP_MINUTES` | `10` | Gap that splits one visit into two sessions |
| `POS_CORRELATION_WINDOW_MINUTES` | `5` | ±N min match for billing-zone → POS row |
| `STALE_FEED_THRESHOLD_MINUTES` | `10` | `/health` STALE_FEED warning threshold |

---

## 10. Operational recipes

### 10.1 Wipe the DB and start fresh
```powershell
del data\apex.db
alembic upgrade head
```
(Restart `uvicorn` so it reopens the new file.)

### 10.2 End-to-end: real CCTV → analytics → dashboard
```powershell
# Terminal 1
del data\apex.db
alembic upgrade head
uvicorn app.main:app --reload

# Terminal 2 — load POS first so correlation is ready when events arrive
python -m scripts.load_pos --path "data/provided_context/Brigade_Bangalore_10_April_26 (1)bc6219c.csv"

# Terminal 2 — process all 5 cameras with YOLO
foreach ($i in 1..5) {
  python -m pipeline.run --detector yolo `
    --video "data/CCTV Footage/CAM $i.mp4" `
    --camera "CAM_FLOOR_0$i" `
    --store ST1008 `
    --api-url http://localhost:8000
}

# Browser
start http://localhost:8000/dashboard?store=ST1008
```

### 10.3 End-to-end: synthetic demo with POS-aligned dates
```powershell
del data\apex.db
alembic upgrade head
# (restart uvicorn)
python -m scripts.load_pos --path "data/provided_context/Brigade_Bangalore_10_April_26 (1)bc6219c.csv"
python -m pipeline.run --detector synthetic --api-url http://localhost:8000 --start-time 2026-04-10T11:25:00Z
curl http://localhost:8000/stores/ST1008/metrics
```
Expected: `total_purchases > 0`, `gross_basket_inr > 0`, `conversion_rate > 0`.

### 10.4 Multi-store demo
Run scenarios for two store IDs; the dashboard accepts `?store=<id>` to flip
between them. Each store gets its own STALE_FEED row on `/health`.

### 10.5 Acceptance-gate (Docker, for the rubric reviewer)
```bash
docker compose up --build -d
curl http://localhost:8000/health
docker compose down
```
Postgres + API + migrations come up unattended.

---

## 11. Troubleshooting

### 11.1 `pytest` fails on a fresh clone
- Run `pip install -r requirements.txt` first.
- Wipe the test DB by running tests once — they bootstrap their own SQLite.
- If "table events already exists" appears in `test_migrations.py`, you have
  a leftover SQLite file in a temp dir; close all Python processes and retry.

### 11.2 `/metrics` shows `total_purchases: 0` even after loading POS
The synthetic scenario's default start time (`2026-06-01`) doesn't overlap
with the POS dates (`2026-04-10`). Re-run with
`--start-time 2026-04-10T11:25:00Z` (POS time is IST = UTC+5:30, so
`11:25:36 IST = 11:25:36 - 5:30 = 11:25:36 UTC` only when the IST hour was
already 11:25 — the actual buyer-row IST `16:55:36` is `11:25:36 UTC`).

### 11.3 Dashboard shows "reconnecting…" forever
- Confirm `uvicorn` is running on the same host/port.
- Check the browser console — a CORS or mixed-content error means the
  WebSocket protocol is wrong (`ws://` vs `wss://`).

### 11.4 Corporate proxy blocks pip / GitHub
For pip:
```powershell
pip install --proxy http://<user>:<pass>@<proxy-host>:<port> -r requirements-pipeline.txt
```
For YOLO weights, manually download `yolov8n.pt` from
<https://github.com/ultralytics/assets/releases> and place it in the
working directory; pass `--weights ./yolov8n.pt`.

### 11.5 OpenCV can't open the mp4
```powershell
python -c "import cv2; c=cv2.VideoCapture('data/CCTV Footage/CAM 1.mp4'); print('opened:', c.isOpened())"
```
If `False`, install ffmpeg or transcode: `ffmpeg -i in.mp4 -c:v libx264 out.mp4`.

### 11.6 Postgres path: `db` host not reachable
Inside docker-compose, the API container reaches Postgres at host `db` (the
service name). Locally, override `DB_HOST=localhost`. The `.env.example`
keeps SQLite as the default to avoid this trap.

### 11.7 Anomaly endpoint returns no items
This is correct when the store is healthy. To force one, follow
[Section 7.3](#73-trigger-a-queue-spike-anomaly).
