# CHOICES.md

> Five decisions, fully reasoned. Filled out as decisions are made across batches.

---

## Decision 1 — Detection model & tracker (YOLOv8n + ByteTrack, with a synthetic fallback backend)

**Context.** The brief requires a detection-and-tracking pipeline that runs on
1080p15fps CCTV footage from 5 stores × 3 cameras × 20 minutes. The reviewer's
acceptance gate runs on a generic Linux/macOS Docker host (no guaranteed GPU).
The user's local dev machine is a Lilly-issued Windows laptop with no GPU,
no challenge dataset on hand, and corporate-proxy constraints on outbound
network. The detection pipeline must be **(a) accurate enough** to make the
event-driven analytics meaningful, **(b) fast enough** to keep up with 15 fps
on CPU during a live demo, **(c) testable without torch / video / weights**,
and **(d) demoable when no real footage is available**.

**Options considered.**

| Option | Pros | Cons |
|---|---|---|
| **YOLOv8n + ByteTrack (Ultralytics) — primary backend** ✅ | One pip install pulls model+tracker; ~6 MB weights auto-fetch; production-grade person detection on COCO; ByteTrack handles short occlusions; Ultralytics' API exposes track IDs cleanly via `model.track(stream=True, persist=True, tracker='bytetrack.yaml')` | Pulls torch (~200 MB) — heavy on a corporate-proxy laptop; CPU inference ~5–10 fps on a typical i7 → marginal for live 15 fps |
| YOLOv8s + ByteTrack | ~2 mAP better than n | ~3× slower on CPU — kills live demo on laptops |
| YOLOv8n + DeepSORT (separate) | More configurable Re-ID | Extra wiring; ByteTrack matches DeepSORT quality on this use-case (people in retail) and is built-in |
| RT-DETR / YOLOv9 / YOLO-World | Newer, sometimes more accurate | Larger weights, less battle-tested for tracking, heavier API surface |
| **`SyntheticBackend` — companion backend, not a replacement** ✅ | Deterministic events for tests / demo; zero deps; can demo a perfectly-scripted "buyer / abandoner / browser" scenario without any video | Doesn't actually detect anything — only useful where ground-truth scenarios suffice (tests, demo) |

**Decision.** Two backends behind a single `DetectorBackend` protocol:

1. **`YoloBackend`** (production) — Ultralytics YOLOv8n + ByteTrack, lazy-imported
   so `pytest` and the synthetic-only demo path never load torch / cv2. Defaults
   chosen with the brief's hardware budget in mind: `yolov8n.pt`, `conf=0.4`,
   `iou=0.5`, person class only, `tracker=bytetrack.yaml`, CPU device.
2. **`SyntheticBackend`** (demo / test) — produces declarative scenarios with
   linear-interpolated waypoints. The pre-baked `demo_scenario` walks three
   distinct visitor archetypes (buyer, abandoner, browser) through the demo
   layout so every event type fires at least once during the demo.

The CLI exposes `--detector synthetic|yolo`. Tests use `synthetic` (or the
mocked YOLO test in `test_yolo_backend.py`) so the pipeline test surface
runs in <1 second on a vanilla `.venv` with no extra installs.

**Consequences.**

