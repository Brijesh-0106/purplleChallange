# TECHNICAL_OVERVIEW.md · Architecture Deep Dive

> Written for engineers who want the full mental model. If you only need
> commands, see [USAGE.md](USAGE.md). If you want decisions and trade-offs,
> see [docs/CHOICES.md](docs/CHOICES.md).

---

## Table of Contents

- [1. System overview](#1-system-overview)
- [2. Layered architecture](#2-layered-architecture)
- [3. Detection pipeline](#3-detection-pipeline)
- [4. Event model](#4-event-model)
- [5. Ingestion path](#5-ingestion-path)
- [6. Sessionisation & analytics](#6-sessionisation--analytics)
- [7. POS correlation](#7-pos-correlation)
- [8. Anomaly detection](#8-anomaly-detection)
- [9. Live dashboard](#9-live-dashboard)
- [10. Observability](#10-observability)
- [11. Persistence & migrations](#11-persistence--migrations)
- [12. Concurrency & failure modes](#12-concurrency--failure-modes)
- [13. Performance characteristics](#13-performance-characteristics)
- [14. Extensibility playbook](#14-extensibility-playbook)

---

## 1. System overview

```
                        ┌─────────────────────────┐
   Raw video / clips ──▶│   Detection Pipeline    │
   (5 cams × 1080p15)   │  YOLOv8n + ByteTrack +  │
                        │  Re-ID + Staff cls +    │
                        │  Group detector + FSM   │
                        └─────────────┬───────────┘
                                      │ Event JSON (HTTP batches ≤ 500)
                                      ▼
                        ┌─────────────────────────┐
                        │   Intelligence API      │
                        │   FastAPI + Pydantic    │
                        │   ┌───────────────────┐ │
                        │   │ Ingestion         │ │ → /events/ingest
                        │   ├───────────────────┤ │
                        │   │ Analytics svcs    │ │ → /metrics, /funnel,
                        │   │  - sessions       │ │   /heatmap
                        │   │  - POS correlation│ │
                        │   │  - metrics        │ │
                        │   │  - funnel         │ │
                        │   │  - heatmap        │ │
                        │   ├───────────────────┤ │
                        │   │ Anomalies         │ │ → /anomalies
                        │   ├───────────────────┤ │
                        │   │ Health probe      │ │ → /health
                        │   ├───────────────────┤ │
                        │   │ Broadcaster (WS)  │ │ → /dashboard, /ws
                        │   └───────────────────┘ │
                        └─────────────┬───────────┘
                                      │ SQLAlchemy 2.0
                                      ▼
                        ┌─────────────────────────┐
                        │  Persistence            │
                        │  SQLite (dev)           │
                        │  Postgres (prod)        │
                        │  Tables: events, pos,   │
                        │   store_layouts         │
                        └─────────────────────────┘

                        ┌─────────────────────────┐
                        │  Live Dashboard         │
                        │  Vanilla JS + Chart.js  │ ← /dashboard
                        │  WebSocket pub-sub      │ ← /ws/stores/{id}
                        └─────────────────────────┘
```

### Bounded contexts
1. **Detection** (`pipeline/`) — turns video into events. Pure pipeline + emitter.
2. **Ingestion** (`app.services.ingestion`) — durable, idempotent receipt of events.
3. **Analytics** (`app.services.{sessions,pos_correlation,metrics,funnel,heatmap,analytics}`) — derived projections at query time.
4. **Anomaly** (`app.services.{anomaly_rules,anomalies}`) — rule-based detection on top of analytics.
5. **Observability** (`app.api.routes_health` + `app.core.middleware`) — operator-facing signals.
6. **Presentation** (`app.api.routes_*` + `app.static.dashboard.html`) — HTTP / WS / HTML.

The contexts share **only the event model and the persistence layer**. No
business logic crosses module boundaries.

---

## 2. Layered architecture

We follow Clean Architecture: dependencies point inward, never outward.

```
┌─────────────────────────────────────────────────┐
│  Presentation                                   │   ← FastAPI routers
│  app/api/routes_*.py                            │      DTOs (Pydantic schemas)
├─────────────────────────────────────────────────┤
│  Application (use cases)                        │   ← orchestrates domain + infra
│  app/services/*.py                              │      no I/O except via repos
├─────────────────────────────────────────────────┤
│  Domain (pure)                                  │   ← entities + invariants
│  app/domain/*.py                                │      zero deps on FastAPI / SQLAlchemy
├─────────────────────────────────────────────────┤
│  Infrastructure                                 │   ← I/O lives here
│  app/infra/{db,models,repositories,loaders}/    │      SQLAlchemy, file readers
└─────────────────────────────────────────────────┘
```

**Rules we enforce:**
- Domain never imports Pydantic, SQLAlchemy, or FastAPI.
- Application imports domain + infra; never directly from `app.api.*`.
- Infrastructure is the **only** layer touching the network or disk.
- DTOs (Pydantic) live in `app/schemas/`; never reach the domain.

This is what lets us mock-out the DB in unit tests, swap SQLite for
Postgres without code changes, and replace YOLO with a synthetic backend
behind the same `DetectorBackend` protocol.

---

## 3. Detection pipeline

```
DetectorBackend  →  EventBuilder (FSM)  →  Emitter (HTTP / dry-run)
        │                  │                    │
        ▼                  ▼                    ▼
DetectorFrame[]    Re-ID + Staff cls +    POST /events/ingest
                   Group detector +
                   per-track FSM
```

### 3.1 `DetectorBackend` protocol
```python
class DetectorBackend(Protocol):
    def frames(self) -> Iterator[DetectorFrame]: ...
```
Two implementations:

| Backend | Purpose | Deps |
|---|---|---|
| **`SyntheticBackend`** | Demo, tests, CI | None — pure Python |
| **`YoloBackend`** | Real video | torch + ultralytics + cv2 (lazy-imported) |

The lazy imports are critical: `pytest` and the synthetic-only path never
load torch, keeping the unit test loop ~6 seconds and the demo viable on a
machine without a GPU.

### 3.2 EventBuilder FSM

The brain of the pipeline. Stateful per `track_id`:

```
State per track:                    Frame → events:
  track_id          → P-???           timeouts first  (EXIT for absent tracks)
  person_id         → P-??? (Re-ID)    per-detection update:
  entry_ts                              new track   → ENTRY or REENTRY
  last_seen_ts                          zone change → ZONE_EXIT, ZONE_ENTER
  last_zone                             billing in   → BILLING_QUEUE_JOIN
  zone_entered_ts                       billing out  → BILLING_QUEUE_ABANDON + DWELL
  is_staff (locked at first sighting)   non-billing
                                          out      → ZONE_EXIT + DWELL
```

**Key behaviours:**
- **Re-ID** — fresh tracks are matched against recently-departed people via
  `ReIDIndex` (cosine similarity on 64-D appearance descriptors). Match → REENTRY.
- **Staff locking** — `is_staff` decided at first sighting and applied to all
  subsequent events for that track (reduces flicker if a single frame is ambiguous).
- **Group detection** — sliding window of recent ENTRYs; if ≥ 2 within
  `group_window_s` (default 1.0s), the events get `group_size` stamped
  retroactively in the same frame.
- **Min-dwell suppression** — DWELL events shorter than `min_dwell_s`
  (default 2.0s) are dropped to avoid jitter at zone boundaries.
- **Determinism** — `event_id`s are deterministic (`evt:store:track:type:ts:seq`)
  so re-running a clip produces identical IDs; the API's idempotency takes care
  of dedup automatically.

### 3.3 Re-ID strategy

A learned model (OSNet, CLIP) would be ~50 MB of dependencies for marginal
gain on the demo's scale. We use:

- **Synthetic visitors:** hand-authored 3-channel RGB `appearance_signature`
  projected to 64-D.
- **Real video:** crop the bbox region, resize to 8×4, fold RGB → 64-D
  (`descriptor_from_bbox_crop`).

Both produce vectors in the same space; the `ReIDIndex` doesn't care which.

| Constant | Default | Purpose |
|---|---|---|
| `cosine_threshold` | 0.85 | Higher = stricter match |
| `revisit_window_s` | 600 (10 min) | Beyond this, treat as a new visitor |
| `max_keep_departed` | 1024 | LRU bound on departed-person memory |

Trade-offs and alternatives discussed in [docs/CHOICES.md → Decision 4](docs/CHOICES.md).

### 3.4 Staff classifier

| Implementation | When to use |
|---|---|
| `LabeledStaffClassifier` | Synthetic (ground-truth flag) |
| `UniformColorStaffClassifier` | Real video — flags near-black uniforms |
| `CompositeStaffClassifier` | Production — explicit flag wins, else fallback |
| `VlmStaffClassifier` | Documented stub for Claude Vision A/B (not wired by default) |

**Bias:** `UniformColorStaffClassifier` returns `False` when no signature is
available — better to under-flag staff (slightly inflates visitor count)
than to over-flag and silently drop real customers.

---

## 4. Event model

Single denormalised `events` table with a closed-set `event_type`:

| event_type | Required fields | Notes |
|---|---|---|
| ENTRY / REENTRY | track_id, person_id | REENTRY only when Re-ID matches a departed person |
| EXIT | track_id, person_id | Emitted via timeout or end-of-stream flush |
| ZONE_ENTER / ZONE_EXIT | + zone_id | Non-billing zones |
| DWELL | + zone_id, duration_s | Suppressed below `min_dwell_s` |
| BILLING_QUEUE_JOIN | + zone_id, duration_s=0 | Entered the billing zone |
| BILLING_QUEUE_ABANDON | + zone_id, duration_s | Left without a POS match (yet) |

**Forward-compat:** Pydantic `extra="allow"` — pipeline producers may attach
arbitrary fields; they land in `payload` (JSON column). Lets us add
`group_size`, `confidence`, etc. without schema migrations.

**Closed enum, type-conditional rules:** Pydantic `model_validator` enforces:
- Zone-bound events MUST carry `zone_id`; perimeter events (ENTRY/EXIT)
  MUST NOT.
- DWELL / BILLING_* MUST carry `duration_s`.
- All timestamps MUST be tz-aware UTC. Naïve datetimes are rejected, not
  silently coerced — the rubric explicitly penalises silent data fixups.

Full rationale in [docs/CHOICES.md → Decision 2](docs/CHOICES.md).

---

## 5. Ingestion path

```
HTTP POST /events/ingest
        │
        ▼
FastAPI router (routes_events.py)
        │  cap check (≤ EVENT_INGEST_BATCH_MAX) → 413 if over
        │  Pydantic validation → 422 if any event malformed
        ▼
IngestionService.ingest()
        │
        ▼
EventRepository.insert_many()
   │
   │  1. Pre-fetch existing event_ids in this batch (one SELECT)
   │  2. Track seen-this-batch to dedupe intra-batch repeats
   │  3. ORM add_all + flush — UNIQUE(event_id) catches concurrent races
   │
   ▼
DB                                     Broadcaster (best-effort)
                                       publish "store_changed" → WS subscribers
```

**Idempotency contract** (see [docs/CHOICES.md → Decision 3](docs/CHOICES.md)):
- Per-event `status` returned: `accepted` / `duplicate` / `rejected`.
- Re-sending the same event_id is a no-op + counted as `duplicate`.
- 503 response on DB unreachable explicitly tells producers to retry the
  same batch (the operation is safe to repeat).

**HTTP semantics:**
| Status | Code | When |
|---|---|---|
| 202 | accepted body | Whole batch processed (incl. partial dups) |
| 413 | BATCH_TOO_LARGE | `len(events) > EVENT_INGEST_BATCH_MAX` |
| 422 | VALIDATION_ERROR | Any event fails Pydantic schema |
| 503 | DEPENDENCY_UNAVAILABLE | DB unreachable |
| 500 | INTERNAL_ERROR | Anything else; sanitised — never leaks stack |

---

## 6. Sessionisation & analytics

The events table is the source of truth; sessions are derived **at query time**.

### 6.1 Session definition
A `Session` is a contiguous span of `[ENTRY|REENTRY → … → EXIT]` events for
one `(store_id, person_id)`. Two rules:
- REENTRY events **within** a session do **not** open a new session.
- A new session opens when the gap between EXIT and the next ENTRY/REENTRY
  exceeds `session_gap_minutes` (default 10).

This is what "session-based, no double counting" in the rubric means:
- A customer who steps out for a phone call → 1 session.
- The same customer Monday + Tuesday → 2 sessions.

### 6.2 Analytics call graph

```
GET /stores/{id}/metrics
        │
        ▼
AnalyticsService._load(store_id, since, until)
        │
        ├─▶ EventRepository.fetch_for_analytics()  →  list[event_dict]
        ├─▶ POSRepository.fetch_in_window()        →  list[pos_row]
        │
        ▼
build_sessions(events, session_gap_minutes)        →  list[Session]
        │
        ▼
correlate_purchases(sessions, pos_rows, ±5 min)    →  list[Session] with .purchase
        │
        ▼
compute_store_metrics(sessions)                    →  StoreMetrics DTO
```

`/funnel`, `/heatmap`, and `/anomalies` reuse the same `_load` →
`build_sessions` → `correlate_purchases` pipeline; only the final `compute_*`
function differs.

### 6.3 `data_confidence` flag
When a window has fewer than 20 customer sessions, the response carries
`data_confidence: "low"`. The dashboard greys out figures with low
confidence so consumers don't read noise as signal. Threshold is configurable.

---

## 7. POS correlation

The detection pipeline can't see what happens **at** the counter — only that
a person entered the billing zone and left. POS correlation is what
upgrades the abandon → purchase signal.

### 7.1 Algorithm
1. Sessions are sorted by `started_at` (earliest first).
2. POS rows grouped by `store_id`, sorted by timestamp.
3. For each session that **reached billing** (and isn't staff):
   - Find the first un-consumed POS row at the same store with timestamp
     within `±POS_CORRELATION_WINDOW_MINUTES` of the session's
     `[started_at, ended_at]`.
   - If found: mark `purchase=True`, attach `purchase_basket_inr`,
     **consume the POS row** (so subsequent sessions can't double-count it).

### 7.2 Why "consume-once" matters
A single transaction is one customer's purchase. Without the consume-once
guard, three sessions overlapping the same POS row would all be marked as
purchasers — corrupting conversion-rate by a factor of 3. The
`test_pos_correlation::test_only_first_match_in_window_used` and the
`test_routes_stores::test_metrics_happy_path` tests guard this invariant.

---

## 8. Anomaly detection

Three rule types, each pure (events / sessions in → list[Anomaly] out):

### 8.1 `queue_spike`
Time-bucketed queue depth from BILLING_QUEUE_JOIN/ABANDON deltas. Fires
when depth ≥ `threshold_depth` (default 7) for ≥ `min_duration_s` (default
60s). Severity escalates to **critical** at peak depth ≥ 12.

Suppression: short blips that don't satisfy `min_duration_s` are dropped.
The brief's "queue spiking" example is about sustained pressure, not pulses.

### 8.2 `conversion_drop`
Compares current-window conversion rate to a baseline-window rate (default:
last hour vs prior 24 h). Fires when:
- `current / baseline ≤ drop_ratio` (default 0.5),
- baseline visitors ≥ 20,
- current visitors ≥ 5.

The denominator floors are essential — without them, every quiet morning
fires the rule, and reviewers see alert fatigue.

### 8.3 `dead_zone`
A zone in the store's expected-zone list with **zero** customer visits while
others logged ≥ `min_other_visits` (default 5). Staff visits don't count — a
staffer walking through Z_PMU doesn't reflect customer engagement.

### 8.4 Severity ordering
Anomalies are sorted **critical → warn → info**, then by detection time.
Empty result is the common case (the store is fine); the route returns
`200 { count: 0, anomalies: [] }` rather than 404.

Each anomaly includes:
- `severity` (closed enum)
- `message` (human-readable, includes evidence)
- `suggested_action` (short, imperative)
- `details` (rule-specific evidence dict — e.g. `peak_depth`)

Full rationale: [docs/CHOICES.md → Decision 5](docs/CHOICES.md).

---

## 9. Live dashboard

Two-channel design:
1. **WebSocket** carries tiny `{"type": "store_changed", "store_id": "..."}`
   pings (one per ingest batch).
2. **HTTP** fetches the actual analytics on each ping.

Why split? The wire payload stays trivially small and the data is always
fresh. Schema changes to `/metrics` don't require WebSocket protocol changes.

### 9.1 Broadcaster
In-process pub-sub keyed by `store_id`. Per-connection async-lock,
fire-and-forget sends with dead-client reaping. Singleton via
`get_broadcaster()`.

We chose in-process over Redis/Kafka because:
- Brief targets one API instance.
- An external broker is one more thing to fail in the acceptance gate.
- Same protocol — swap for a Redis-backed implementation later if scale demands.

### 9.2 Dashboard HTML/JS
Single static file (`app/static/dashboard.html`):
- Vanilla JS — no build step, no bundler.
- Chart.js from CDN (one `<script>` tag).
- Auto-reconnects on WebSocket close (server restart heals in 2s).
- Heartbeat refresh every 5s — covers idle stores so STALE_FEED warnings
  appear progressively.

### 9.3 Replay script
`scripts/replay_events.py` paces synthetic events to wall-clock time
(`--speed 1.0` = real-time, `--speed 6.0` = 6× faster) so the dashboard
updates progressively during a live demo.

---

## 10. Observability

### 10.1 Per-request structured logs
Every request emits **one** access-log line via structlog with:

```json
{
  "timestamp": "2026-04-10T11:25:36.123Z",
  "level": "info",
  "event": "request.completed",
  "trace_id": "9f3e...",
  "method": "POST",
  "endpoint": "/events/ingest",
  "status_code": 202,
  "latency_ms": 14.7,
  "store_id": "ST1008",
  "event_count": 18,
  "accepted": 17,
  "duplicates": 1,
  "rejected": 0
}
```

Domain context (`store_id`, `event_count`, …) is attached via
`structlog.contextvars` from inside service methods — the route doesn't
need to remember to log it.

### 10.2 `trace_id` propagation
- Generated per-request by `RequestContextMiddleware`.
- Honours an inbound `x-trace-id` header so an upstream load balancer
  / dashboard can propagate.
- Bound to the structlog context for the request's lifetime.

### 10.3 `/health` per-store stale-feed
For each store with at least one event, `/health` reports:
- `last_event_at`
- `lag_seconds`
- `stale: true|false` (lag > `STALE_FEED_THRESHOLD_MINUTES`)
- `status: "ok" | "stale_feed" | "no_data"`

If any store is stale, `/health` overall `status` flips to `degraded` —
external monitors can page on `status != "ok"` without conflating a stale
detection feed with a fully-down API.

### 10.4 Sanitised errors
All exception paths funnel through `app.core.errors.register_exception_handlers`:
- AppError → status from `http_status` field
- RequestValidationError → 422 with safe-serialised Pydantic errors
- Anything else → 500 INTERNAL_ERROR (no stack trace in body, full
  traceback in logs)

---

## 11. Persistence & migrations

### 11.1 Tables
| Table | PK / Uniqueness | Notes |
|---|---|---|
| `events` | id (surrogate) · UNIQUE(event_id) | Append-only, idempotent |
| `pos_transactions` | id (surrogate) · UNIQUE(store_id, txn_id) | Idempotent re-load |
| `store_layouts` | store_id (PK) | One JSON blob per store |

### 11.2 Indexes
- `events`: `(store_id, timestamp)`, `(store_id, zone_id, timestamp)`,
  per-column on `event_id`, `event_type`, `track_id`, `person_id`,
  `zone_id`, `is_staff`.
- `pos_transactions`: `(store_id, timestamp)`.

### 11.3 Migrations
Alembic baseline (`migrations/versions/0001_initial.py`) is hand-authored to
match `app/infra/models.py` exactly. A test (`tests/test_migrations.py`)
asserts that `alembic upgrade head` produces the same tables/columns as
`Base.metadata` — catching drift in CI.

### 11.4 SQLite ↔ Postgres compatibility
- All migration types are dialect-neutral (`String`, `Integer`,
  `DateTime(timezone=True)`, `Numeric`, `JSON`).
- SQLite drops tzinfo on round-trip; the repositories normalise on read
  via `_utc(ts)` so downstream code never sees naive datetimes regardless
  of dialect.
- No `INSERT ... ON CONFLICT` syntax — we do dedup in Python before insert
  and rely on UNIQUE constraints as a race-safety net.

---

## 12. Concurrency & failure modes

### 12.1 Concurrent ingest
Two pipelines POSTing the same `event_id` will race the UNIQUE constraint.
The repository catches the `IntegrityError`, re-fetches existing IDs,
re-classifies the loser as `duplicate`, and re-flushes only the new rows.
No data loss, no duplicates, honest per-event response.

### 12.2 DB unavailable mid-ingest
`OperationalError` is caught in the service layer and translated to
`DependencyUnavailableError` → 503 with `code: "DEPENDENCY_UNAVAILABLE"`
and a hint that the operation is idempotent (retry the same batch).

### 12.3 WebSocket failures
Dead/closed clients are reaped silently on the next publish — never raises
to the caller. Failed sends are logged at DEBUG level only.

### 12.4 Pipeline emitter retries
- Network errors (httpx exceptions) → exponential backoff up to `max_retries`,
  then move to a dead-letter list (`pending()`).
- 503 responses → retry per the API contract.
- 4xx responses → no retry (data is malformed; retrying never helps); move
  to dead-letter so the operator can inspect.

### 12.5 Logging stream lifecycle
The `_LazyStderrLogger` (in `app.core.logging`) reads `sys.stderr` at write
time, not at config time — which fixes pytest's `capsys` swapping the
stderr stream out from under cached structlog factories.

---

## 13. Performance characteristics

### 13.1 Detection
| Backend | Throughput (CPU, i7-12th) | Memory |
|---|---|---|
| `SyntheticBackend` | ~10k events/s | < 50 MB |
| `YoloBackend` (yolov8n.pt, 1080p) | ~5–10 fps | ~600 MB (torch baseline) |

For real-time on 1080p15fps you need either GPU or frame-skipping
(configurable via `--conf`/`--iou` thresholds and a future frame stride flag).

### 13.2 API
- `/events/ingest` (200 events): ~10–30 ms median on SQLite.
- `/metrics`, `/funnel`, `/heatmap`: ~5–20 ms on a few thousand events.
- `/anomalies`: ~30–80 ms (loads two windows of events + sessionises both).

### 13.3 Scale ceiling (current code)
Sessions are computed at query time. For a single store-day (~1k events)
this is fast. For 40 stores × multiple weeks, promote sessions to a
materialised table (one nightly compute, queries become point-reads).

---

## 14. Extensibility playbook

| Want to add… | Where to plug in |
|---|---|
| A new event type | `app/domain/events.py` + `app/schemas/events.py` validators + EventBuilder emit logic |
| A new analytics endpoint | New file in `app/services/`, expose via `app/api/routes_stores.py`, add Pydantic DTO in `app/schemas/analytics.py` |
| A new anomaly rule | New function in `app/services/anomaly_rules.py`, wire into `AnomalyService.list_for_store` |
| A different DB | Change `DB_DRIVER` env var; SQLAlchemy handles the rest. Add a dialect-specific column type only if you have to. |
| A different detector (RT-DETR, YOLOv9) | New class implementing `DetectorBackend`; CLI gets a new `--detector` choice. |
| A learned Re-ID model | Replace `descriptor_from_*` functions; descriptor dim is configurable. |
| Multi-store dashboard | Dashboard already accepts `?store=<id>`. Extend the broadcaster's "all stores" subscription if needed. |
| External pub-sub | Replace `Broadcaster` with a Redis-backed class — same `subscribe / unsubscribe / publish` protocol. |

---

## See also

- [docs/DESIGN.md](docs/DESIGN.md) — high-level architecture + AI-assisted decisions.
- [docs/CHOICES.md](docs/CHOICES.md) — five reasoned engineering decisions.
- [USAGE.md](USAGE.md) — every command, every endpoint, every flag.
- [BUSINESS_OVERVIEW.md](BUSINESS_OVERVIEW.md) — what this means for the business.
