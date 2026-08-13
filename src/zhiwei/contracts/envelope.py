"""Typed schema/version envelopes with fail-closed schema resolution."""

from __future__ import annotations

import base64
import re
import struct
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel

from zhiwei.contracts.canonical import (
    CanonicalizationError,
    digest,
    encode_bytes,
    encode_datetime,
    encode_decimal,
    encode_float,
    encode_integer,
    encode_text,
)

PayloadT = TypeVar("PayloadT", bound=BaseModel)
_SCHEMA_ID_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)*")
_TYPE_TAG = "$zhiwei_type"
_TYPE_VALUE = "value"
_PAYLOAD_ENCODING = "zhiwei.typed.v1"


class UnknownSchemaError(LookupError):
    """Raised when no model is registered for a schema id and version."""


@dataclass(frozen=True, slots=True, init=False)
class Envelope(Generic[PayloadT]):
    """A typed payload bound to a stable schema id and positive version."""

    schema_id: str
    schema_version: int
    _payload: PayloadT = field(repr=False)
    _canonical_payload: object = field(repr=False)

    def __init__(self, schema_id: str, schema_version: int, payload: PayloadT) -> None:
        _validate_schema_key(schema_id, schema_version)
        if not isinstance(payload, BaseModel):
            raise ValueError("payload must be a pydantic BaseModel")
        payload_snapshot = payload.model_copy(deep=True)
        canonical_payload = _encode_typed_value(
            payload_snapshot.model_dump(mode="python", by_alias=True)
        )
        object.__setattr__(self, "schema_id", schema_id)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "_payload", payload_snapshot)
        object.__setattr__(self, "_canonical_payload", canonical_payload)

    @property
    def payload(self) -> PayloadT:
        """Return a detached typed copy so the envelope identity stays immutable."""
        return self._payload.model_copy(deep=True)

    def canonical_mapping(self) -> dict[str, object]:
        """Return the complete canonical identity of this envelope."""
        return {
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "payload_encoding": _PAYLOAD_ENCODING,
            "payload": deepcopy(self._canonical_payload),
        }

    @property
    def digest(self) -> str:
        """Return a digest covering schema identity, version, and payload."""
        return digest(self.canonical_mapping())


class SchemaRegistry:
    """An explicit, process-local mapping from schema keys to payload models."""

    def __init__(self) -> None:
        self._models: dict[tuple[str, int], type[BaseModel]] = {}

    def register(self, schema_id: str, schema_version: int, model: type[BaseModel]) -> None:
        """Register one schema key exactly once."""
        _validate_schema_key(schema_id, schema_version)
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise ValueError("model must be a pydantic BaseModel class")
        key = (schema_id, schema_version)
        if key in self._models:
            raise ValueError(f"schema is already registered: {schema_id}@{schema_version}")
        self._models[key] = model

    def resolve(self, schema_id: str, schema_version: int) -> type[BaseModel]:
        """Resolve a schema key or fail closed."""
        _validate_schema_key(schema_id, schema_version)
        try:
            return self._models[(schema_id, schema_version)]
        except KeyError as exc:
            raise UnknownSchemaError(f"unknown schema: {schema_id}@{schema_version}") from exc

    def decode(self, value: Mapping[str, object]) -> Envelope[BaseModel]:
        """Validate an envelope mapping against its registered payload model."""
        if not isinstance(value, Mapping):
            raise ValueError("envelope must be a mapping")
        raw_fields = {"schema_id", "schema_version", "payload"}
        typed_fields = raw_fields | {"payload_encoding"}
        if frozenset(value) not in {frozenset(raw_fields), frozenset(typed_fields)}:
            raise ValueError("envelope fields do not match a supported encoding")
        schema_id = value["schema_id"]
        schema_version = value["schema_version"]
        if not isinstance(schema_id, str):
            raise ValueError("schema_id must be a string")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ValueError("schema_version must be an integer")
        model = self.resolve(schema_id, schema_version)
        encoded_payload = value["payload"]
        if "payload_encoding" not in value:
            payload = model.model_validate(encoded_payload, strict=True, extra="forbid")
            return Envelope(schema_id=schema_id, schema_version=schema_version, payload=payload)
        if value["payload_encoding"] != _PAYLOAD_ENCODING:
            raise ValueError("unknown payload encoding")
        payload_value = _decode_typed_value(encoded_payload)
        payload = model.model_validate(payload_value, strict=False, extra="forbid")
        if (
            _encode_typed_value(payload.model_dump(mode="python", by_alias=True))
            != encoded_payload
        ):
            raise ValueError("payload is not in canonical typed form")
        return Envelope(schema_id=schema_id, schema_version=schema_version, payload=payload)

    def registered(self) -> tuple[tuple[str, int], ...]:
        """Enumerate registered schema keys in deterministic order."""
        return tuple(sorted(self._models))


