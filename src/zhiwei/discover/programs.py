"""S8 DiscoveryProgram and ProgramVersion domain models.

ProgramVersion 固定 risk charter、sources/entities、exclusions、triggers、
detector packs、evidence/falsification standard、recipients、budget、
approval/action policy 和 service identity。

activate/deactivate/version change 有 audit；后台 run 不继承创建者
session/token/personal memory。

事实源：specs/s8-discover-actions.md §3，ADR-004。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import ensure_utc


class ProgramStatus(StrEnum):
    """DiscoveryProgram lifecycle states."""

    DRAFT = "draft"
    ACTIVE = "active"
    DEACTIVATED = "deactivated"
    SUPERSEDED = "superseded"


class AuditAction(StrEnum):
    """Audit trail action types for program lifecycle changes."""

    CREATED = "created"
    ACTIVATED = "activated"
    DEACTIVATED = "deactivated"
    VERSION_BUMPED = "version_bumped"
    TRIGGER_ADDED = "trigger_added"
    TRIGGER_REMOVED = "trigger_removed"
    POLICY_CHANGED = "policy_changed"


class _FrozenModel(BaseModel):
    """Base frozen model: immutable + reject unknown fields (fail closed)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("created_at", "updated_at", check_fields=False)
    @classmethod
    def _utc_aware(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class SourceRef(_FrozenModel):
    """Reference to a data source monitored by the program."""

    source_id: UUID
    name: str = Field(min_length=1)
    classification: str = Field(default="PUBLIC")
    connector_type: str = Field(min_length=1)


class DetectorPackRef(_FrozenModel):
    """Reference to a detector pack used by the program."""

    pack_id: UUID
    name: str = Field(min_length=1)
    version: int = Field(ge=1)


class FalsificationStandard(_FrozenModel):
    """ADR-004: 配置假标准，声明每个 hypothesis 至少需要多少个 negative probe。

    N 由 ProgramVersion 的 falsification standard 声明——hypothesis 只有在
    「至少 N 个 negative probe 已执行且未推翻」时才能进入 triage 队列。
    """

    min_probes_required: int = Field(ge=1, default=3)
    max_probe_budget_tokens: int = Field(ge=0, default=50_000)
    probe_generation_model: str | None = None
    deterministic_evaluation_only: bool = True


class BudgetLimit(_FrozenModel):
    """Token/cost budget for the program."""

    max_weighted_tokens_per_run: int = Field(ge=0, default=200_000)
    max_cost_usd_per_day: float = Field(ge=0.0, default=10.0)
    hard_stop: bool = False


class ApprovalPolicy(_FrozenModel):
    """Approval and action policy for the program.

    不自动决定业务真相，不默认执行高风险动作。
    """

    require_human_approval_for_actions: bool = True
    auto_execute_low_risk: bool = False
    high_risk_action_types: tuple[str, ...] = ("execute", "delete", "modify")
    escalation_recipients: tuple[str, ...] = ()


class AuditRecord(_FrozenModel):
    """Immutable audit record for a program lifecycle event."""

    id: UUID
    program_id: UUID
    action: AuditAction
    performed_by: str = Field(min_length=1)
    timestamp: datetime
    details: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _utc_timestamp(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class ProgramVersion(_FrozenModel):
    """A frozen version of a DiscoveryProgram configuration.

    ProgramVersion is immutable: changing any field produces a new version.
    The version field increments on each change; parent_id links to the
    previous version for audit trail.
    """

    id: UUID
    program_id: UUID
    version: int = Field(ge=1)
    risk_charter: str = Field(min_length=1)
    sources: tuple[SourceRef, ...] = Field(default_factory=tuple)
    exclusions: tuple[str, ...] = Field(default_factory=tuple)
    triggers: tuple[UUID, ...] = Field(default_factory=tuple)
    detector_packs: tuple[DetectorPackRef, ...] = Field(default_factory=tuple)
    falsification_standard: FalsificationStandard = Field(default_factory=FalsificationStandard)
    recipients: tuple[str, ...] = Field(default_factory=tuple)
    budget: BudgetLimit = Field(default_factory=BudgetLimit)
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    service_identity: str | None = None
    parent_id: UUID | None = None
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("version", "schema_version")
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value


class DiscoveryProgram(_FrozenModel):
    """A discovery program that monitors sources and generates signals.

    Background runs spawned by this program do NOT inherit the creator's
    session/token/personal memory — this is enforced by the runtime layer
    reading service_identity rather than creator session context.
    """

    id: UUID
    name: str = Field(min_length=1)
    description: str = ""
    current_version_id: UUID
    status: ProgramStatus = ProgramStatus.DRAFT
    created_by: str = Field(min_length=1)
    service_identity: str | None = None
    created_at: datetime
    updated_at: datetime
    schema_version: int = 1

    @field_validator("version", "schema_version", check_fields=False)
    @classmethod
    def _version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be positive")
        return value


class ProgramManager:
    """Manages DiscoveryProgram and ProgramVersion lifecycle.

    activate/deactivate/version change 有 audit；
    后台 run 不继承创建者 session/token/personal memory。
    """

    def __init__(self) -> None:
        self._programs: dict[UUID, DiscoveryProgram] = {}
        self._versions: dict[UUID, ProgramVersion] = {}
        self._audit_log: list[AuditRecord] = []

    def create_program(
        self,
        name: str,
        created_by: str,
        risk_charter: str,
        *,
        description: str = "",
        service_identity: str | None = None,
    ) -> DiscoveryProgram:
        """Create a new discovery program in DRAFT status."""
        now = datetime.now(UTC)
        program_id = new_id()
        version = ProgramVersion(
            id=new_id(),
            program_id=program_id,
            version=1,
            risk_charter=risk_charter,
            service_identity=service_identity,
            created_at=now,
            updated_at=now,
        )
        self._versions[version.id] = version

        program = DiscoveryProgram(
            id=program_id,
            name=name,
            description=description,
            current_version_id=version.id,
            status=ProgramStatus.DRAFT,
            created_by=created_by,
            service_identity=service_identity,
            created_at=now,
            updated_at=now,
        )
        self._programs[program.id] = program

        self._audit_log.append(
            AuditRecord(
                id=new_id(),
                program_id=program.id,
                action=AuditAction.CREATED,
                performed_by=created_by,
                timestamp=now,
                details={"version_id": str(version.id), "risk_charter": risk_charter},
            )
        )
        return program

    def get_program(self, program_id: UUID) -> DiscoveryProgram:
        if program_id not in self._programs:
            raise ValueError(f"Program {program_id} not found")
        return self._programs[program_id]

    def get_version(self, version_id: UUID) -> ProgramVersion:
        if version_id not in self._versions:
            raise ValueError(f"Version {version_id} not found")
        return self._versions[version_id]

    def activate(self, program_id: UUID, performed_by: str) -> DiscoveryProgram:
        """Activate a draft program. Produces an audit record."""
        program = self.get_program(program_id)
        if program.status != ProgramStatus.DRAFT:
            raise ValueError(
                f"Cannot activate program in {program.status} status; only draft can be activated"
            )
        now = datetime.now(UTC)
        updated = program.model_copy(
            update={"status": ProgramStatus.ACTIVE, "updated_at": now}
        )
        self._programs[program_id] = updated

        self._audit_log.append(
            AuditRecord(
                id=new_id(),
                program_id=program_id,
                action=AuditAction.ACTIVATED,
                performed_by=performed_by,
                timestamp=now,
            )
        )
        return updated

    def deactivate(self, program_id: UUID, performed_by: str) -> DiscoveryProgram:
        """Deactivate an active program. Produces an audit record."""
        program = self.get_program(program_id)
        if program.status != ProgramStatus.ACTIVE:
            raise ValueError(
                f"Cannot deactivate program in {program.status} status; only active can be deactivated"
            )
        now = datetime.now(UTC)
        updated = program.model_copy(
            update={"status": ProgramStatus.DEACTIVATED, "updated_at": now}
        )
        self._programs[program_id] = updated

        self._audit_log.append(
            AuditRecord(
                id=new_id(),
                program_id=program_id,
                action=AuditAction.DEACTIVATED,
                performed_by=performed_by,
                timestamp=now,
            )
        )
        return updated

    def bump_version(
        self,
        program_id: UUID,
        performed_by: str,
        *,
        risk_charter: str | None = None,
    ) -> ProgramVersion:
        """Create a new version of the program. Produces an audit record.

        The new version inherits fields from the current version and
        overrides only explicitly provided values.
        """
        program = self.get_program(program_id)
        current = self.get_version(program.current_version_id)

        now = datetime.now(UTC)
        new_version = current.model_copy(
            update={
                "id": new_id(),
                "version": current.version + 1,
                "parent_id": current.id,
                "risk_charter": risk_charter if risk_charter is not None else current.risk_charter,
                "created_at": now,
                "updated_at": now,
            }
        )
        self._versions[new_version.id] = new_version

        updated_program = program.model_copy(
            update={"current_version_id": new_version.id, "updated_at": now}
        )
        self._programs[program_id] = updated_program

        self._audit_log.append(
            AuditRecord(
                id=new_id(),
                program_id=program_id,
                action=AuditAction.VERSION_BUMPED,
                performed_by=performed_by,
                timestamp=now,
                details={
                    "old_version": current.version,
                    "new_version": new_version.version,
                    "new_version_id": str(new_version.id),
                },
            )
        )
        return new_version

    def get_audit_log(self, program_id: UUID) -> tuple[AuditRecord, ...]:
        """Return audit records for a program in chronological order."""
        return tuple(r for r in self._audit_log if r.program_id == program_id)
