"""S0-T6 RED: immutable eval registries, terminal completeness and sealing."""

from __future__ import annotations

import copy
import socket
import subprocess
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from zhiwei.evals.datasets import DatasetVersionSpec
from zhiwei.evals.domain import EvalMode, RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.executors.base import EvalExecutor
from zhiwei.evals.executors.empty import EmptyExecutor
from zhiwei.evals.executors.legacy import LegacyExecutor
from zhiwei.evals.legacy_assets import LegacyAssetInventory
from zhiwei.evals.runs import EvalRunState, EvalStateError
from zhiwei.evals.sealing import (
    SealVerificationError,
    build_sealed_artifact,
    verify_sealed_artifact,
)
from zhiwei.evals.suites import EvalSuiteVersionSpec

from zhiwei.contracts.canonical import digest_bytes

REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_ID = UUID("11111111-1111-4111-8111-111111111111")
SUITE_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
EVAL_RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
DIGESTS = {
    "code_digest": digest_bytes(b"code"),
    "config_digest": digest_bytes(b"config"),
    "schema_digest": digest_bytes(b"schema"),
}


def _unit(index: int) -> RegisteredUnit:
    return RegisteredUnit(sample_id=f"sample-{index}", unit_id=f"unit-{index}")


def _outcome(index: int, status: SampleStatus = SampleStatus.COMPLETED) -> SampleOutcome:
    return SampleOutcome(
        unit=_unit(index),
        status=status,
        result={"index": index, "status": status.value},
    )


def _state(*units: RegisteredUnit) -> EvalRunState:
    return EvalRunState.create(mode=EvalMode.FIXTURE, registered_units=units, **DIGESTS)


def test_eval_modes_are_explicit_and_do_not_conflate_fixture_with_live() -> None:
    assert {mode.value for mode in EvalMode} == {
        "fixture",
        "replay",
        "offline",
        "live",
        "shadow",
        "human",
    }
    assert EvalMode("fixture") is not EvalMode("live")
    with pytest.raises(ValueError):
        EvalMode("unknown")


def test_dataset_and_suite_versions_are_frozen_and_digest_bound() -> None:
    dataset = DatasetVersionSpec(
        dataset_id=DATASET_ID,
        version=1,
        content_digest=digest_bytes(b"dataset"),
        manifest_id=UUID("55555555-5555-4555-8555-555555555555"),
    )
    suite = EvalSuiteVersionSpec(
        suite_id=SUITE_ID,
        version=1,
        content_digest=digest_bytes(b"suite"),
        registered_units=(_unit(2), _unit(1)),
    )

    assert suite.registered_units == (_unit(1), _unit(2))
    with pytest.raises(ValidationError):
        dataset.version = 2
    with pytest.raises(ValidationError):
        suite.content_digest = digest_bytes(b"changed")
    with pytest.raises(ValidationError):
        DatasetVersionSpec(
            dataset_id=DATASET_ID,
            version=1,
            content_digest="not-a-digest",
            manifest_id=dataset.manifest_id,
        )


def test_suite_and_run_reject_duplicate_registered_units() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        EvalSuiteVersionSpec(
            suite_id=SUITE_ID,
            version=1,
            content_digest=digest_bytes(b"suite"),
            registered_units=(_unit(1), _unit(1)),
        )
    with pytest.raises(EvalStateError, match="duplicate"):
        _state(_unit(1), _unit(1))


def test_outcomes_are_terminal_digest_bound_and_cannot_escape_registry() -> None:
    state = _state(_unit(1))
    with pytest.raises(EvalStateError, match="registered"):
        state.record(_outcome(2))
    with pytest.raises(EvalStateError, match="terminal"):
        state.record(_outcome(1, SampleStatus.RUNNING))

    completed = state.record(_outcome(1))
    assert completed.outcomes[0].result_digest == digest_bytes(
        b'{"index":1,"status":"completed"}'
    )
    with pytest.raises(EvalStateError, match="already"):
        completed.record(_outcome(1))


@pytest.mark.parametrize(
    "status",
    [
        SampleStatus.COMPLETED,
        SampleStatus.FAILED,
        SampleStatus.REFUSED,
        SampleStatus.ERROR,
    ],
)
def test_every_registered_unit_terminal_status_counts_toward_completeness(
    status: SampleStatus,
) -> None:
    state = _state(_unit(1)).record(_outcome(1, status))
    assert state.is_complete
    assert state.pending_units == ()
    assert state.seal().status.value == "sealed"


