"""Canonical value encoders and digest helpers."""

from __future__ import annotations

import base64
import hashlib
import math
import struct
import unicodedata
from datetime import datetime
from decimal import Decimal
from typing import TypeAlias

import rfc8785

from zhiwei.contracts.time import ensure_utc

CanonicalValue: TypeAlias = (
    bool | int | float | str | list["CanonicalValue"] | dict[str, "CanonicalValue"] | None
)

_SAFE_INTEGER_MAX = 2**53 - 1


class CanonicalizationError(ValueError):
    """Raised when a value has no unambiguous canonical representation."""


def encode_text(value: str) -> str:
    """Normalize text to the Unicode NFC form used by canonical values."""
    if not isinstance(value, str):
        raise CanonicalizationError("encode_text requires str")
    return unicodedata.normalize("NFC", value)


def encode_integer(value: int) -> str:
    """Encode an arbitrary-precision integer without loss."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanonicalizationError("encode_integer requires int, not bool")
    return str(value)


def encode_decimal(value: Decimal) -> str:
    """Expand a finite Decimal to non-exponent notation while preserving scale."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CanonicalizationError("encode_decimal requires a finite Decimal")
    return format(value, "f")


def encode_float(value: float) -> str:
    """Encode a finite IEEE-754 binary64 value as lowercase hexadecimal bits."""
    if not isinstance(value, float) or not math.isfinite(value):
        raise CanonicalizationError("encode_float requires a finite float")
    return struct.pack(">d", value).hex()


def encode_bytes(value: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    if not isinstance(value, bytes):
        raise CanonicalizationError("encode_bytes requires bytes")
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def encode_datetime(value: datetime) -> str:
    """Encode an aware datetime in ISO-8601 after normalizing it to UTC."""
    if not isinstance(value, datetime):
        raise CanonicalizationError("encode_datetime requires datetime")
    try:
        return ensure_utc(value).isoformat()
    except ValueError as exc:
        raise CanonicalizationError(str(exc)) from exc


def canonical_json(value: object) -> bytes:
    """Serialize JSON data with NFC normalization and RFC 8785/JCS semantics."""
    normalized = _normalize_json(value, path="$")
    try:
        return rfc8785.dumps(normalized)
    except (rfc8785.CanonicalizationError, UnicodeError) as exc:
        raise CanonicalizationError(str(exc)) from exc


def digest(value: object) -> str:
    """Return the SHA-256 digest of a value's canonical JSON bytes."""
    return digest_bytes(canonical_json(value))


def digest_bytes(value: bytes) -> str:
    """Return a lowercase, algorithm-prefixed SHA-256 digest."""
    if not isinstance(value, bytes):
        raise CanonicalizationError("digest_bytes requires bytes")
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _normalize_json(value: object, *, path: str) -> CanonicalValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return encode_text(value)
    if isinstance(value, int):
        if abs(value) > _SAFE_INTEGER_MAX:
            raise CanonicalizationError(
                f"{path}: integer exceeds the JCS safe domain; use encode_integer"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"{path}: non-finite float is not representable")
        return value
    if isinstance(value, list):
        return [_normalize_json(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        normalized: dict[str, CanonicalValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: object keys must be strings")
            normalized_key = encode_text(key)
            if normalized_key in normalized:
                raise CanonicalizationError(
                    f"{path}: multiple object keys normalize to {normalized_key!r}"
                )
            normalized[normalized_key] = _normalize_json(item, path=f"{path}.{normalized_key}")
        return normalized
    if isinstance(value, Decimal):
        raise CanonicalizationError(f"{path}: Decimal requires encode_decimal")
    if isinstance(value, bytes):
        raise CanonicalizationError(f"{path}: bytes require encode_bytes")
    if isinstance(value, datetime):
        raise CanonicalizationError(f"{path}: datetime requires encode_datetime")
    raise CanonicalizationError(f"{path}: unsupported type {type(value).__name__}")