def _validate_schema_key(schema_id: str, schema_version: int) -> None:
    if not isinstance(schema_id, str) or _SCHEMA_ID_PATTERN.fullmatch(schema_id) is None:
        raise ValueError("schema_id must be a lowercase dotted name")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version <= 0:
        raise ValueError("schema_version must be a positive integer")


def _encode_typed_value(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _encode_typed_value(value.value)
    if isinstance(value, str):
        return encode_text(value)
    if isinstance(value, int):
        return _typed("integer", encode_integer(value))
    if isinstance(value, float):
        return _typed("float", encode_float(value))
    if isinstance(value, Decimal):
        return _typed("decimal", encode_decimal(value))
    if isinstance(value, bytes):
        return _typed("bytes", encode_bytes(value))
    if isinstance(value, datetime):
        return _typed("datetime", encode_datetime(value))
    if isinstance(value, UUID):
        return _typed("uuid", str(value))
    if isinstance(value, list):
        return _typed("list", [_encode_typed_value(item) for item in value])
    if isinstance(value, tuple):
        return _typed("tuple", [_encode_typed_value(item) for item in value])
    if isinstance(value, Mapping):
        encoded: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("typed payload object keys must be strings")
            normalized_key = encode_text(key)
            if normalized_key in encoded:
                raise CanonicalizationError(
                    f"multiple typed payload keys normalize to {normalized_key!r}"
                )
            encoded[normalized_key] = _encode_typed_value(item)
        return _typed("object", encoded)
    raise CanonicalizationError(f"unsupported typed payload value: {type(value).__name__}")


def _decode_typed_value(value: object) -> object:
    if value is None or isinstance(value, (bool, str)):
        return value
    if not isinstance(value, Mapping) or set(value) != {_TYPE_TAG, _TYPE_VALUE}:
        raise ValueError("invalid typed payload value")
    kind = value[_TYPE_TAG]
    encoded = value[_TYPE_VALUE]
    if not isinstance(kind, str):
        raise ValueError("typed payload tag must be a string")
    if kind == "object":
        if not isinstance(encoded, Mapping):
            raise ValueError("typed object payload must be a mapping")
        return {key: _decode_typed_value(item) for key, item in encoded.items()}
    if kind in {"list", "tuple"}:
        if not isinstance(encoded, list):
            raise ValueError(f"typed {kind} payload must be a list")
        items = [_decode_typed_value(item) for item in encoded]
        return items if kind == "list" else tuple(items)
    if not isinstance(encoded, str):
        raise ValueError(f"typed {kind} payload must be a string")
    if kind == "integer":
        decoded_integer = int(encoded)
        if encode_integer(decoded_integer) != encoded:
            raise ValueError("integer payload is not canonical")
        return decoded_integer
    if kind == "float":
        try:
            decoded_float = struct.unpack(">d", bytes.fromhex(encoded))[0]
        except (ValueError, struct.error) as exc:
            raise ValueError("float payload is not binary64 hexadecimal") from exc
        if encode_float(decoded_float) != encoded:
            raise ValueError("float payload is not canonical")
        return decoded_float
    if kind == "decimal":
        decoded_decimal = Decimal(encoded)
        if encode_decimal(decoded_decimal) != encoded:
            raise ValueError("decimal payload is not canonical")
        return decoded_decimal
    if kind == "bytes":
        try:
            decoded_bytes = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
        except ValueError as exc:
            raise ValueError("bytes payload is not base64url") from exc
        if encode_bytes(decoded_bytes) != encoded:
            raise ValueError("bytes payload is not canonical")
        return decoded_bytes
    if kind == "datetime":
        try:
            decoded_datetime = datetime.fromisoformat(encoded)
        except ValueError as exc:
            raise ValueError("datetime payload is not ISO-8601") from exc
        if encode_datetime(decoded_datetime) != encoded:
            raise ValueError("datetime payload is not canonical UTC")
        return decoded_datetime
    if kind == "uuid":
        try:
            decoded_uuid = UUID(encoded)
        except ValueError as exc:
            raise ValueError("uuid payload is malformed") from exc
        if str(decoded_uuid) != encoded:
            raise ValueError("uuid payload is not canonical")
        return decoded_uuid
    raise ValueError(f"unknown typed payload tag: {kind}")


def _typed(kind: str, value: object) -> dict[str, object]:
    return {_TYPE_TAG: kind, _TYPE_VALUE: value}
