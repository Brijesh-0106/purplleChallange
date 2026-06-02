"""Event emitter — ships events from the pipeline to the API.

Two modes:
    HttpEmitter — POST batches to `/events/ingest` with retry + backoff.
    DryRunEmitter — collect events in memory and/or print them. No network.

Both implement the same minimal protocol so the runner can swap them based
on the CLI flag. The dry-run path is what makes pipeline tests run with
zero infra (per project working agreement: no Docker, no live API).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Iterable, Protocol

import httpx

from app.core.logging import get_logger
from app.schemas.events import Event

logger = get_logger(__name__)


class Emitter(Protocol):
    def emit(self, events: list[Event]) -> None:
        ...

    def flush(self) -> None:
        ...


# ─────────────────────────────────────────────────────────────────────
# Dry-run
# ─────────────────────────────────────────────────────────────────────


@dataclass
class DryRunEmitter:
    """Collects events for assertions / printing — never makes a network call."""

    print_to_stdout: bool = False
    collected: list[Event] = field(default_factory=list)

    def emit(self, events: list[Event]) -> None:
        self.collected.extend(events)
        if self.print_to_stdout:
            for ev in events:
                # One JSON object per line — friendly for `jq` and friends.
                print(ev.model_dump_json())

    def flush(self) -> None:
        # Nothing to flush; method exists for protocol compatibility.
        return None


# ─────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────


@dataclass
class HttpEmitter:
    """Batches events and POSTs to /events/ingest with bounded retries.

    Two internal buffers:
        _pending — events waiting to be batched and sent (FIFO).
        _failed  — events for which a send attempt was made and the API/network
                   refused (after retries). They live here forever unless the
                   caller pops them via `pending()` and decides what to do
                   (e.g. spill to disk on shutdown).

    Why two buffers? If a permanently-failing batch went back into `_pending`,
    the `emit()` draining loop would pick it up again and retry indefinitely
    on every subsequent call, blocking forward progress and looping inside
    `emit()` itself when batch_size <= unsent_count. Keeping failures in a
    parallel list lets new events flow through while operators inspect the
    dead-letter set out-of-band.
    """

    base_url: str = "http://localhost:8000"
    batch_size: int = 200
    timeout_s: float = 5.0
    max_retries: int = 3
    backoff_initial_s: float = 0.5

    _pending: list[Event] = field(default_factory=list, init=False)
    _failed: list[Event] = field(default_factory=list, init=False)
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    # ── Public API ───────────────────────────────────────────────────

    def emit(self, events: list[Event]) -> None:
        self._pending.extend(events)
        # Flush opportunistically when we have at least a full batch ready.
        while len(self._pending) >= self.batch_size:
            chunk = self._pending[: self.batch_size]
            self._pending = self._pending[self.batch_size :]
            self._post_with_retry(chunk)

    def flush(self) -> None:
        """Send any leftover events (final partial batch)."""
        if not self._pending:
            return
        chunk = self._pending
        self._pending = []
        self._post_with_retry(chunk)

    def pending(self) -> list[Event]:
        """All events the API has NOT acknowledged.

        Includes both the un-batched buffer and the dead-letter set so
        callers see the full unacked surface in one call (useful for
        spill-to-disk on shutdown).
        """
        return [*self._pending, *self._failed]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ── Internals ────────────────────────────────────────────────────

    def _client_get(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s)
        return self._client

    def _post_with_retry(self, events: list[Event]) -> None:
        """POST one batch with exponential backoff. Returns silently on success.

        On terminal failure (network exhausted, or 4xx that retrying won't fix),
        the batch is moved to `_failed` — NOT back into `_pending`.
        """
        body = {"events": [json.loads(e.model_dump_json()) for e in events]}
        url = f"{self.base_url.rstrip('/')}/events/ingest"

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client_get().post(url, json=body)
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    logger.error(
                        "emitter.network_failed",
                        attempts=attempt,
                        error=str(exc),
                        events=len(events),
                    )
                    self._failed.extend(events)
                    return
                self._sleep_backoff(attempt)
                continue

            # 503 → DB unavailable; retry per the API contract (idempotent).
            if resp.status_code == 503 and attempt < self.max_retries:
                logger.warning(
                    "emitter.api_503", attempt=attempt, hint="api says retry — DB unavailable"
                )
                self._sleep_backoff(attempt)
                continue

            if resp.status_code in (200, 202):
                summary = _safe_summary(resp.json())
                logger.info(
                    "emitter.batch_sent",
                    events=len(events),
                    accepted=summary.get("accepted"),
                    duplicates=summary.get("duplicates"),
                )
                return

            # 4xx (other than 503) → schema/cap error; do NOT retry blindly.
            # The data is malformed; surface via logs and dead-letter so the
            # operator can intervene without losing events on the floor.
            logger.error(
                "emitter.http_error",
                status_code=resp.status_code,
                body=_truncate(resp.text, 500),
                events=len(events),
            )
            self._failed.extend(events)
            return

    def _sleep_backoff(self, attempt: int) -> None:
        # 0.5s, 1.0s, 2.0s, …
        time.sleep(self.backoff_initial_s * (2 ** (attempt - 1)))


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


def _safe_summary(body: object) -> dict[str, int]:
    """Extract counters from the API's ingest response — never raise on shape."""
    if not isinstance(body, dict):
        return {}
    return {
        "accepted": int(body.get("accepted", 0)) if isinstance(body.get("accepted"), int) else 0,
        "duplicates": int(body.get("duplicates", 0)) if isinstance(body.get("duplicates"), int) else 0,
        "rejected": int(body.get("rejected", 0)) if isinstance(body.get("rejected"), int) else 0,
    }


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "…(truncated)"


def chunked(items: Iterable[Event], n: int) -> Iterable[list[Event]]:
    """Yield items in chunks of size n. Caller-friendly helper for ad-hoc batches."""
    chunk: list[Event] = []
    for it in items:
        chunk.append(it)
        if len(chunk) >= n:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