def test_partial_can_resume_with_the_same_registry_but_cannot_seal() -> None:
    partial = _state(_unit(1), _unit(2)).record(_outcome(1)).pause()
    with pytest.raises(EvalStateError, match="terminal"):
        partial.seal()

    resumed = partial.resume()
    assert resumed.registered_units == partial.registered_units
    assert resumed.outcomes == partial.outcomes
    sealed = resumed.record(_outcome(2, SampleStatus.ERROR)).seal()
    assert sealed.status.value == "sealed"
    with pytest.raises(EvalStateError, match="sealed"):
        sealed.resume()


def test_sealed_artifact_is_deterministic_recomputable_and_tamper_evident() -> None:
    left = _state(_unit(2), _unit(1)).record(_outcome(2)).record(_outcome(1)).seal()
    right = _state(_unit(1), _unit(2)).record(_outcome(1)).record(_outcome(2)).seal()
    artifact_a, seal_digest_a = build_sealed_artifact(
        run_id=RUN_ID,
        eval_run_id=EVAL_RUN_ID,
        state=left,
        dataset_digest=digest_bytes(b"dataset"),
        dataset_manifest_id=UUID("55555555-5555-4555-8555-555555555555"),
        suite_digest=digest_bytes(b"suite"),
        migration_revision="0001_foundation",
        test_report_digest=digest_bytes(b"test report"),
        test_report_manifest_id=UUID("66666666-6666-4666-8666-666666666666"),
    )
    artifact_b, seal_digest_b = build_sealed_artifact(
        run_id=RUN_ID,
        eval_run_id=EVAL_RUN_ID,
        state=right,
        dataset_digest=digest_bytes(b"dataset"),
        dataset_manifest_id=UUID("55555555-5555-4555-8555-555555555555"),
        suite_digest=digest_bytes(b"suite"),
        migration_revision="0001_foundation",
        test_report_digest=digest_bytes(b"test report"),
        test_report_manifest_id=UUID("66666666-6666-4666-8666-666666666666"),
    )

    assert artifact_a == artifact_b
    assert seal_digest_a == seal_digest_b
    verified = verify_sealed_artifact(artifact_a.canonical_mapping(), seal_digest_a)
    assert verified.eval_run_id == EVAL_RUN_ID
    assert verified.code_digest == DIGESTS["code_digest"]

    tampered = copy.deepcopy(artifact_a.canonical_mapping())
    tampered["samples"][0]["status"] = "failed"
    with pytest.raises(SealVerificationError, match="digest"):
        verify_sealed_artifact(tampered, seal_digest_a)


@pytest.mark.asyncio
async def test_empty_executor_is_a_real_port_and_never_calls_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("empty executor must not call a model or network")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_network)
    monkeypatch.setattr(socket.socket, "sendto", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_network)
    monkeypatch.setattr(subprocess, "run", refuse_network)
    monkeypatch.setattr(subprocess, "Popen", refuse_network)
    executor = EmptyExecutor()
    assert isinstance(executor, EvalExecutor)
    outcome = await executor.execute(_unit(1))
    repeated = await executor.execute(_unit(1))
    assert repeated == outcome
    assert outcome.unit == _unit(1)
    assert outcome.status is SampleStatus.FAILED
    assert outcome.result == {"reason": "empty_executor"}
    assert outcome.result_digest == digest_bytes(b'{"reason":"empty_executor"}')


@pytest.mark.asyncio
async def test_legacy_executor_adapts_frozen_assets_without_rewriting_them() -> None:
    checksum_file = REPO_ROOT / "evals" / "CHECKSUMS.sha256"
    before = checksum_file.read_bytes()
    inventory = LegacyAssetInventory.load(REPO_ROOT / "evals")
    registry_keys = [
        (unit.sample_id, unit.unit_id) for unit in inventory.registered_units
    ]
    assert registry_keys == sorted(registry_keys)
    assert inventory.registered_units

    unit = inventory.registered_units[0]
    expected_digest, expected_path = before.decode("utf-8").splitlines()[0].split(maxsplit=1)
    outcome = await LegacyExecutor(inventory).execute(unit)
    assert unit == RegisteredUnit(sample_id=expected_path, unit_id="checksum")
    assert outcome.status is SampleStatus.COMPLETED
    assert outcome.result == {
        "path": expected_path,
        "content_digest": f"sha256:{expected_digest}",
        "verified": True,
    }
    assert checksum_file.read_bytes() == before
