"""S0 relational schema metadata.

The migration is deliberately self-contained; these models are runtime mappings, not migration input.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Declarative base with deterministic constraint names."""

    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (CheckConstraint("schema_version > 0", name="schema_version"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_ref: Mapped[str | None] = mapped_column(String(255))
    retention_policy: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_workspaces_org",
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "id", name="uq_workspaces_org_id"),
        UniqueConstraint("organization_id", "name", name="uq_workspaces_org_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    classification_ceiling: Mapped[str] = mapped_column(String(32), default="PUBLIC")
    budget_policy: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentDefinition(Base):
    __tablename__ = "agent_definitions"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_agent_definitions_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_agent_definitions_scope_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "agent_definition_id"],
            [
                "agent_definitions.organization_id",
                "agent_definitions.workspace_id",
                "agent_definitions.id",
            ],
            name="fk_agent_versions_definition",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_agent_versions_scope_id"
        ),
        UniqueConstraint(
            "agent_definition_id", "version", name="uq_agent_versions_definition_version"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    agent_definition_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_runs_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "agent_version_id"],
            [
                "agent_versions.organization_id",
                "agent_versions.workspace_id",
                "agent_versions.id",
            ],
            name="fk_runs_agent_version",
        ),
        UniqueConstraint("organization_id", "workspace_id", "id", name="uq_runs_scope_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    agent_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CanonicalEvent(Base):
    __tablename__ = "canonical_events"
    __table_args__ = (
        CheckConstraint("sequence_no > 0", name="sequence"),
        CheckConstraint("payload_schema_version > 0", name="payload_schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_canonical_events_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "sequence_no", name="uq_canonical_events_run_sequence"),
        UniqueConstraint(
            "organization_id", "run_id", "idempotency_key", name="uq_events_idempotency"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    task_id: Mapped[UUID | None] = mapped_column(Uuid)
    attempt_id: Mapped[UUID | None] = mapped_column(Uuid)
    epoch_id: Mapped[UUID | None] = mapped_column(Uuid)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_event_digest: Mapped[str | None] = mapped_column(String(71))
    event_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CanonicalProjection(Base):
    __tablename__ = "canonical_projections"
    __table_args__ = (
        CheckConstraint("sequence_no >= 0", name="sequence"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_canonical_projections_run",
            ondelete="CASCADE",
        ),
    )

    run_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    head_event_digest: Mapped[str | None] = mapped_column(String(71))
    state: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ArtifactManifest(Base):
    __tablename__ = "artifact_manifests"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_artifact_manifests_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_artifact_manifests_scope_id"
        ),
        UniqueConstraint(
            "organization_id", "object_key", name="uq_artifact_manifests_org_object_key"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    owner_resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_resource_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_schema_id: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    retention: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, default=dict)
    encryption_key_ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_dataset_versions_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "manifest_id"],
            [
                "artifact_manifests.organization_id",
                "artifact_manifests.workspace_id",
                "artifact_manifests.id",
            ],
            name="fk_dataset_versions_manifest",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_dataset_versions_scope_id"
        ),
        UniqueConstraint("dataset_id", "version", name="uq_dataset_versions_dataset_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    dataset_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    manifest_id: Mapped[UUID | None] = mapped_column(Uuid)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvalSuiteVersion(Base):
    __tablename__ = "eval_suite_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="version"),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_eval_suite_versions_workspace",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_eval_suite_versions_scope_id"
        ),
        UniqueConstraint("suite_id", "version", name="uq_eval_suite_versions_suite_version"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    suite_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvalRun(Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_eval_runs_workspace",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_eval_runs_run",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "dataset_version_id"],
            [
                "dataset_versions.organization_id",
                "dataset_versions.workspace_id",
                "dataset_versions.id",
            ],
            name="fk_eval_runs_dataset_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "eval_suite_version_id"],
            [
                "eval_suite_versions.organization_id",
                "eval_suite_versions.workspace_id",
                "eval_suite_versions.id",
            ],
            name="fk_eval_runs_suite_version",
        ),
        UniqueConstraint("organization_id", "workspace_id", "id", name="uq_eval_runs_scope_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    run_id: Mapped[UUID | None] = mapped_column(Uuid)
    dataset_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    eval_suite_version_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    code_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    config_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    schema_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EvalSample(Base):
    __tablename__ = "eval_samples"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id", "eval_run_id"],
            ["eval_runs.organization_id", "eval_runs.workspace_id", "eval_runs.id"],
            name="fk_eval_samples_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "eval_run_id", "sample_id", "unit_id", name="uq_eval_samples_registered_unit"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    eval_run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    sample_id: Mapped[str] = mapped_column(String(255), nullable=False)
    unit_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE)
    result_digest: Mapped[str | None] = mapped_column(String(71))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_idempotency_records_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_idempotency_records_workspace",
        ),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_tenant_scope_key",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    scope: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="completed")
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_audit_events_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_audit_events_workspace",
        ),
        UniqueConstraint("event_digest", name="uq_audit_events_event_digest"),
        UniqueConstraint(
            "organization_id",
            "workspace_id",
            "previous_event_digest",
            name="uq_audit_events_scope_previous",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    actor_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    previous_event_digest: Mapped[str | None] = mapped_column(String(71))
    event_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxMessage(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'delivered', 'dead_letter')",
            name="status",
        ),
        CheckConstraint(
            "(status = 'processing' AND claimed_by IS NOT NULL "
            "AND claim_token IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND claimed_by IS NULL "
            "AND claim_token IS NULL AND claimed_at IS NULL "
            "AND lease_expires_at IS NULL)",
            name="claim",
        ),
        CheckConstraint(
            "(status = 'dead_letter' AND dead_lettered_at IS NOT NULL) OR "
            "(status <> 'dead_letter' AND dead_lettered_at IS NULL)",
            name="dead_letter",
        ),
        CheckConstraint("schema_version > 0", name="schema_version"),
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_outbox_organization",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_outbox_workspace",
        ),
        Index("ix_outbox_dispatch", "status", "available_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    organization_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    workspace_id: Mapped[UUID | None] = mapped_column(Uuid, index=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    claim_token: Mapped[UUID | None] = mapped_column(Uuid)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
