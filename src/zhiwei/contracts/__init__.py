"""Canonical contracts：JCS 语义 JSON、typed envelope、opaque id 与 UTC 时间。"""

from __future__ import annotations

from zhiwei.contracts.canonical import (
    CanonicalizationError,
    canonical_json,
    digest,
    digest_bytes,
    encode_bytes,
    encode_datetime,
    encode_decimal,
    encode_float,
    encode_integer,
    encode_text,
)
from zhiwei.contracts.envelope import Envelope, SchemaRegistry, UnknownSchemaError
from zhiwei.contracts.identifiers import IdentifierError, new_id
from zhiwei.contracts.time import utc_now

__all__ = [
    "CanonicalizationError",
    "Envelope",
    "IdentifierError",
    "SchemaRegistry",
    "UnknownSchemaError",
    "canonical_json",
    "digest",
    "digest_bytes",
    "encode_bytes",
    "encode_datetime",
    "encode_decimal",
    "encode_float",
    "encode_integer",
    "encode_text",
    "new_id",
    "utc_now",
]