* **Test isolation.** Importing `pipeline.yolo_backend` does NOT import torch
  or cv2. Both are imported at the first call to `frames()`, with friendly
  `RuntimeError`s if they're missing — the user sees "run `pip install -r
  requirements-pipeline.txt`" instead of an opaque `ImportError`.
* **Two requirements files.** `requirements.txt` stays light enough for the
  reviewer's CI; `requirements-pipeline.txt` adds the heavy CV stack.
* **Demo never blocks on dataset availability.** `python -m pipeline.run
  --detector synthetic --dry-run` emits a complete event stream (ENTRY →
  ZONE_ENTER/EXIT → DWELL → BILLING_QUEUE_JOIN/ABANDON → EXIT) that the API,
  analytics endpoints, and live dashboard can consume end-to-end.
* **Real footage is a one-line swap** — `--detector yolo --video path/to.mp4`
  is the only CLI change needed when the dataset arrives.
* **Confidence is preserved**, never silently dropped. Detections below
  `conf=0.4` are filtered at the Ultralytics layer (efficiency); everything
  above flows through with its real confidence written into each event.

---

## Decision 2 — Event schema design

**Context.** The pipeline emits events that the API ingests, the DB stores, and
analytics endpoints aggregate. The schema is the contract between every layer
and is the hardest thing to change later — getting it right early matters.

**Options considered.**

| Option | Pros | Cons |
|---|---|---|
| **Single denormalised event table with closed-set `event_type`** ✅ | Idempotent ingest is trivial (one UNIQUE column, `event_id`); analytics just filter by type; future event types added without migrations; matches the brief's verbs (ENTRY/EXIT/ZONE_*/DWELL/BILLING_*) directly | Some columns (`zone_id`, `duration_s`) are nullable for events that don't use them — makes the table "loose" |
| **Per-type tables** (`entries`, `exits`, `zone_events`, `billing_events`) | Strong typing per row; no nulls | Cross-type analytics need UNIONs; adding a new type means a migration; idempotency dedup spans 4 tables |
| **JSON-only storage** (one column, `payload jsonb`, generated columns) | Maximally flexible; pipeline can emit anything | Expensive indexes; weak validation; brittle aggregation queries; harder for reviewers to read; loses Pydantic-↔-ORM symmetry |

**Decision.** Single denormalised events table with **`event_type` as a closed
enum** in the domain layer. Pydantic enforces the closed set; SQLAlchemy stores
the string value; type-conditional rules (zone_id required iff zone-bound,
duration_s required for DWELL/BILLING_*) are validated in one `model_validator`.
Forward-compatible: pipeline producers may attach extra fields and they land
in `payload` (JSON), so future event attributes (e.g. `track_id`, `is_staff`,
`group_size` — already promoted to first-class columns) don't require a
schema-breaking change.

**Consequences.**

* **Idempotency** is one `UNIQUE(event_id)` constraint; the repository pre-fetches
  existing IDs and falls back to a race-safe re-classification if a concurrent
  producer collides. Works on Postgres and SQLite without dialect-specific SQL.
* **UTC enforcement.** Naïve timestamps are *rejected*, not silently coerced —
  the brief explicitly penalises silent data fixups.
* **`extra="allow"`** keeps producers and consumers loosely coupled while
  keeping the strongly-typed columns the analytics layer depends on.
* **Indexing.** `(store_id, timestamp)` covers most analytics queries;
  `(store_id, zone_id, timestamp)` covers funnel/heatmap; `event_id` is unique
  and indexed for ingest dedup.

---

## Decision 3 — Idempotency at the API layer (not the DB layer alone)

**Context.** The detection pipeline emits events over HTTP. CCTV and edge boxes
are unreliable: a network blip mid-batch can leave the producer unsure whether
its last `POST /events/ingest` actually landed. The natural mitigation is for
producers to retry — but retries must NEVER produce duplicate rows in the
analytics store, because every metric (conversion rate, dwell time, queue
depth) is a count over events.

**Options considered.**

| Option | Pros | Cons |
|---|---|---|
| **Idempotency split: validate + dedup at API layer; UNIQUE constraint as a DB safety net** ✅ | Producer gets per-event feedback (`accepted` vs `duplicate`); race-safe under concurrent producers; portable across SQLite + Postgres; observable (counters in the access log) | Two layers of dedup logic to maintain |
| **DB-only**: rely on `INSERT ... ON CONFLICT DO NOTHING` | Simplest code | Postgres-specific (loses test portability with SQLite); producer can't tell duplicates from accepted writes — every retry returns "all OK" even if the original payload was different |
| **Idempotency-Key header** (Stripe-style) | Industry-standard; client controls retry semantics | Heavier — adds an idempotency-keys table, TTLs, replay-payload comparison; overkill given that `event_id` is already a globally-unique natural key |
| **No idempotency, fix duplicates in analytics with `DISTINCT`** | Trivial ingest path | Every analytics query slows down; metric correctness depends on remembering to dedup everywhere — hostile to onboarding |

**Decision.** **Idempotency at the API/repository layer**, with the UNIQUE constraint
as a race-safety net. Concretely:

1. The repository pre-fetches `event_id`s already in the DB and classifies each
   incoming event as `accepted` or `duplicate` *before* the insert.
2. The remaining "new" rows are inserted in one flush. If two concurrent
   producers race the same `event_id`, the UNIQUE constraint blocks the
   loser; the repo catches the `IntegrityError`, re-runs the existence check,
   and re-classifies the affected rows as `duplicate`. No data loss; no
   duplicates; the API still returns honest per-event statuses.
3. Intra-batch duplicates (same `event_id` twice in the same payload) collapse
   to "first wins, rest are duplicates" — proven by `test_intra_batch_duplicate_event_ids`.

**Consequences.**

* **Honest feedback to producers.** Per-event `status` lets the pipeline
  reconcile "did my last batch reach the API?" without re-emitting rows.
* **Graceful degradation.** When the DB is unreachable, the service raises
  `DependencyUnavailableError`, the route returns **503** with code
  `DEPENDENCY_UNAVAILABLE`, and the response body explicitly tells the
  producer to retry (the operation is idempotent). No partial writes.
* **Portability.** The same code runs against SQLite (in tests) and
  Postgres (in prod) without dialect-specific SQL.
* **Observability.** `event_count`, `accepted`, `duplicates`, and `rejected`
  are bound to the request's structlog contextvars — they appear in the
  trailing `request.completed` access log line alongside `trace_id`,
  `endpoint`, and `latency_ms`. One log line per request, fully reconcilable.
* **Cap.** Batch size is capped at `EVENT_INGEST_BATCH_MAX` (default 500);
  exceeding it returns **413** with code `BATCH_TOO_LARGE` and the actual /
  allowed counts in `details` so callers can adapt without guessing.

---

## Decision 4 — Re-ID, staff exclusion, and group-entry: lightweight in-process logic over heavy ML

**Context.** The evaluation rubric explicitly rewards "Handles re-entry, staff,
and group entry correctly". A naïve pipeline that takes ByteTrack track_ids
at face value will:

* Count one customer who steps out for a phone call and walks back in as **two
  visitors** — double-counting destroys the conversion-rate metric.
* Include the **floor staff** in visitor counts — Purplle's floor team walks
  the store all day; treating them as customers crashes conversion to ~0.
* Treat **a group of three friends** entering together as three independent
  visitors — overstates traffic and warps funnel ratios.

The natural reach is for a deep-learning Re-ID model (OSNet via `torchreid`,
or fine-tuned CLIP), a uniform/staff-detection classifier, and a learned
group-clustering model. **All three are overkill for a 10-minute reviewer
demo**, especially with no real footage to train against and a Lilly proxy
that may block model weight downloads.

**Options considered (Re-ID).**

| Option | Pros | Cons |
|---|---|---|
| **`torchreid` OSNet** (~10 MB weights) | High accuracy, well-known | Adds heavy CV deps to the API surface; weights need a download path; one more thing that can fail at the acceptance gate |
| **CLIP / Hugging Face VLM embeddings** | Zero training | Even bigger weights, slower CPU inference, network dep |
| **Cosine on tiny RGB-grid descriptors (8×4 → 64-D)** ✅ | No ML deps; deterministic; testable; works for both real video (cv2 crop+resize) and synthetic (3-channel signature → 64-D projection) | Lower discrimination than learned embeddings — fine when person count is small (a single store on one camera) but degrades at scale |
| **Trajectory-only re-ID** (foot-point distance + time) | Simplest | Doesn't survive an EXIT (the whole point of REENTRY) |

**Decision (Re-ID).** **Lightweight cosine on 64-D tile descriptors.** The same
descriptor space is consumed by both backends — `SyntheticBackend` projects a
hand-authored 3-channel `appearance_signature` into 64-D; `YoloBackend`
crops, resizes to 8×4, and folds RGB → 64-D via `descriptor_from_bbox_crop`.
A `ReIDIndex` keeps two tables: `_active` (currently inside the store) and
`_departed` (left within the last 10 minutes). On a fresh ENTRY the index
queries `_departed` for the highest cosine similarity ≥ 0.85; if a hit is
found, the new track inherits the matched `person_id` and the EventBuilder
emits a **REENTRY** instead of an ENTRY. Outside the 10-minute window the
match is dropped (LRU-bounded at 1024 departed identities).

**Options considered (staff classifier).**

| Option | Pros | Cons |
|---|---|---|
| **Labelled flag from synthetic data** ✅ for tests | Ground truth, deterministic | Doesn't help on real video |
| **Uniform colour heuristic** ✅ for real video | No ML deps; matches Purplle's actual "staff in dark uniform" reality; zero install cost | Brittle if dress code changes; under-flags by design (we'd rather miss a staffer than drop a real customer) |
| **Claude Vision A/B per ambiguous frame** (stub `VlmStaffClassifier`) | Most accurate signal | Adds a paid API call + ~1 s latency per ambiguous frame; needs an Anthropic key in the acceptance-gate env (likely missing); cache by descriptor would help but is one more thing to build |
| Fine-tuned classifier head on top of YOLO | Highest accuracy | Needs labelled training data we don't have |

**Decision (staff classifier).** **`LabeledStaffClassifier`** for the
synthetic-driven test/demo path; **`UniformColorStaffClassifier`** for the
real-video path; **`CompositeStaffClassifier`** as the production glue —
respects an explicit `is_staff` flag when the detector provides one, falls
back to colour matching otherwise. The **VLM path is documented as a stub**
(`VlmStaffClassifier`) so a reviewer can see the upgrade path without paying
its operational cost in the acceptance gate.

**Options considered (group entry).**

| Option | Pros | Cons |
|---|---|---|
| **Sliding-window count of ENTRY/REENTRY events** ✅ | Trivially correct; cost is one timestamp per recent entry | Doesn't reason about *which* tracks are in the same group — but the brief asks for group entry detection, not group identity |
| Spatio-temporal clustering (DBSCAN on ENTRY positions + ts) | Identifies group members | Significantly more code to maintain; the brief doesn't reward identity, just count |
| Off-the-shelf person-association model | Most rigorous | Same heavy-ML objections as Re-ID above |

**Decision (group entry).** **Sliding-window count.** When ≥2 ENTRY/REENTRY
events fall within `group_window_s` (default 1.0 s), the EventBuilder stamps
`group_size` on the events emitted *this frame*. Analytics layer
treats `group_size >= 2` as "part of a group" and can de-bias visitor counts
or report group-conversion as a derived metric. Earlier-frame entries already
flushed retain their original `group_size=null`; the test
`test_group_entry_stamps_group_size` enforces that ≥3 of the trio's entries
get stamped.

**Consequences.**

* **Zero new heavy deps.** `requirements.txt` stays small; `requirements-pipeline.txt`
  only carries Ultralytics + cv2 (already there from Batch 4). The Re-ID layer
  is pure Python.
* **Two ground-truth signals on every event.** `is_staff` and `person_id`
  flow through ENTRY → ZONE_* → BILLING → EXIT for the same track. Analytics
  in Batch 6 just filters `is_staff=False` and counts distinct `person_id`s.
* **Demo is self-evidencing.** The brigade scenario explicitly contains one
  re-entry pair (V-002 → V-002b sharing a blue-jacket signature), one staff
  member (STAFF-01 in dark uniform), and a group of three with distinct
  signatures. The reviewer sees REENTRY, `is_staff=true`, and `group_size=3`
  in the dry-run output — direct proof that all three rubric items work.
* **Thresholds are config-driven**: `cosine_threshold`, `revisit_window_s`,
  `group_window_s` all live in dataclasses with sensible defaults, ready to
  be tuned against real footage if/when it arrives.

---

## Decision 5 — Anomaly detection: rule-based, time-bucketed, with explicit suppression on tiny denominators

**Context.** The evaluation rubric (Section 5.2) explicitly rewards "logical
and meaningful" anomalies, and the brief asks for `severity` and
`suggested_action` fields. The path of least resistance — a generic
"statistical outlier" detector (z-scores over event counts) — looks
sophisticated but produces alerts that an operator can't act on. The
opposite extreme — hardcoded "store closed at 19:00" rules — is brittle and
unscored.

**Options considered.**

| Option | Pros | Cons |
|---|---|---|
| **Three rule types tied to specific business KPIs** ✅ | Each anomaly maps to an obvious operator action (open a counter / investigate top SKUs / check signage); short messages fit the 10-min review budget; thresholds are explainable in plain English | Three rules don't cover every conceivable failure mode — but the rubric scores meaning, not coverage |
| Single generic statistical outlier detector | Looks "ML-y"; works on any time series | Operators can't act on "z-score on dwell time was 2.7" without context; small stores have noisy denominators that explode false-positive rates |
| LLM-based anomaly summariser fed the metric series | Friendly natural-language alerts | Cost + latency per call; requires keys in the acceptance-gate env; brittle eval shape |
| Per-store learned thresholds (rolling histograms) | Adapts to each store's traffic profile | Needs days/weeks of data we don't have; cold-start case is exactly the demo case |

**Decision.** **Three rule types**, each pure-function, each fired through a
common `AnomalyService` orchestrator:

1. **`queue_spike`** — derives queue depth from the `BILLING_QUEUE_JOIN`
   minus `BILLING_QUEUE_ABANDON` event delta-stream, bucketed in 30 s slices.
   Fires when depth ≥ `threshold_depth` (default 7) holds for ≥
   `min_duration_s` (default 60 s). Severity escalates to **critical** at
   depth ≥ 12. Blips that don't satisfy `min_duration_s` are suppressed —
   the brief's "queue depth spiking" example is about sustained pressure,
   not transient pulses. Suggested action: *"Open additional billing
   counter or move staff to billing."*
2. **`conversion_drop`** — compares the current window's conversion rate to
   a baseline window's. Fires only when `current/baseline ≤ 0.5` AND the
   baseline has ≥ 20 visitors AND the current window has ≥ 5 visitors.
   Both denominators matter: without them, a quiet first-30-minutes-of-the-
   day would scream every morning. Severity is **critical** when the drop
   is ≥ 75 %. Suggested action: *"Investigate billing-queue length, staff
   availability, or stockouts on top SKUs."*
3. **`dead_zone`** — flags expected zones (pulled from
   `store_layouts.payload.zones[].zone_id`, with a Brigade Road fallback)
   that received zero customer visits while other zones logged ≥ 5 visits
   total. Staff visits explicitly do NOT save a zone from being marked dead
   — staff walking through doesn't reflect customer engagement. Suggested
   action: *"Check signage, lighting, or display layout in {zone}."*

The orchestrator runs all three rules, sorts results **critical → warn →
info** then by detection time, and returns. Empty-result is the common
case ("the store is fine"); the route returns 200 with `count=0` instead
of 404, matching the rubric's pro-200-with-zeros stance.

**Consequences.**

* **Each anomaly has an obvious next step.** The `suggested_action` text is
  short, imperative, and tied to the rule that fired — exactly what the
  rubric describes as "logical and meaningful".
* **No false-positive avalanche on quiet windows.** Both `conversion_drop`
  and `dead_zone` have denominator floors. Without them, the first hour of
  any store-day would fire every rule.
* **Time-bucketed queue depth** smooths over one-frame jitter without
  adding state. A 30 s bucket size is fine for the 1080p15fps target; even
  a single-camera setup produces several detections per bucket.
* **Severity is informative, not aspirational.** `critical` fires only on
  strong signals (≥ 12 in queue, ≥ 75 % conversion drop). Reviewers
  filtering by severity see real escalations, not noise.
* **Cold-start safe.** With the synthetic Brigade scenario alone, the
  baseline window is too small for `conversion_drop` to fire — that's
  correct behaviour. Tests demonstrate `dead_zone` and `queue_spike`
  firing on appropriately-shaped synthetic seeds.
