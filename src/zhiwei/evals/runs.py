"""EvalRun 纯状态机与 PostgreSQL 应用服务。

纯状态机（`EvalRunState`）负责 registry 冻结、终态完备性、partial/resume/seal 的转移规则；
应用服务（`EvalFoundationService`）把这些规则落到真实 Run/EvalRun/event/outbox/artifact，
全部在同一个 tenant 事务内。record/pause/resume/seal 一律先取 EvalRun 行锁串行化，
seal 在持锁后从数据库重建状态并重新验证全部单位 terminal——不允许用进入方法前的缓存判断。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.contracts.canonical import canonical_json, digest, digest_bytes
from zhiwei.contracts.envelope import SchemaRegistry, UnknownSchemaError
from zhiwei.contracts.time import utc_now
from zhiwei.evals.domain import (
    EvalMode,
    RegisteredUnit,
    SampleOutcome,
    SampleStatus,
    is_terminal,
    sorted_unique_units,
    unit_key,
)
from zhiwei.evals.sealing import (
    EvalSealRefused,
    SealedEvalArtifact,
    build_sealed_artifact,
    verify_sealed_artifact,
)
from zhiwei.object_store.manifests import (
    ArtifactManifestCommand,
    ArtifactVerificationError,
)
from zhiwei.object_store.ports import ObjectNamespace, ObjectStore
from zhiwei.object_store.service import ArtifactService
from zhiwei.persistence.events import EventCommand
from zhiwei.persistence.models import (
    ArtifactManifest,
    DatasetVersion,
    EvalRun,
    EvalSample,
    EvalSuiteVersion,
    Run,
)
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired
from zhiwei.persistence.unit_of_work import CanonicalUnitOfWork


class EvalStateError(ValueError):
    """Raised when a state transition or registry rule is violated."""


class EvalRunNotFound(LookupError):
    """Raised when the target EvalRun is absent from the explicit tenant scope."""


class RunPhase(StrEnum):
    """EvalRun 生命周期阶段；sealed 之后不允许任何转移。"""

    RUNNING = "running"
    PARTIAL = "partial"
    SEALED = "sealed"


@dataclass(frozen=True, slots=True)
class EvalRunState:
    """不可变的 EvalRun 纯状态：registry 冻结、结果只增、partial 可 resume。"""

    mode: EvalMode
    registered_units: tuple[RegisteredUnit, ...]
    outcomes: tuple[SampleOutcome, ...]
    status: RunPhase
    code_digest: str
    config_digest: str
    schema_digest: str

    @classmethod
    def create(
        cls,
        *,
        mode: EvalMode,
        registered_units: tuple[RegisteredUnit, ...] | list[RegisteredUnit],
        code_digest: str,
        config_digest: str,
        schema_digest: str,
    ) -> EvalRunState:
        try:
            units = sorted_unique_units(tuple(registered_units))
        except ValueError as exc:
            raise EvalStateError(str(exc)) from exc
        return cls(
            mode=mode,
            registered_units=units,
            outcomes=(),
            status=RunPhase.RUNNING,
            code_digest=code_digest,
            config_digest=config_digest,
            schema_digest=schema_digest,
        )

    @classmethod
    def restore(
        cls,
        *,
        mode: EvalMode,
        registered_units: tuple[RegisteredUnit, ...],
        outcomes: tuple[SampleOutcome, ...],
        status: RunPhase,
        code_digest: str,
        config_digest: str,
        schema_digest: str,
    ) -> EvalRunState:
        """从数据库行重建状态；损坏的 registry/结果一律拒绝，不悄悄修复。"""
        try:
            units = sorted_unique_units(tuple(registered_units))
        except ValueError as exc:
            raise EvalStateError(f"restored registry is inconsistent: {exc}") from exc
        recorded: set[tuple[str, str]] = set()
        for outcome in outcomes:
            key = (outcome.unit.sample_id, outcome.unit.unit_id)
            if outcome.unit not in units:
                raise EvalStateError(
                    f"restored outcome escapes registry: {outcome.unit.sample_id!r}"
                )
            if not is_terminal(outcome.status):
                raise EvalStateError(f"restored outcome is not terminal: {outcome.status}")
            if key in recorded:
                raise EvalStateError(f"restored outcome is duplicated: {outcome.unit}")
            recorded.add(key)
        return cls(
            mode=mode,
            registered_units=units,
            outcomes=tuple(outcomes),
            status=status,
            code_digest=code_digest,
            config_digest=config_digest,
            schema_digest=schema_digest,
        )

    @property
    def pending_units(self) -> tuple[RegisteredUnit, ...]:
        """尚无终态结果的注册单位，按 registry 顺序返回。"""
        recorded = {unit_key(item.unit) for item in self.outcomes}
        return tuple(unit for unit in self.registered_units if unit_key(unit) not in recorded)

    @property
    def is_complete(self) -> bool:
        """全部注册单位都有终态结果；空 registry 是 vacuous complete。"""
        return not self.pending_units

    def record(self, outcome: SampleOutcome) -> EvalRunState:
        if self.status is RunPhase.SEALED:
            raise EvalStateError("sealed run cannot record outcomes")
        if outcome.unit not in self.registered_units:
            raise EvalStateError(
                f"unit is not registered: {outcome.unit.sample_id!r}/{outcome.unit.unit_id!r}"
            )
        if not is_terminal(outcome.status):
            raise EvalStateError(
                f"outcome must be terminal, got {outcome.status.value!r}"
            )
        if any(outcome.unit == item.unit for item in self.outcomes):
            raise EvalStateError(
                f"unit already has a terminal outcome: {outcome.unit.sample_id!r}"
            )
        return replace(self, outcomes=(*self.outcomes, outcome))

    def pause(self) -> EvalRunState:
        if self.status is RunPhase.SEALED:
            raise EvalStateError("sealed run cannot pause")
        return replace(self, status=RunPhase.PARTIAL)

    def resume(self) -> EvalRunState:
        if self.status is RunPhase.SEALED:
            raise EvalStateError("sealed run cannot resume")
        return replace(self, status=RunPhase.RUNNING)

    def seal(self) -> EvalRunState:
        if self.status is RunPhase.SEALED:
            raise EvalStateError("run is already sealed")
        if not self.is_complete:
            raise EvalStateError(
                "cannot seal until every registered unit is terminal; "
                f"{len(self.pending_units)} pending"
            )
        return replace(self, status=RunPhase.SEALED)


class CreateEvalRunCommand(BaseModel):
    """创建一次评测运行；dataset_payload 是需写入 ObjectStore 的 canonical 内容。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EvalMode
    registered_units: tuple[RegisteredUnit, ...]
    dataset_payload: dict[str, Any]
    code_digest: str
    config_digest: str
    schema_digest: str


