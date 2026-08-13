"""Opaque external identifiers."""

from __future__ import annotations

import re
from uuid import UUID, uuid4

_PREFIX_PATTERN = re.compile(r"[a-z][a-z0-9]*")
_HEX_PATTERN = re.compile(r"[0-9a-f]{32}")


class IdentifierError(ValueError):
    """Raised when an external identifier is malformed or has the wrong type."""


def new_id() -> UUID:
    """Generate an opaque, non-time-ordered UUIDv4."""
    return uuid4()


def format_id(prefix: str, value: UUID) -> str:
    """Format a UUIDv4 with a lowercase resource-type prefix."""
    _validate_prefix(prefix)
    if not isinstance(value, UUID) or value.version != 4:
        raise IdentifierError("value must be a UUIDv4")
    return f"{prefix}_{value.hex}"


def parse_id(prefix: str, text: str) -> UUID:
    """Parse an external identifier, enforcing its expected resource prefix."""
    _validate_prefix(prefix)
    if not isinstance(text, str):
        raise IdentifierError("identifier must be a string")
    marker = f"{prefix}_"
    if not text.startswith(marker):
        raise IdentifierError(f"identifier must use the {prefix!r} prefix")
    raw = text[len(marker) :]
    if _HEX_PATTERN.fullmatch(raw) is None:
        raise IdentifierError("identifier payload must be 32 lowercase hexadecimal characters")
    value = UUID(hex=raw)
    if value.version != 4:
        raise IdentifierError("identifier must contain a UUIDv4")
    return value


def _validate_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or _PREFIX_PATTERN.fullmatch(prefix) is None:
        raise IdentifierError("prefix must start with a lowercase letter and contain only letters or digits")
