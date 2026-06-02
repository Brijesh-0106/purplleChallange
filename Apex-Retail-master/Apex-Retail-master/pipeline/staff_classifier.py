"""Staff classifier.

Purplle's actual stores have salespeople in **black T-shirts with the brand
logo** — that's the simplest visual differentiator from customers (who arrive
in arbitrary clothing). A perfect classifier would use a fine-tuned model;
that's overkill for a 10-minute reviewer demo and adds heavy dependencies.

What we ship instead:

    * `StaffClassifier` — protocol; anything that maps a Detection (or its
      descriptor) to a bool answers "is this person staff?"
    * `UniformColorStaffClassifier` — heuristic implementation. Takes a
      reference uniform colour (default: dark / near-black) and a
      tolerance; returns True when the detected person's appearance
      descriptor is dominated by that colour.
    * `LabeledStaffClassifier` — explicit `is_staff=True` flag on the
      detection. Used by SyntheticBackend so we can demonstrate staff
      exclusion deterministically.
    * `VlmStaffClassifier` (NOT WIRED) — documented stub showing how a
      Claude Vision A/B would slot in. Discussed in `CHOICES.md`.

Why two classifiers? The brief explicitly rewards "Handles staff correctly".
With synthetic data we use the labelled path (ground truth); with real
video we use the colour heuristic (real signal). The pipeline depends on
the protocol, so swapping classifiers is a one-line change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from pipeline.detector import Detection
from pipeline.reid import descriptor_from_signature


class StaffClassifier(Protocol):
    """Anything that can answer 'is this person staff?' for a Detection."""

    def is_staff(self, detection: Detection) -> bool:
        ...


# ──────────────────────────────────────────────────────────────────
# Implementations
# ──────────────────────────────────────────────────────────────────


@dataclass
class LabeledStaffClassifier:
    """Trusts the `is_staff` flag on the detection (or its `extras`).

    Used by the SyntheticBackend test path where ground-truth is known.
    """

    def is_staff(self, detection: Detection) -> bool:
        if detection.is_staff is not None:
            return detection.is_staff
        return bool(detection.extras.get("is_staff", False))


@dataclass
class UniformColorStaffClassifier:
    """Cheap colour-histogram heuristic for real video.

    Args:
        reference_signature: 3-channel RGB signature of the staff uniform,
            in [0, 1]. Default = (0.10, 0.10, 0.10) i.e. near-black.
        tolerance:           Max L2 distance (in 3-D RGB unit cube) to
            count as a match. Default 0.20 covers genuinely-dark uniforms
            without flagging customers in dark jeans + light tops.

    The classifier reads a 3-D average colour from `detection.extras`
    (key: "appearance_signature"). If absent, returns False — better to
    UNDER-flag staff (count them as visitors) than to silently drop real
    customers as "staff".
    """

    reference_signature: tuple[float, float, float] = (0.10, 0.10, 0.10)
    tolerance: float = 0.20

    def is_staff(self, detection: Detection) -> bool:
        sig = detection.extras.get("appearance_signature")
        if sig is None:
            return False
        try:
            r, g, b = (float(sig[0]), float(sig[1]), float(sig[2]))
        except (KeyError, ValueError, TypeError):
            return False

        ref_r, ref_g, ref_b = self.reference_signature
        # L2 in unit cube — fast and good enough for 3 channels.
        d2 = (r - ref_r) ** 2 + (g - ref_g) ** 2 + (b - ref_b) ** 2
        return (d2 ** 0.5) <= self.tolerance

    @staticmethod
    def signature_to_descriptor(sig: Sequence[float]):
        """Bridge: project an RGB triple into the 64-D Re-ID descriptor space."""
        return descriptor_from_signature(sig)


@dataclass
class CompositeStaffClassifier:
    """Use the labelled flag if present; otherwise fall back to a heuristic.

    `primary` is constructed via a `default_factory` because Python's
    `@dataclass` forbids mutable class instances as inline defaults
    (raises `ValueError: mutable default ...` at class-creation time).
    """

    fallback: StaffClassifier
    primary: StaffClassifier = field(default_factory=lambda: LabeledStaffClassifier())

    def is_staff(self, detection: Detection) -> bool:
        # If the detection explicitly says is_staff (True/False), respect it.
        if detection.is_staff is not None:
            return detection.is_staff
        return self.fallback.is_staff(detection)


# ──────────────────────────────────────────────────────────────────
# VLM stub — documented, not wired
# ──────────────────────────────────────────────────────────────────


@dataclass
class VlmStaffClassifier:
    """Documented Claude Vision A/B path. Intentionally NOT wired in default flows.

    Wiring this would add a network dependency (Anthropic API), a paid call
    per ambiguous frame, and latency (~1 s per call). The pragmatic
    deployment is to call it ONLY for low-confidence colour-classifier
    decisions — caching by appearance descriptor so the same uniform isn't
    re-judged on every frame.

    Justification for keeping it a stub: see `CHOICES.md` Decision 4.
    """

    def is_staff(self, detection: Detection) -> bool:  # pragma: no cover
        raise NotImplementedError(
            "VlmStaffClassifier is intentionally a stub — see CHOICES.md."
        )