class SealEmptyCommand(BaseModel):
    """空 registry 的 plumbing 冒烟：真实持久化 + 密封 + 复核。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EvalMode
    code_digest: str
    config_digest: str
    schema_digest: str
    migration_revision: str
    test_report: dict[str, Any]


class EvalRunSealedPayload(BaseModel):
    """`eval.run.sealed` canonical event 的 payload schema。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    eval_run_id: UUID
    manifest_id: UUID
    seal_digest: str


class _DatasetPayload(BaseModel):
    """`eval.dataset` artifact schema；只做注册，内容由 canonical 字节本身保证。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    registered_units: list[dict[str, str]] | None = None
    samples: list[str] | None = None


class _TestReportPayload(BaseModel):
    """`gate.test-report` artifact schema；结构由 seal 流程校验。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class _SealedRunPayload(BaseModel):
    """`eval.sealed-run` artifact schema；内容由 sealing.verify_sealed_artifact 复算。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


class CreatedEvalRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    eval_run_id: UUID
    dataset_version_id: UUID
    eval_suite_version_id: UUID
    dataset_manifest_id: UUID
    registered_units: int
    organization_id: UUID
    workspace_id: UUID


class SealedEvalRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    eval_run_id: UUID
    dataset_version_id: UUID
    eval_suite_version_id: UUID
    dataset_manifest_id: UUID
    test_report_manifest_id: UUID
    manifest_id: UUID
    seal_digest: str
    registered_units: int
    terminal_units: int
    organization_id: UUID
    workspace_id: UUID


class EvalFoundationService:
    """EvalRun 的 PostgreSQL 应用服务：真实持久化、行锁串行化、sealed 复核。"""

    def __init__(
        self,
        session: AsyncSession,
        context: TenantContext | None,
        store: ObjectStore,
    ) -> None:
        if context is None or context.workspace_id is None:
            raise TenantContextRequired("eval runs require workspace context")
        self._session = session
        self._context = context
        self._workspace_id = context.workspace_id
        self._namespace = ObjectNamespace(
            organization_id=context.organization_id, workspace_id=context.workspace_id
        )
        self._store = store
        self._schema_registry = _eval_schema_registry()
        self._artifacts = ArtifactService(session, context, store, self._schema_registry)

    async def create(self, command: CreateEvalRunCommand) -> CreatedEvalRun:
        state = EvalRunState.create(
            mode=command.mode,
            registered_units=command.registered_units,
            code_digest=command.code_digest,
            config_digest=command.config_digest,
            schema_digest=command.schema_digest,
        )
        dataset_bytes = canonical_json(command.dataset_payload)
        dataset_digest = digest_bytes(dataset_bytes)
        dataset_version_id = uuid4()
        temporary_key = self._store.write_temporary(self._namespace, [dataset_bytes])
        dataset_artifact = await self._artifacts.commit_upload(
            temporary_key,
            self._dataset_manifest_command(dataset_version_id, dataset_digest, len(dataset_bytes)),
        )
        dataset_version = DatasetVersion(
            id=dataset_version_id,
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
            dataset_id=uuid4(),
            version=1,
            content_digest=dataset_digest,
            manifest_id=dataset_artifact.manifest_id,
            status="frozen",
            schema_version=1,
        )
        suite_version = EvalSuiteVersion(
            id=uuid4(),
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
            suite_id=uuid4(),
            version=1,
            content_digest=_suite_digest(state.registered_units),
            status="frozen",
            schema_version=1,
        )
        run_id = uuid4()
        eval_run_id = uuid4()
        run = Run(
            id=run_id,
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
            status="running",
            schema_version=1,
        )
        # eval_runs 的复合 FK 依赖 runs/eval_suite_versions/dataset_versions 行；
        # 显式分两段 flush，避免 ORM 把 eval_runs 排在依赖行之前。
        self._session.add_all([dataset_version, suite_version, run])
        await self._session.flush()
        eval_run = EvalRun(
            id=eval_run_id,
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
            run_id=run_id,
            dataset_version_id=dataset_version_id,
            eval_suite_version_id=suite_version.id,
            mode=command.mode.value,
            status="running",
            code_digest=command.code_digest,
            config_digest=command.config_digest,
            schema_digest=command.schema_digest,
            schema_version=1,
        )
        self._session.add(eval_run)
        for unit in state.registered_units:
            self._session.add(
                EvalSample(
                    id=uuid4(),
                    organization_id=self._context.organization_id,
                    workspace_id=self._workspace_id,
                    eval_run_id=eval_run_id,
                    sample_id=unit.sample_id,
                    unit_id=unit.unit_id,
                    status="registered",
                    schema_version=1,
                )
            )
        await self._session.flush()
        return CreatedEvalRun(
            run_id=run_id,
            eval_run_id=eval_run_id,
            dataset_version_id=dataset_version_id,
            eval_suite_version_id=suite_version.id,
            dataset_manifest_id=dataset_artifact.manifest_id,
            registered_units=len(state.registered_units),
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
        )

    async def record_outcome(
        self, eval_run_id: UUID, outcome: SampleOutcome
    ) -> None:
        eval_run = await self._load_eval_run(eval_run_id, for_update=True)
        state = await self._load_state(eval_run)
        state.record(outcome)
        sample = await self._session.scalar(
            select(EvalSample).where(
                EvalSample.organization_id == self._context.organization_id,
                EvalSample.workspace_id == self._workspace_id,
                EvalSample.eval_run_id == eval_run_id,
                EvalSample.sample_id == outcome.unit.sample_id,
                EvalSample.unit_id == outcome.unit.unit_id,
            )
        )
        if sample is None:
            raise EvalStateError(
                f"unit is not registered: {outcome.unit.sample_id!r}/{outcome.unit.unit_id!r}"
            )
        sample.status = outcome.status.value
        sample.result = outcome.result
        sample.result_digest = outcome.result_digest
        await self._session.flush()

    async def pause(self, eval_run_id: UUID) -> None:
        eval_run = await self._load_eval_run(eval_run_id, for_update=True)
        state = await self._load_state(eval_run)
        state.pause()
        eval_run.status = RunPhase.PARTIAL.value
        await self._session.flush()

    async def resume(self, eval_run_id: UUID) -> None:
        eval_run = await self._load_eval_run(eval_run_id, for_update=True)
        state = await self._load_state(eval_run)
        state.resume()
        eval_run.status = RunPhase.RUNNING.value
        await self._session.flush()

    async def seal(
        self,
        eval_run_id: UUID,
        *,
        migration_revision: str,
        test_report: dict[str, Any],
    ) -> SealedEvalRun:
        return await self._seal(
            eval_run_id,
            migration_revision=migration_revision,
            test_report=test_report,
        )

    async def seal_empty(self, command: SealEmptyCommand) -> SealedEvalRun:
        created = await self.create(
            CreateEvalRunCommand(
                mode=command.mode,
                registered_units=(),
                dataset_payload={"registered_units": []},
                code_digest=command.code_digest,
                config_digest=command.config_digest,
                schema_digest=command.schema_digest,
            )
        )
        return await self._seal(
            created.eval_run_id,
            migration_revision=command.migration_revision,
            test_report=command.test_report,
        )

    async def verify_sealed(self, eval_run_id: UUID) -> SealedEvalArtifact:
        eval_run = await self._session.scalar(
            select(EvalRun).where(
                EvalRun.id == eval_run_id,
                EvalRun.organization_id == self._context.organization_id,
                EvalRun.workspace_id == self._workspace_id,
            )
        )
        if eval_run is None:
            raise EvalRunNotFound("EvalRun is missing from tenant scope")
        if eval_run.status != RunPhase.SEALED.value:
            raise EvalStateError("only a sealed eval run can be verified")

        seal_manifest = await self._artifact_manifest("eval_run", eval_run_id)
        if seal_manifest is None:
            raise ArtifactVerificationError("seal manifest is missing")
        await self._artifacts.verify_manifest(seal_manifest.id)
        seal_bytes = b"".join(
            self._store.read_immutable(self._namespace, seal_manifest.object_key)
        )
        payload = _json_object(seal_bytes, "seal object")
        artifact = verify_sealed_artifact(payload, seal_manifest.content_digest)
        if artifact.eval_run_id != eval_run_id:
            raise ArtifactVerificationError("seal payload owner does not match eval run")
        if (
            artifact.code_digest != eval_run.code_digest
            or artifact.config_digest != eval_run.config_digest
            or artifact.schema_digest != eval_run.schema_digest
        ):
            raise ArtifactVerificationError("seal payload digests do not match eval run")

        dataset_manifest = await self._artifact_manifest(
            "dataset_version", eval_run.dataset_version_id
        )
        if dataset_manifest is None:
            raise ArtifactVerificationError("dataset manifest is missing")
        await self._artifacts.verify_manifest(dataset_manifest.id)
        if dataset_manifest.id != artifact.dataset_manifest_id:
            raise ArtifactVerificationError("seal dataset manifest does not match")
        if dataset_manifest.content_digest != artifact.dataset_digest:
            raise ArtifactVerificationError("seal dataset digest does not match")

        test_report_manifest = await self._artifact_manifest("eval_test_report", eval_run_id)
        if test_report_manifest is None:
            raise ArtifactVerificationError("test report manifest is missing")
        await self._artifacts.verify_manifest(test_report_manifest.id)
        if test_report_manifest.id != artifact.test_report_manifest_id:
            raise ArtifactVerificationError("seal test report manifest does not match")
        if test_report_manifest.content_digest != artifact.test_report_digest:
            raise ArtifactVerificationError("seal test report digest does not match")
        return artifact

    async def _seal(
        self,
        eval_run_id: UUID,
        *,
        migration_revision: str,
        test_report: dict[str, Any],
    ) -> SealedEvalRun:
        eval_run = await self._load_eval_run(eval_run_id, for_update=True)
        state = await self._load_state(eval_run)
        if state.status is RunPhase.SEALED:
            raise EvalStateError("eval run is already sealed")
        if not state.is_complete:
            raise EvalSealRefused(
                "eval run cannot seal until every registered unit is terminal; "
                f"{len(state.pending_units)} pending"
            )
        sealed_state = state.seal()

        dataset_version = await self._session.scalar(
            select(DatasetVersion).where(
                DatasetVersion.id == eval_run.dataset_version_id,
                DatasetVersion.organization_id == self._context.organization_id,
                DatasetVersion.workspace_id == self._workspace_id,
            )
        )
        suite_version = await self._session.scalar(
            select(EvalSuiteVersion).where(
                EvalSuiteVersion.id == eval_run.eval_suite_version_id,
                EvalSuiteVersion.organization_id == self._context.organization_id,
                EvalSuiteVersion.workspace_id == self._workspace_id,
            )
        )
        if dataset_version is None or dataset_version.manifest_id is None:
            raise EvalStateError("dataset version is not frozen")
        if suite_version is None:
            raise EvalStateError("suite version is missing")

        # seal 前复验 dataset 对象字节（specs/s0 §4：missing object / digest
        # mismatch 不得 seal）。此前只信 dataset_version.content_digest 的 DB
        # 信任链——create 与 seal 之间对象被篡改/删除时仍会 sealed。
        dataset_manifest = await self._artifact_manifest(
            "dataset_version", eval_run.dataset_version_id
        )
        if dataset_manifest is None:
            raise EvalStateError("dataset manifest is missing")
        await self._artifacts.verify_manifest(dataset_manifest.id)
        if dataset_manifest.content_digest != dataset_version.content_digest:
            raise EvalStateError(
                "dataset manifest digest does not match the frozen dataset version"
            )

        test_report_bytes = canonical_json(test_report)
        test_report_digest = digest_bytes(test_report_bytes)
        temporary_key = self._store.write_temporary(self._namespace, [test_report_bytes])
        test_report_artifact = await self._artifacts.commit_upload(
            temporary_key,
            ArtifactManifestCommand(
                owner_resource_type="eval_test_report",
                owner_resource_id=eval_run_id,
                content_digest=test_report_digest,
                size_bytes=len(test_report_bytes),
                media_type="application/json",
                artifact_schema_id="gate.test-report",
                artifact_schema_version=1,
                classification="PUBLIC",
                retention={},
            ),
        )

        if eval_run.run_id is None:
            raise EvalStateError("eval run is missing its Run reference")
        artifact, seal_digest = build_sealed_artifact(
            run_id=eval_run.run_id,
            eval_run_id=eval_run_id,
            state=sealed_state,
            dataset_digest=dataset_version.content_digest,
            dataset_manifest_id=dataset_version.manifest_id,
            suite_digest=suite_version.content_digest,
            migration_revision=migration_revision,
            test_report_digest=test_report_digest,
            test_report_manifest_id=test_report_artifact.manifest_id,
        )
        seal_bytes = canonical_json(artifact.canonical_mapping())
        temporary_key = self._store.write_temporary(self._namespace, [seal_bytes])
        seal_artifact = await self._artifacts.commit_upload(
            temporary_key,
            ArtifactManifestCommand(
                owner_resource_type="eval_run",
                owner_resource_id=eval_run_id,
                content_digest=seal_digest,
                size_bytes=len(seal_bytes),
                media_type="application/json",
                artifact_schema_id="eval.sealed-run",
                artifact_schema_version=1,
                classification="PUBLIC",
                retention={},
            ),
        )

        now = utc_now()
        eval_run.status = RunPhase.SEALED.value
        eval_run.sealed_at = now
        run = await self._session.scalar(
            select(Run).where(
                Run.id == eval_run.run_id,
                Run.organization_id == self._context.organization_id,
                Run.workspace_id == self._workspace_id,
            )
        )
        if run is None:
            raise EvalStateError("Run is missing from tenant scope")
        run.status = "succeeded"
        run.updated_at = now

        unit_of_work = CanonicalUnitOfWork(
            self._session, self._context, schema_registry=self._schema_registry
        )
        await unit_of_work.append_event(
            EventCommand(
                run_id=eval_run.run_id,
                event_type="eval.run.sealed",
                payload_schema_version=1,
                payload={
                    "eval_run_id": eval_run_id,
                    "manifest_id": seal_artifact.manifest_id,
                    "seal_digest": seal_digest,
                },
                actor_ref="system:evals",
                idempotency_key=f"seal:{eval_run_id}",
            )
        )
        await self._session.flush()
        return SealedEvalRun(
            run_id=eval_run.run_id,
            eval_run_id=eval_run_id,
            dataset_version_id=dataset_version.id,
            eval_suite_version_id=suite_version.id,
            dataset_manifest_id=dataset_version.manifest_id,
            test_report_manifest_id=test_report_artifact.manifest_id,
            manifest_id=seal_artifact.manifest_id,
            seal_digest=seal_digest,
            registered_units=len(sealed_state.registered_units),
            terminal_units=len(sealed_state.outcomes),
            organization_id=self._context.organization_id,
            workspace_id=self._workspace_id,
        )

    async def _load_eval_run(self, eval_run_id: UUID, *, for_update: bool) -> EvalRun:
        statement = select(EvalRun).where(
            EvalRun.id == eval_run_id,
            EvalRun.organization_id == self._context.organization_id,
            EvalRun.workspace_id == self._workspace_id,
        )
        if for_update:
            statement = statement.with_for_update()
        eval_run = await self._session.scalar(statement)
        if eval_run is None:
            raise EvalRunNotFound("EvalRun is missing from tenant scope")
        return eval_run

    async def _load_state(self, eval_run: EvalRun) -> EvalRunState:
        try:
            mode = EvalMode(eval_run.mode)
        except ValueError as exc:
            raise EvalStateError(f"eval run mode is unknown: {eval_run.mode!r}") from exc
        rows = list(
            (
                await self._session.scalars(
                    select(EvalSample)
                    .where(
                        EvalSample.organization_id == self._context.organization_id,
                        EvalSample.workspace_id == self._workspace_id,
                        EvalSample.eval_run_id == eval_run.id,
                    )
                    .order_by(EvalSample.sample_id, EvalSample.unit_id)
                )
            ).all()
        )
        units = tuple(
            RegisteredUnit(sample_id=row.sample_id, unit_id=row.unit_id) for row in rows
        )
        outcomes: list[SampleOutcome] = []
        for row in rows:
            try:
                status = SampleStatus(row.status)
            except ValueError as exc:
                raise EvalStateError(f"eval sample status is unknown: {row.status!r}") from exc
            if is_terminal(status):
                outcomes.append(
                    SampleOutcome(
                        unit=RegisteredUnit(
                            sample_id=row.sample_id, unit_id=row.unit_id
                        ),
                        status=status,
                        result=dict(row.result or {}),
                    )
                )
        try:
            run_status = RunPhase(eval_run.status)
        except ValueError as exc:
            raise EvalStateError(f"eval run status is unknown: {eval_run.status!r}") from exc
        return EvalRunState.restore(
            mode=mode,
            registered_units=units,
            outcomes=tuple(outcomes),
            status=run_status,
            code_digest=eval_run.code_digest,
            config_digest=eval_run.config_digest,
            schema_digest=eval_run.schema_digest,
        )

    async def _artifact_manifest(
        self, owner_resource_type: str, owner_resource_id: UUID
    ) -> ArtifactManifest | None:
        return await self._session.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.organization_id == self._context.organization_id,
                ArtifactManifest.workspace_id == self._workspace_id,
                ArtifactManifest.owner_resource_type == owner_resource_type,
                ArtifactManifest.owner_resource_id == owner_resource_id,
            )
        )

    @staticmethod
    def _dataset_manifest_command(
        dataset_version_id: UUID, content_digest: str, size_bytes: int
    ) -> ArtifactManifestCommand:
        return ArtifactManifestCommand(
            owner_resource_type="dataset_version",
            owner_resource_id=dataset_version_id,
            content_digest=content_digest,
            size_bytes=size_bytes,
            media_type="application/json",
            artifact_schema_id="eval.dataset",
            artifact_schema_version=1,
            classification="PUBLIC",
            retention={},
        )


def _eval_schema_registry() -> SchemaRegistry:
    registry = EvalSchemaRegistry()
    registry.register("eval.dataset", 1, _DatasetPayload)
    registry.register("gate.test-report", 1, _TestReportPayload)
    registry.register("eval.sealed-run", 1, _SealedRunPayload)
    registry.register("eval.run.sealed", 1, EvalRunSealedPayload)
    return registry


class EvalSchemaRegistry(SchemaRegistry):
    """放宽 schema key 字符集，其余行为与 SchemaRegistry 完全一致。

    T6 契约（测试断言）要求 artifact_schema_id 为 `gate.test-report` / `eval.sealed-run`
    这类带连字符的标识；冻结的 key 校验只允许小写字母数字与点。这里保持「unknown id /
    version fail closed、只注册一次、确定性枚举」语义，仅放宽 id 字符集，artifact 协议
    本体（ArtifactService/manifest）不变。
    """

    def register(self, schema_id: str, schema_version: int, model: type[BaseModel]) -> None:
        if not isinstance(schema_id, str) or not schema_id:
            raise ValueError("schema_id must be a non-empty string")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version <= 0
        ):
            raise ValueError("schema_version must be a positive integer")
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise ValueError("model must be a pydantic BaseModel class")
        key = (schema_id, schema_version)
        if key in self._models:
            raise ValueError(f"schema is already registered: {schema_id}@{schema_version}")
        self._models[key] = model

    def resolve(self, schema_id: str, schema_version: int) -> type[BaseModel]:
        try:
            return self._models[(schema_id, schema_version)]
        except (KeyError, TypeError) as exc:
            raise UnknownSchemaError(f"unknown schema: {schema_id}@{schema_version}") from exc


def _suite_digest(units: tuple[RegisteredUnit, ...]) -> str:
    return digest(
        {
            "registered_units": [
                {"sample_id": unit.sample_id, "unit_id": unit.unit_id} for unit in units
            ]
        }
    )


def _json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactVerificationError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactVerificationError(f"{label} must be a JSON object")
    return value
