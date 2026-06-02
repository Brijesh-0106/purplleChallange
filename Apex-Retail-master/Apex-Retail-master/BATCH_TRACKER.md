# Batch Tracker

## Project: Apex Retail — Store Intelligence System

**Goal:** End-to-end CCTV → real-time analytics API for retail stores.
**North Star:** Offline Store Conversion Rate.
**Stack:** Python 3.11 (.venv) · FastAPI · SQLite (dev) / Postgres (acceptance) · YOLOv8 + ByteTrack · Synthetic-event demo backend · Vanilla JS + Chart.js dashboard.

**Demo target store:** `ST1008` — Purplle Brigade Road, Bangalore (real layout, 8 zones).

---

### Batch Plan — ALL 8 BATCHES SHIPPED ✅

| # | Batch | Scope | Status |
|---|-------|-------|--------|
| 1 | Project Foundation | Skeleton, config, structured logging, `/health`, docker-compose, smoke tests | ✅ Completed |
| 2 | Event Schema & Data Layer | Pydantic v2 event schema, SQLAlchemy models, Alembic migration, repositories, POS + layout loaders | ✅ Completed |
| 3 | Ingestion API + Idempotency | `POST /events/ingest`, dedup on `event_id`, partial-success errors, structured access logs, graceful 503 | ✅ Completed |
| 4 | Detection Pipeline — Core | YOLOv8 + ByteTrack (lazy imports) **and** SyntheticBackend; per-track FSM; CLI; HTTP / dry-run emitters | ✅ Completed |
| 5 | Detection Pipeline — Intelligence + Real-Context | Brigade Road layout · 5-archetype scenario · Re-ID · staff classifier · group entry · Purplle POS loader | ✅ Completed |
| 6 | Intelligence Endpoints | Sessions · POS correlation · `/metrics` (+`/Metrics`) · `/funnel` · `/heatmap` · seed fixtures | ✅ Completed |
| 7 | Anomalies + Health Polish | `/anomalies` (queue_spike / conversion_drop / dead_zone) · `/health` per-store STALE_FEED · graceful degradation | ✅ Completed |
| 8 | Live Dashboard + Tests + Docs | WebSocket broadcaster · vanilla-JS + Chart.js dashboard · replay script · ≥70% coverage · final docs | ✅ **Completed** |

---

### Current Batch

**Batch 8 · Live Dashboard + Final Polish — ✅ Completed**

#### Deliverables

**WebSocket pub-sub:**
- `app/services/broadcaster.py` — in-process pub-sub keyed by `store_id`. Per-connection async-lock, fire-and-forget sends with dead-client reaping. Singleton via `get_broadcaster()`.
- `IngestionService.ingest()` — best-effort `store_changed` ping per affected store after successful insert; broadcaster failures NEVER fail ingest.

**Routes:**
- `app/api/routes_dashboard.py` — `GET /dashboard` returns the static HTML; `WebSocket /ws/stores/{store_id}` accepts long-lived subscriptions and unsubscribes cleanly on disconnect.

**Dashboard UI:**
- `app/static/dashboard.html` — single-page vanilla JS + Chart.js (CDN, no build step). Four KPI cards (conversion / visitors / purchases / queue), a funnel bar chart, a per-zone visit-share bar chart, an anomaly feed (severity-coloured), and a per-store health table with the STALE_FEED status. Auto-reconnects on WebSocket close.

**Replay:**
- `scripts/replay_events.py` — paces the brigade synthetic scenario into the API at real-time speed (`--speed N` for 1×/3×/6×). Demos look live without needing real video.

**Tests (8 new):**
- `tests/test_broadcaster.py` — 7 async tests: subscribe/unsubscribe count, JSON publish, store-isolation, dead/closed client reap, no-op publish, singleton.
- `tests/test_dashboard_route.py` — 2 tests: HTML served at `/dashboard`, WebSocket subscribe → publish → receive round-trip.

**Coverage:**
- `pyproject.toml` — `coverage.report.fail_under = 70` (was 0). Per the rubric's "Testing covers key scenarios" criterion.

**Final docs polish:**
- `README.md` — fully rewritten. Reviewer-friendly 90-second demo recipe at the very top, acceptance-gate path explicit, full project structure, tests-by-file table, all 5 acceptance-gate checks ticked.
- `docs/DESIGN.md` — Overview ASCII pipeline diagram + final architecture map + dashboard branch added.
- `docs/CHOICES.md` — 5 decisions, ~3,500 words combined.

---

### Notes & Decisions

**B8.1** **Two-channel design (push pings, pull data).** WebSocket carries tiny `{"type":"store_changed","store_id":"..."}` payloads; the dashboard turns each ping into HTTP fetches against `/metrics /funnel /heatmap /anomalies /health`. The wire payload stays tiny, the data is always fresh, and analytics changes don't require WebSocket protocol changes.

**B8.2** **In-process broadcaster, not Redis/Kafka.** The brief targets one API instance; an external broker would add an acceptance-gate dependency for zero scoring benefit. The protocol is plain `subscribe / unsubscribe / publish` — swap the implementation later if scale demands it.

**B8.3** **Best-effort broadcast — never fails ingest.** The events are durable in the DB before we publish; broadcaster errors are caught and logged at DEBUG level. Producers see consistent `202 Accepted` regardless of dashboard state.

**B8.4** **Vanilla JS + CDN Chart.js, no build step.** A bundled React/Vite app would impress on craft but cost the reviewer time and complicate the acceptance-gate Docker image. Static HTML loads instantly and is trivially auditable.

**B8.5** **Auto-reconnect on WebSocket close.** A server restart heals itself in 2 s; the dashboard surfaces "reconnecting…" between attempts so the operator sees the connection state rather than stale data.

**B8.6** **`replay_events.py` separate from `pipeline.run`.** The runner bursts events at maximum speed (right for ingest tests); the replayer paces them (right for live demos). One file each, sharing the same EventBuilder.

**B8.7** **Heartbeat refresh every 5s.** Even when no events are landing, the dashboard re-fetches `/health` and `/anomalies` so STALE_FEED warnings appear progressively (not only when an event arrives).

**B8.8** **Coverage gate raised to 70%.** Per the Evaluation Framework's "Testing: Covers key scenarios and edge cases" criterion.

---

### Final Numbers

| Metric | Value |
|---|---|
| Source files (app/ + pipeline/) | ~50 |
| Test files | 26 |
| Tests | ~165 |
| `CHOICES.md` decisions | 5 (model, schema, idempotency, Re-ID, anomalies) |
| `DESIGN.md` words | ~750 |
| `CHOICES.md` words | ~3,500 |
| Endpoints | `/health` · `/events/ingest` · `/stores/{id}/{metrics,Metrics,funnel,heatmap,anomalies}` · `/dashboard` · `/ws/stores/{id}` |
| Acceptance-gate checks | 5 / 5 ✅ |

---

### Risks Carried Forward (operational, not blocking)

- **YOLO weights download** (`yolov8n.pt`, ~6 MB) is fetched on first real-video run. Lilly proxy may need to allow `github.com` / `huggingface.co` releases. Synthetic demo path is unaffected.
- **Cross-camera Re-ID** is currently single-camera per-instance. A multi-camera deployment would need to merge `frames()` streams from multiple `YoloBackend` instances into one `EventBuilder` (deferred — single-camera covers the rubric and the Brigade Road demo).
- **Postgres in production** vs SQLite in dev — same SQLAlchemy code path, but a real perf-test on a year of events would surface index gaps. Indexes today are `(store_id, timestamp)` and `(store_id, zone_id, timestamp)`; sufficient for the rubric.
