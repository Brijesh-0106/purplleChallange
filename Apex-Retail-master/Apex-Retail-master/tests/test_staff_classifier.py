# PROMPT (Claude, Batch 5):
#   "Test the staff classifiers: (1) LabeledStaffClassifier respects
#    is_staff flag, (2) UniformColorStaffClassifier matches near-black
#    signatures within tolerance, (3) UniformColor falls back to False
#    when no signature is present (under-flag, never over-flag),
#    (4) CompositeStaffClassifier prefers explicit flag over fallback."
#
# CHANGES MADE:
#   - Used real Visitor signatures (the brigade staff is (0.10, 0.10, 0.10),
#     the brigade buyer is (0.85, 0.20, 0.45)) so test vocabulary tracks runtime.

from __future__ import annotations

from pipeline.detector import Detection
from pipeline.staff_classifier import (
    CompositeStaffClassifier,
    LabeledStaffClassifier,
    UniformColorStaffClassifier,
)


def _det(is_staff=None, sig=None) -> Detection:
    extras = {}
    if sig is not None:
        extras["appearance_signature"] = list(sig)
    return Detection(
        track_id="T",
        bbox=(0.0, 0.0, 10.0, 10.0),
        confidence=0.9,
        is_staff=is_staff,
        extras=extras,
    )


# ── LabeledStaffClassifier ──────────────────────────────────────────


def test_labeled_classifier_true_when_flagged() -> None:
    assert LabeledStaffClassifier().is_staff(_det(is_staff=True)) is True


def test_labeled_classifier_false_by_default() -> None:
    assert LabeledStaffClassifier().is_staff(_det()) is False


# ── UniformColorStaffClassifier ─────────────────────────────────────


def test_uniform_color_matches_near_black() -> None:
    cls = UniformColorStaffClassifier()
    # Brigade staff signature, exact reference match
    assert cls.is_staff(_det(sig=(0.10, 0.10, 0.10))) is True
    # Slightly off but within tolerance
    assert cls.is_staff(_det(sig=(0.18, 0.10, 0.10))) is True


def test_uniform_color_does_not_match_bright_top() -> None:
    cls = UniformColorStaffClassifier()
    # Brigade buyer signature (bright pink)
    assert cls.is_staff(_det(sig=(0.85, 0.20, 0.45))) is False


def test_uniform_color_under_flags_when_signature_missing() -> None:
    """No signature → False. Better to under-flag staff than drop real customers."""
    cls = UniformColorStaffClassifier()
    assert cls.is_staff(_det()) is False


# ── CompositeStaffClassifier ────────────────────────────────────────


def test_composite_explicit_flag_wins() -> None:
    cls = CompositeStaffClassifier(fallback=UniformColorStaffClassifier())
    # Black uniform but is_staff=False overrides to False.
    assert cls.is_staff(_det(is_staff=False, sig=(0.10, 0.10, 0.10))) is False
    # Bright top but is_staff=True overrides to True.
    assert cls.is_staff(_det(is_staff=True, sig=(0.85, 0.20, 0.45))) is True


def test_composite_falls_back_when_no_flag() -> None:
    cls = CompositeStaffClassifier(fallback=UniformColorStaffClassifier())
    assert cls.is_staff(_det(sig=(0.10, 0.10, 0.10))) is True
    assert cls.is_staff(_det(sig=(0.85, 0.20, 0.45))) is False
