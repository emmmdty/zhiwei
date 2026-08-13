"""Create the S0 PostgreSQL foundation.

Revision ID: 0001_foundation
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "organizations",
    "workspaces",
    "agent_definitions",
    "agent_versions",
    "runs",
    "canonical_events",
    "canonical_projections",
    "artifact_manifests",
    "dataset_versions",
    "eval_suite_versions",
    "eval_runs",
    "eval_samples",
    "idempotency_records",
    "audit_events",
    "outbox",
)
_WORKSPACE_TABLES = {
    "agent_definitions",
    "agent_versions",
    "runs",
    "canonical_events",
    "canonical_projections",
    "artifact_manifests",
    "dataset_versions",
    "eval_suite_versions",
    "eval_runs",
    "eval_samples",
}
_OPTIONAL_WORKSPACE_TABLES = {"idempotency_records", "audit_events", "outbox"}
_MUTABLE_COLUMNS = {
    "organizations": ("status", "policy_ref", "retention_policy"),
    "workspaces": ("name", "classification_ceiling", "budget_policy"),
    "agent_definitions": ("name", "lifecycle"),
    "runs": ("status", "updated_at"),
    "canonical_projections": ("sequence_no", "head_event_digest", "state", "updated_at"),
    "eval_runs": ("status", "sealed_at"),
    "eval_samples": ("status", "result", "result_digest"),
    "outbox": (
        "status",
        "attempts",
        "available_at",
        "claimed_by",
        "claimed_at",
        "last_error",
        "dead_lettered_at",
    ),
}
_ORG_GUC = "NULLIF(current_setting('zhiwei.organization_id', true), '')::uuid"
_WORKSPACE_GUC = "NULLIF(current_setting('zhiwei.workspace_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("policy_ref", sa.String(length=255), nullable=True),
        sa.Column(
            "retention_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_organizations_schema_version")),
        sa.PrimaryKeyConstraint("id", name="pk_organizations"),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "classification_ceiling",
            sa.String(length=32),
            server_default=sa.text("'PUBLIC'"),
            nullable=False,
        ),
        sa.Column(
            "budget_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_workspaces_schema_version")),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], name="fk_workspaces_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.UniqueConstraint("organization_id", "id", name="uq_workspaces_org_id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_workspaces_org_name"),
    )
    op.create_index("ix_workspaces_organization_id", "workspaces", ["organization_id"])

    op.create_table(
        "agent_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "lifecycle", sa.String(length=32), server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_agent_definitions_schema_version")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_agent_definitions_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_definitions"),
        sa.UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_agent_definitions_scope_id"
        ),
    )
    _tenant_indexes("agent_definitions")

    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_definition_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(length=71), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_agent_versions_version")),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_agent_versions_schema_version")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "agent_definition_id"],
            [
                "agent_definitions.organization_id",
                "agent_definitions.workspace_id",
                "agent_definitions.id",
            ],
            name="fk_agent_versions_definition",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_versions"),
        sa.UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_agent_versions_scope_id"
        ),
        sa.UniqueConstraint(
            "agent_definition_id", "version", name="uq_agent_versions_definition_version"
        ),
    )
    _tenant_indexes("agent_versions")

    op.create_table(
        "runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_runs_schema_version")),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_runs_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "agent_version_id"],
            ["agent_versions.organization_id", "agent_versions.workspace_id", "agent_versions.id"],
            name="fk_runs_agent_version",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        sa.UniqueConstraint("organization_id", "workspace_id", "id", name="uq_runs_scope_id"),
    )
    _tenant_indexes("runs")

    op.create_table(
        "canonical_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_no", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("epoch_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("previous_event_digest", sa.String(length=71), nullable=True),
        sa.Column("event_digest", sa.String(length=71), nullable=False),
        sa.Column(
            "committed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence_no > 0", name=op.f("ck_canonical_events_sequence")),
        sa.CheckConstraint(
            "payload_schema_version > 0",
            name=op.f("ck_canonical_events_payload_schema_version"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_canonical_events_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_canonical_events"),
        sa.UniqueConstraint("run_id", "sequence_no", name="uq_canonical_events_run_sequence"),
        sa.UniqueConstraint(
            "organization_id", "run_id", "idempotency_key", name="uq_events_idempotency"
        ),
    )
    _tenant_indexes("canonical_events")
    op.create_index("ix_canonical_events_run_id", "canonical_events", ["run_id"])

    op.create_table(
        "canonical_projections",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("sequence_no", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("head_event_digest", sa.String(length=71), nullable=True),
        sa.Column(
            "state",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "sequence_no >= 0", name=op.f("ck_canonical_projections_sequence")
        ),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_canonical_projections_schema_version")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_canonical_projections_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_canonical_projections"),
    )
    _tenant_indexes("canonical_projections")

    op.create_table(
        "artifact_manifests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("owner_resource_type", sa.String(length=64), nullable=False),
        sa.Column("owner_resource_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.String(length=71), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("artifact_schema_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column(
            "retention",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("encryption_key_ref", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_artifact_manifests_size")),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_artifact_manifests_schema_version")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_artifact_manifests_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_manifests"),
        sa.UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_artifact_manifests_scope_id"
        ),
        sa.UniqueConstraint(
            "organization_id", "object_key", name="uq_artifact_manifests_org_object_key"
        ),
    )
    _tenant_indexes("artifact_manifests")

    op.create_table(
        "dataset_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(length=71), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_dataset_versions_version")),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_dataset_versions_schema_version")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_dataset_versions_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "manifest_id"],
            [
                "artifact_manifests.organization_id",
                "artifact_manifests.workspace_id",
                "artifact_manifests.id",
            ],
            name="fk_dataset_versions_manifest",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dataset_versions"),
        sa.UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_dataset_versions_scope_id"
        ),
        sa.UniqueConstraint("dataset_id", "version", name="uq_dataset_versions_dataset_version"),
    )
    _tenant_indexes("dataset_versions")

    op.create_table(
        "eval_suite_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(length=71), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("version > 0", name=op.f("ck_eval_suite_versions_version")),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_eval_suite_versions_schema_version")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_eval_suite_versions_workspace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_eval_suite_versions"),
        sa.UniqueConstraint(
            "organization_id", "workspace_id", "id", name="uq_eval_suite_versions_scope_id"
        ),
        sa.UniqueConstraint("suite_id", "version", name="uq_eval_suite_versions_suite_version"),
    )
    _tenant_indexes("eval_suite_versions")

    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("dataset_version_id", sa.Uuid(), nullable=False),
        sa.Column("eval_suite_version_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("code_digest", sa.String(length=71), nullable=False),
        sa.Column("config_digest", sa.String(length=71), nullable=False),
        sa.Column("schema_digest", sa.String(length=71), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_eval_runs_schema_version")),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_eval_runs_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "run_id"],
            ["runs.organization_id", "runs.workspace_id", "runs.id"],
            name="fk_eval_runs_run",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "dataset_version_id"],
            [
                "dataset_versions.organization_id",
                "dataset_versions.workspace_id",
                "dataset_versions.id",
            ],
            name="fk_eval_runs_dataset_version",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "eval_suite_version_id"],
            [
                "eval_suite_versions.organization_id",
                "eval_suite_versions.workspace_id",
                "eval_suite_versions.id",
            ],
            name="fk_eval_runs_suite_version",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_eval_runs"),
        sa.UniqueConstraint("organization_id", "workspace_id", "id", name="uq_eval_runs_scope_id"),
    )
    _tenant_indexes("eval_runs")

    op.create_table(
        "eval_samples",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("eval_run_id", sa.Uuid(), nullable=False),
        sa.Column("sample_id", sa.String(length=255), nullable=False),
        sa.Column("unit_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("result_digest", sa.String(length=71), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_eval_samples_schema_version")),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id", "eval_run_id"],
            ["eval_runs.organization_id", "eval_runs.workspace_id", "eval_runs.id"],
            name="fk_eval_samples_eval_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_eval_samples"),
        sa.UniqueConstraint(
            "eval_run_id", "sample_id", "unit_id", name="uq_eval_samples_registered_unit"
        ),
    )
    _tenant_indexes("eval_samples")
    op.create_index("ix_eval_samples_eval_run_id", "eval_samples", ["eval_run_id"])

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_digest", sa.String(length=71), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'completed'"), nullable=False
        ),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "schema_version > 0", name=op.f("ck_idempotency_records_schema_version")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_idempotency_records_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_idempotency_records_workspace",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_records"),
        sa.UniqueConstraint(
            "organization_id",
            "workspace_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_tenant_scope_key",
            postgresql_nulls_not_distinct=True,
        ),
    )
    _optional_workspace_indexes("idempotency_records")

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("actor_ref", sa.String(length=255), nullable=False),
        sa.Column("payload_digest", sa.String(length=71), nullable=False),
        sa.Column("previous_event_digest", sa.String(length=71), nullable=True),
        sa.Column("event_digest", sa.String(length=71), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_audit_events_schema_version")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_audit_events_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_audit_events_workspace",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
    )
    _optional_workspace_indexes("audit_events")

    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("topic", sa.String(length=128), nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("claimed_by", sa.String(length=255), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_outbox_attempts")),
        sa.CheckConstraint("schema_version > 0", name=op.f("ck_outbox_schema_version")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_outbox_organization",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "workspace_id"],
            ["workspaces.organization_id", "workspaces.id"],
            name="fk_outbox_workspace",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_outbox"),
    )
    _optional_workspace_indexes("outbox")
    op.create_index("ix_outbox_dispatch", "outbox", ["status", "available_at"])

    op.execute("GRANT USAGE ON SCHEMA public TO zhiwei_app")
    for table in _TABLES:
        _enable_rls(table)
        op.execute(f'REVOKE ALL PRIVILEGES ON TABLE "{table}" FROM zhiwei_app')
        op.execute(f'GRANT SELECT, INSERT ON TABLE "{table}" TO zhiwei_app')
        if columns := _MUTABLE_COLUMNS.get(table):
            quoted_columns = ", ".join(f'"{column}"' for column in columns)
            op.execute(f'GRANT UPDATE ({quoted_columns}) ON TABLE "{table}" TO zhiwei_app')


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)


def _tenant_indexes(table: str) -> None:
    op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
    op.create_index(f"ix_{table}_workspace_id", table, ["workspace_id"])


def _optional_workspace_indexes(table: str) -> None:
    _tenant_indexes(table)


def _enable_rls(table: str) -> None:
    if table == "organizations":
        expression = f"id = {_ORG_GUC}"
    elif table == "workspaces":
        expression = (
            f"organization_id = {_ORG_GUC} AND "
            f"({_WORKSPACE_GUC} IS NULL OR id = {_WORKSPACE_GUC})"
        )
    elif table in _WORKSPACE_TABLES:
        expression = f"organization_id = {_ORG_GUC} AND workspace_id = {_WORKSPACE_GUC}"
    elif table in _OPTIONAL_WORKSPACE_TABLES:
        expression = (
            f"organization_id = {_ORG_GUC} AND "
            f"(workspace_id IS NULL OR workspace_id = {_WORKSPACE_GUC})"
        )
    else:  # pragma: no cover - migration constants are exhaustive
        raise RuntimeError(f"missing RLS policy for {table}")
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(
        f'CREATE POLICY "{table}_tenant_isolation" ON "{table}" '
        f"USING ({expression}) WITH CHECK ({expression})"
    )
