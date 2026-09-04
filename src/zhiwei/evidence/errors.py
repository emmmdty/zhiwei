"""S6 Evidence domain errors.

Fail closed: every invalid input raises a typed exception.
"""

from __future__ import annotations


class EvidenceError(RuntimeError):
    """Base class for all evidence contract errors."""


class ClaimLevelViolationError(EvidenceError):
    """A claim type is not supported by the given reproducibility level.

    Fact/Quote require replayable or copy_frozen; reference_only only supports
    Inference/Recommendation.
    """


class EvidenceRefValidationError(EvidenceError):
    """An EvidenceRef has invalid fields for its variant or reproducibility level."""


class CopyFrozenBindingError(EvidenceError):
    """A copy_frozen EvidenceRef is missing required binding fields."""


class BundleValidationError(EvidenceError):
    """An EvidenceBundle fails structural or semantic validation."""
