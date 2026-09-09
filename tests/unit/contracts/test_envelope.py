"""S0-T2 RED：schema/version envelope。

spec §4 的不变量："schema/version 未知时 fail closed"。envelope 是这条不变量的落点：
任何跨进程/跨时间边界的结构都带 `schema_id` + `schema_version`，读到不认识的组合就拒绝，
而不是尽力而为地解析。

注册表刻意不做成模块级单例——全局注册表意味着"哪些 schema 可用"取决于 import 顺序，
测试之间会互相污染，生产上则无法回答"这个进程当时认得哪些 schema"。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from enum import Enum, IntEnum, StrEnum
from typing import Any

import pytest
from pydantic import BaseModel, Field, RootModel

from zhiwei.contracts.canonical import digest
from zhiwei.contracts.envelope import (
    Envelope,
    SchemaRegistry,
    UnknownSchemaError,
)


class RunStarted(BaseModel):
    run_id: str
    reason: str


class RunFinished(BaseModel):
    run_id: str
    terminal: bool


class UntypedValue(BaseModel):
    value: Any


class RunPhase(StrEnum):
    STARTED = "started"


class Priority(IntEnum):
    HIGH = 1


class Outcome(Enum):
    COMPLETE = "complete"


class EnumPayload(BaseModel):
    phase: RunPhase
    priority: Priority = Priority.HIGH
    outcome: Outcome = Outcome.COMPLETE


class TuplePayload(BaseModel):
    coordinates: tuple[int, int]


class ScalarPayload(RootModel[int]):
    pass


class AliasedPayload(BaseModel):
    internal_name: str = Field(alias="externalName")


@pytest.fixture
def registry() -> SchemaRegistry:
    reg = SchemaRegistry()
    reg.register("run.started", 1, RunStarted)
    return reg


# --------------------------------------------------------------------------- 身份与 digest


def test_envelope_digest_covers_the_payload() -> None:
    a = Envelope(schema_id="run.started", schema_version=1, payload=RunStarted(run_id="r1", reason="x"))
    b = Envelope(schema_id="run.started", schema_version=1, payload=RunStarted(run_id="r1", reason="y"))
    assert a.digest != b.digest


def test_envelope_digest_covers_the_schema_version() -> None:
    """版本是身份的一部分。

    同样的字段在 v1 和 v2 下可能含义不同；如果 digest 不含版本，一次 schema 演进就能让
    两条语义不同的记录拥有同一个 digest。
    """
    payload = RunStarted(run_id="r1", reason="x")
    v1 = Envelope(schema_id="run.started", schema_version=1, payload=payload)
    v2 = Envelope(schema_id="run.started", schema_version=2, payload=payload)
    assert v1.digest != v2.digest


def test_envelope_digest_covers_the_schema_id() -> None:
    payload = RunStarted(run_id="r1", reason="x")
    left = Envelope(schema_id="run.started", schema_version=1, payload=payload)
    right = Envelope(schema_id="run.restarted", schema_version=1, payload=payload)
    assert left.digest != right.digest


def test_envelope_digest_is_the_canonical_digest_of_its_own_serialization() -> None:
    """envelope 的 digest 不能是另一套自制哈希——必须复用 canonical 层。"""
    envelope = Envelope(
        schema_id="run.started", schema_version=1, payload=RunStarted(run_id="r1", reason="x")
    )
    assert envelope.digest == digest(envelope.canonical_mapping())


def test_envelope_digest_is_stable_across_equal_instances() -> None:
    make = lambda: Envelope(  # noqa: E731
        schema_id="run.started", schema_version=1, payload=RunStarted(run_id="r1", reason="x")
    )
    assert make().digest == make().digest


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (0.0, -0.0),
        (1, 1.0),
        (b"abc", "abc"),
    ],
)
def test_envelope_digest_preserves_runtime_value_types(left: Any, right: Any) -> None:
    make = lambda value: Envelope(  # noqa: E731
        schema_id="typed.value", schema_version=1, payload=UntypedValue(value=value)
    )
    assert make(left).digest != make(right).digest


def test_envelope_digest_normalizes_equivalent_datetimes_to_utc() -> None:
    east_eight = timezone(timedelta(hours=8))
    utc_value = UntypedValue(value=datetime(2026, 8, 13, 12, 0, tzinfo=UTC))
    offset_value = UntypedValue(value=datetime(2026, 8, 13, 20, 0, tzinfo=east_eight))
    left = Envelope(schema_id="typed.value", schema_version=1, payload=utc_value)
    right = Envelope(schema_id="typed.value", schema_version=1, payload=offset_value)
    assert left.digest == right.digest


def test_list_and_tuple_have_distinct_typed_digests() -> None:
    as_list = Envelope(
        schema_id="typed.value", schema_version=1, payload=UntypedValue(value=[1, 2])
    )
    as_tuple = Envelope(
        schema_id="typed.value", schema_version=1, payload=UntypedValue(value=(1, 2))
    )
    assert as_list.digest != as_tuple.digest


def test_canonical_mapping_declares_its_payload_encoding() -> None:
    envelope = Envelope(
        schema_id="run.started",
        schema_version=1,
        payload=RunStarted(run_id="r1", reason="x"),
    )
    assert envelope.canonical_mapping()["payload_encoding"] == "zhiwei.typed.v1"


# --------------------------------------------------------------------------- 不可变


def test_envelope_is_immutable() -> None:
    envelope = Envelope(
        schema_id="run.started", schema_version=1, payload=RunStarted(run_id="r1", reason="x")
    )
    with pytest.raises((AttributeError, TypeError, ValueError)):
        envelope.schema_version = 2  # type: ignore[misc]


def test_envelope_snapshots_the_source_payload() -> None:
    payload = RunStarted(run_id="r1", reason="original")
    envelope = Envelope(schema_id="run.started", schema_version=1, payload=payload)
    original_digest = envelope.digest

    payload.reason = "mutated"

    assert envelope.payload.reason == "original"
    assert envelope.digest == original_digest


def test_envelope_does_not_expose_its_mutable_payload() -> None:
    envelope = Envelope(
        schema_id="run.started",
        schema_version=1,
        payload=RunStarted(run_id="r1", reason="original"),
    )
    original_digest = envelope.digest

    detached_payload = envelope.payload
    detached_payload.reason = "mutated"

    assert envelope.payload.reason == "original"
    assert envelope.digest == original_digest


@pytest.mark.parametrize("version", [0, -1])
def test_schema_version_must_be_positive(version: int) -> None:
    with pytest.raises(ValueError):
        Envelope(
            schema_id="run.started", schema_version=version, payload=RunStarted(run_id="r", reason="x")
        )


@pytest.mark.parametrize("schema_id", ["", " ", "Run.Started", "run started"])
def test_schema_id_must_be_a_well_formed_lowercase_dotted_name(schema_id: str) -> None:
    """schema_id 是长期标识符，大小写/空格变体会在几个月后变成两个"同一个" schema。"""
    with pytest.raises(ValueError):
        Envelope(schema_id=schema_id, schema_version=1, payload=RunStarted(run_id="r", reason="x"))


# --------------------------------------------------------------------------- 注册表 fail closed


def test_resolve_known_schema(registry: SchemaRegistry) -> None:
    assert registry.resolve("run.started", 1) is RunStarted


def test_unknown_schema_id_is_rejected(registry: SchemaRegistry) -> None:
    with pytest.raises(UnknownSchemaError):
        registry.resolve("run.finished", 1)


def test_unknown_schema_version_is_rejected(registry: SchemaRegistry) -> None:
    """已知 id 的未知版本同样拒绝——"应该向后兼容吧"是这里最贵的假设。"""
    with pytest.raises(UnknownSchemaError):
        registry.resolve("run.started", 2)


def test_decode_rejects_unknown_schema(registry: SchemaRegistry) -> None:
    with pytest.raises(UnknownSchemaError):
        registry.decode(
            {"schema_id": "run.finished", "schema_version": 1, "payload": {"run_id": "r", "terminal": True}}
        )


def test_decode_returns_a_typed_payload(registry: SchemaRegistry) -> None:
    envelope = registry.decode(
        {"schema_id": "run.started", "schema_version": 1, "payload": {"run_id": "r1", "reason": "x"}}
    )
    assert isinstance(envelope.payload, RunStarted)
    assert envelope.payload.run_id == "r1"


def test_decode_rejects_payload_that_does_not_match_the_registered_model(
    registry: SchemaRegistry,
) -> None:
    with pytest.raises(ValueError):
        registry.decode({"schema_id": "run.started", "schema_version": 1, "payload": {"run_id": "r1"}})


def test_decode_rejects_unknown_payload_fields(registry: SchemaRegistry) -> None:
    with pytest.raises(ValueError):
        registry.decode(
            {
                "schema_id": "run.started",
                "schema_version": 1,
                "payload": {"run_id": "r1", "reason": "x", "unknown": True},
            }
        )


def test_decode_rejects_missing_envelope_fields(registry: SchemaRegistry) -> None:
    with pytest.raises(ValueError):
        registry.decode({"schema_id": "run.started", "payload": {"run_id": "r1", "reason": "x"}})


def test_decode_round_trips_through_canonical_mapping(registry: SchemaRegistry) -> None:
    original = Envelope(
        schema_id="run.started", schema_version=1, payload=RunStarted(run_id="r1", reason="x")
    )
    assert registry.decode(original.canonical_mapping()).digest == original.digest


def test_decode_rejects_a_missing_outer_typed_tag() -> None:
    registry = SchemaRegistry()
    registry.register("typed.value", 1, UntypedValue)
    original = Envelope(
        schema_id="typed.value", schema_version=1, payload=UntypedValue(value=1)
    )
    malformed = original.canonical_mapping()
    typed_payload = malformed["payload"]
    assert isinstance(typed_payload, dict)
    malformed["payload"] = typed_payload["value"]

    with pytest.raises(ValueError):
        registry.decode(malformed)


def test_typed_values_round_trip_through_canonical_mapping() -> None:
    registry = SchemaRegistry()
    registry.register("typed.value", 1, UntypedValue)
    original = Envelope(
        schema_id="typed.value",
        schema_version=1,
        payload=UntypedValue(value=[1, -0.0, b"abc", datetime(2026, 8, 13, 12, 0, tzinfo=UTC)]),
    )
    decoded = registry.decode(original.canonical_mapping())
    assert decoded.digest == original.digest
    assert isinstance(decoded.payload, UntypedValue)
    assert decoded.payload.value == original.payload.value


@pytest.mark.parametrize(
    ("schema_id", "model", "payload"),
    [
        ("typed.tuple", TuplePayload, TuplePayload(coordinates=(1, 2))),
        ("typed.enum", EnumPayload, EnumPayload(phase=RunPhase.STARTED)),
        ("typed.scalar", ScalarPayload, ScalarPayload(7)),
        (
            "typed.alias",
            AliasedPayload,
            AliasedPayload.model_validate({"externalName": "value"}),
        ),
    ],
)
def test_supported_pydantic_shapes_round_trip(
    schema_id: str, model: type[BaseModel], payload: BaseModel
) -> None:
    registry = SchemaRegistry()
    registry.register(schema_id, 1, model)
    original = Envelope(schema_id=schema_id, schema_version=1, payload=payload)
    decoded = registry.decode(original.canonical_mapping())
    assert decoded.payload == payload
    assert decoded.digest == original.digest


# --------------------------------------------------------------------------- 注册表无全局状态


def test_registries_are_independent(registry: SchemaRegistry) -> None:
    other = SchemaRegistry()
    with pytest.raises(UnknownSchemaError):
        other.resolve("run.started", 1)


def test_registering_the_same_key_twice_is_rejected(registry: SchemaRegistry) -> None:
    """同一个 (id, version) 不得被重新绑定——那等于事后改写一个已冻结的契约。"""
    with pytest.raises(ValueError):
        registry.register("run.started", 1, RunFinished)


def test_registering_the_same_key_with_the_same_model_is_also_rejected(
    registry: SchemaRegistry,
) -> None:
    """即使模型相同也拒绝：重复注册通常意味着两处代码都以为自己是所有者。"""
    with pytest.raises(ValueError):
        registry.register("run.started", 1, RunStarted)


def test_different_versions_of_one_schema_coexist(registry: SchemaRegistry) -> None:
    registry.register("run.started", 2, RunFinished)
    assert registry.resolve("run.started", 1) is RunStarted
    assert registry.resolve("run.started", 2) is RunFinished


def test_registered_schemas_are_enumerable(registry: SchemaRegistry) -> None:
    """doctor 与 Gate 报告要能列出"这个进程认得哪些 schema"。"""
    assert ("run.started", 1) in set(registry.registered())
