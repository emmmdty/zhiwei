"""S8-T1 RED: DiscoveryProgram and ProgramVersion lifecycle tests.

覆盖 S8 spec §3：
- ProgramVersion 固定 risk charter、sources/entities、exclusions、triggers、
  detector packs、evidence/falsification standard、recipients、budget、
  approval/action policy 和 service identity
- activate/deactivate/version change 有 audit
- 后台 run 不继承创建者 session/token/personal memory

覆盖 S8 spec §4 pipeline start：
- Trigger → watermark/snapshot → DataQualityResult → Signal
- Signal: immutable linked version

覆盖 ADR-004:
- FalsificationStandard 声明 min_probes_required
- NegativeProbe 必须 typed（可机器求值，不是自由文本）
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from zhiwei.contracts.identifiers import new_id
from zhiwei.discover.programs import (
    ApprovalPolicy,
    AuditAction,
    AuditRecord,
    BudgetLimit,
    DiscoveryProgram,
    FalsificationStandard,
    ProgramManager,
    ProgramStatus,
    ProgramVersion,
)
from zhiwei.discover.signals import (
    DataQualityResult,
    FalsificationResult,
    NegativeProbe,
    Signal,
    SignalChain,
    SignalSeverity,
    Watermark,
)
from zhiwei.discover.triggers import (
    ScheduleTrigger,
    SourceDeltaTrigger,
    TriggerRecord,
    TriggerState,
    TriggerType,
    WebhookTrigger,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_program(mgr: ProgramManager | None = None) -> tuple[ProgramManager, DiscoveryProgram]:
    if mgr is None:
        mgr = ProgramManager()
    program = mgr.create_program(
        name="test-program",
        created_by="tester",
        risk_charter="Detect anomalous spending patterns in financial data",
    )
    return mgr, program


# ---------------------------------------------------------------------------
# ProgramVersion frozen fields
# ---------------------------------------------------------------------------


class TestProgramVersionFields:
    def test_version_has_risk_charter(self) -> None:
        mgr, program = _make_program()
        version = mgr.get_version(program.current_version_id)
        assert version.risk_charter == "Detect anomalous spending patterns in financial data"

    def test_version_has_falsification_standard(self) -> None:
        mgr = ProgramManager()
        _, program = _make_program(mgr)
        version = mgr.get_version(program.current_version_id)
        assert isinstance(version.falsification_standard, FalsificationStandard)
        assert version.falsification_standard.min_probes_required >= 1

    def test_version_has_budget(self) -> None:
        mgr = ProgramManager()
        _, program = _make_program(mgr)
        version = mgr.get_version(program.current_version_id)
        assert isinstance(version.budget, BudgetLimit)
        assert version.budget.max_weighted_tokens_per_run >= 0

    def test_version_has_approval_policy(self) -> None:
        mgr, program = _make_program()
        version = mgr.get_version(program.current_version_id)
        assert isinstance(version.approval_policy, ApprovalPolicy)
        assert version.approval_policy.require_human_approval_for_actions is True

    def test_version_is_frozen(self) -> None:
        mgr, program = _make_program()
        version = mgr.get_version(program.current_version_id)
        with pytest.raises(ValidationError):
            version.risk_charter = "changed"  # type: ignore[misc]

    def test_version_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ProgramVersion(
                id=new_id(),
                program_id=new_id(),
                version=1,
                risk_charter="test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
                bogus_field="should fail",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# Program lifecycle
# ---------------------------------------------------------------------------


class TestProgramLifecycle:
    def test_create_program_returns_draft(self) -> None:
        _mgr, program = _make_program()
        assert program.status == ProgramStatus.DRAFT

    def test_activate_draft_to_active(self) -> None:
        mgr, program = _make_program()
        activated = mgr.activate(program.id, performed_by="admin")
        assert activated.status == ProgramStatus.ACTIVE

    def test_cannot_activate_nonexistent_program(self) -> None:
        mgr = ProgramManager()
        with pytest.raises(ValueError, match="not found"):
            mgr.activate(new_id(), performed_by="admin")

    def test_cannot_activate_already_active(self) -> None:
        mgr, program = _make_program()
        mgr.activate(program.id, performed_by="admin")
        with pytest.raises(ValueError, match="Cannot activate"):
            mgr.activate(program.id, performed_by="admin")

    def test_deactivate_active_to_deactivated(self) -> None:
        mgr, program = _make_program()
        mgr.activate(program.id, performed_by="admin")
        deactivated = mgr.deactivate(program.id, performed_by="admin")
        assert deactivated.status == ProgramStatus.DEACTIVATED

    def test_cannot_deactivate_draft(self) -> None:
        mgr, program = _make_program()
        with pytest.raises(ValueError, match="Cannot deactivate"):
            mgr.deactivate(program.id, performed_by="admin")

    def test_program_is_frozen(self) -> None:
        _mgr, program = _make_program()
        with pytest.raises(ValidationError):
            program.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Version bumping with audit
# ---------------------------------------------------------------------------


class TestVersionBump:
    def test_bump_version_increments(self) -> None:
        mgr, program = _make_program()
        new_version = mgr.bump_version(program.id, performed_by="admin")
        assert new_version.version == 2

    def test_bump_version_links_parent(self) -> None:
        mgr, program = _make_program()
        original_version = mgr.get_version(program.current_version_id)
        new_version = mgr.bump_version(program.id, performed_by="admin")
        assert new_version.parent_id == original_version.id

    def test_bump_version_updates_program(self) -> None:
        mgr, program = _make_program()
        new_version = mgr.bump_version(program.id, performed_by="admin")
        updated_program = mgr.get_program(program.id)
        assert updated_program.current_version_id == new_version.id

    def test_bump_version_with_new_risk_charter(self) -> None:
        mgr, program = _make_program()
        new_version = mgr.bump_version(
            program.id,
            performed_by="admin",
            risk_charter="Updated charter for new scope",
        )
        assert new_version.risk_charter == "Updated charter for new scope"

    def test_bump_version_preserves_old_fields(self) -> None:
        mgr, program = _make_program()
        version = mgr.get_version(program.current_version_id)
        new_version = mgr.bump_version(program.id, performed_by="admin")
        assert new_version.version == 2
        assert new_version.risk_charter == version.risk_charter


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


class TestAuditTrail:
    def test_create_produces_audit_record(self) -> None:
        mgr, program = _make_program()
        log = mgr.get_audit_log(program.id)
        assert len(log) == 1
        assert log[0].action == AuditAction.CREATED
        assert log[0].performed_by == "tester"

    def test_activate_produces_audit_record(self) -> None:
        mgr, program = _make_program()
        mgr.activate(program.id, performed_by="admin")
        log = mgr.get_audit_log(program.id)
        assert len(log) == 2
        assert log[1].action == AuditAction.ACTIVATED

    def test_deactivate_produces_audit_record(self) -> None:
        mgr, program = _make_program()
        mgr.activate(program.id, performed_by="admin")
        mgr.deactivate(program.id, performed_by="admin")
        log = mgr.get_audit_log(program.id)
        assert len(log) == 3
        assert log[2].action == AuditAction.DEACTIVATED

    def test_bump_version_produces_audit_record(self) -> None:
        mgr, program = _make_program()
        mgr.bump_version(program.id, performed_by="admin")
        log = mgr.get_audit_log(program.id)
        assert len(log) == 2
        assert log[1].action == AuditAction.VERSION_BUMPED
        assert log[1].details["old_version"] == 1
        assert log[1].details["new_version"] == 2

    def test_audit_record_is_frozen(self) -> None:
        record = AuditRecord(
            id=new_id(),
            program_id=new_id(),
            action=AuditAction.CREATED,
            performed_by="tester",
            timestamp=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            record.action = AuditAction.DEACTIVATED  # type: ignore[misc]

    def test_audit_records_are_chronological(self) -> None:
        mgr, program = _make_program()
        mgr.activate(program.id, performed_by="admin")
        mgr.bump_version(program.id, performed_by="admin")
        log = mgr.get_audit_log(program.id)
        timestamps = [r.timestamp for r in log]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# Background run isolation
# ---------------------------------------------------------------------------


class TestBackgroundRunIsolation:
    def test_program_has_service_identity_field(self) -> None:
        mgr = ProgramManager()
        program = mgr.create_program(
            name="bg-test",
            created_by="tester",
            risk_charter="test",
            service_identity="svc:discover:financial",
        )
        assert program.service_identity == "svc:discover:financial"

    def test_program_without_service_identity(self) -> None:
        _mgr, program = _make_program()
        assert program.service_identity is None

    def test_version_has_service_identity(self) -> None:
        mgr = ProgramManager()
        program = mgr.create_program(
            name="bg-test",
            created_by="tester",
            risk_charter="test",
            service_identity="svc:discover:financial",
        )
        version = mgr.get_version(program.current_version_id)
        assert version.service_identity == "svc:discover:financial"

    def test_version_inherits_service_identity_on_bump(self) -> None:
        mgr = ProgramManager()
        program = mgr.create_program(
            name="bg-test",
            created_by="tester",
            risk_charter="test",
            service_identity="svc:discover:financial",
        )
        new_version = mgr.bump_version(program.id, performed_by="admin")
        assert new_version.service_identity == "svc:discover:financial"


# ---------------------------------------------------------------------------
# Trigger types
# ---------------------------------------------------------------------------


class TestTriggerTypes:
    def test_schedule_trigger_has_cron(self) -> None:
        trigger = ScheduleTrigger(cron_expression="0 9 * * 1-5")
        assert trigger.type == TriggerType.SCHEDULE
        assert trigger.cron_expression == "0 9 * * 1-5"
        assert trigger.timezone == "UTC"

    def test_schedule_trigger_validates_cron_fields(self) -> None:
        with pytest.raises(ValidationError):
            ScheduleTrigger(cron_expression="invalid")

    def test_webhook_trigger_has_path_and_secret(self) -> None:
        trigger = WebhookTrigger(
            path="/webhook/incoming",
            secret_digest="sha256:abc123",
        )
        assert trigger.type == TriggerType.WEBHOOK
        assert trigger.path == "/webhook/incoming"

    def test_webhook_trigger_rejects_empty_path(self) -> None:
        with pytest.raises(ValidationError):
            WebhookTrigger(path="", secret_digest="sha256:abc123")

    def test_source_delta_trigger_has_source_id(self) -> None:
        source_id = new_id()
        trigger = SourceDeltaTrigger(
            source_id=source_id,
            watermark_field="updated_at",
        )
        assert trigger.type == TriggerType.SOURCE_DELTA
        assert trigger.source_id == source_id

    def test_trigger_is_frozen(self) -> None:
        trigger = ScheduleTrigger(cron_expression="0 9 * * 1-5")
        with pytest.raises(ValidationError):
            trigger.cron_expression = "0 10 * * *"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Trigger records
# ---------------------------------------------------------------------------


class TestTriggerRecord:
    def test_trigger_record_binds_to_version(self) -> None:
        version_id = new_id()
        trigger = ScheduleTrigger(cron_expression="0 9 * * 1-5")
        record = TriggerRecord(
            id=new_id(),
            program_version_id=version_id,
            trigger=trigger,
            created_at=datetime.now(UTC),
        )
        assert record.program_version_id == version_id
        assert record.state == TriggerState.IDLE
        assert record.trigger.type == TriggerType.SCHEDULE


# ---------------------------------------------------------------------------
# Signal types
# ---------------------------------------------------------------------------


class TestSignalTypes:
    def test_signal_is_immutable(self) -> None:
        signal = Signal(
            id=new_id(),
            program_version_id=new_id(),
            detector_pack_id=new_id(),
            detector_pack_version=1,
            severity=SignalSeverity.WARNING,
            title="Anomalous spending detected",
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            signal.title = "changed"  # type: ignore[misc]

    def test_signal_links_to_program_version(self) -> None:
        pvid = new_id()
        signal = Signal(
            id=new_id(),
            program_version_id=pvid,
            detector_pack_id=new_id(),
            detector_pack_version=1,
            severity=SignalSeverity.HIGH,
            title="Test signal",
            created_at=datetime.now(UTC),
        )
        assert signal.program_version_id == pvid

    def test_signal_links_to_detector_pack(self) -> None:
        dp_id = new_id()
        signal = Signal(
            id=new_id(),
            program_version_id=new_id(),
            detector_pack_id=dp_id,
            detector_pack_version=3,
            severity=SignalSeverity.CRITICAL,
            title="Test signal",
            created_at=datetime.now(UTC),
        )
        assert signal.detector_pack_id == dp_id
        assert signal.detector_pack_version == 3

    def test_signal_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            Signal(
                id=new_id(),
                program_version_id=new_id(),
                detector_pack_id=new_id(),
                detector_pack_version=1,
                severity=SignalSeverity.INFO,
                title="test",
                created_at=datetime.now(UTC),
                bogus=True,  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# Signal chain (immutable linked version)
# ---------------------------------------------------------------------------


class TestSignalChain:
    def test_chain_links_signals(self) -> None:
        sig1 = new_id()
        sig2 = new_id()
        chain = SignalChain(
            root_signal_id=sig1,
            chain=(sig1, sig2),
            latest_signal_id=sig2,
            created_at=datetime.now(UTC),
        )
        assert chain.root_signal_id == sig1
        assert chain.latest_signal_id == sig2
        assert len(chain.chain) == 2

    def test_chain_is_frozen(self) -> None:
        chain = SignalChain(
            root_signal_id=new_id(),
            chain=(new_id(),),
            latest_signal_id=new_id(),
            created_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            chain.latest_signal_id = new_id()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Data quality results
# ---------------------------------------------------------------------------


class TestDataQualityResult:
    def test_dq_result_is_frozen(self) -> None:
        result = DataQualityResult(
            check_name="schema_valid",
            passed=True,
            row_count=100,
        )
        with pytest.raises(ValidationError):
            result.passed = False  # type: ignore[misc]

    def test_dq_result_default_passed(self) -> None:
        result = DataQualityResult(check_name="row_count_check")
        assert result.passed is False


# ---------------------------------------------------------------------------
# Watermark
# ---------------------------------------------------------------------------


class TestWatermark:
    def test_watermark_is_frozen(self) -> None:
        wm = Watermark(
            source_id=new_id(),
            field_name="updated_at",
            value="2025-01-01T00:00:00Z",
            captured_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            wm.value = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# NegativeProbe (ADR-004)
# ---------------------------------------------------------------------------


class TestNegativeProbe:
    def test_probe_is_typed_not_free_text(self) -> None:
        """ADR-004: probe 必须 typed，断言归约为可机器求值的结构。"""
        probe = NegativeProbe(
            probe_id=new_id(),
            metric="transaction_count",
            entity_scope="vendor:acme-corp",
            window_hours=24,
            comparator="gt",
            threshold=1000.0,
            description="If hypothesis is false, vendor transaction count should exceed 1000",
        )
        assert probe.metric == "transaction_count"
        assert probe.comparator == "gt"
        assert probe.threshold == 1000.0

    def test_probe_is_frozen(self) -> None:
        probe = NegativeProbe(
            probe_id=new_id(),
            metric="test",
            entity_scope="test",
            window_hours=1,
            comparator="gt",
            threshold=0.0,
        )
        with pytest.raises(ValidationError):
            probe.metric = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FalsificationResult (ADR-004)
# ---------------------------------------------------------------------------


class TestFalsificationResult:
    def test_falsification_result_links_to_probe(self) -> None:
        probe = NegativeProbe(
            probe_id=new_id(),
            metric="spend_delta_pct",
            entity_scope="all",
            window_hours=7,
            comparator="gt",
            threshold=50.0,
        )
        result = FalsificationResult(
            probe=probe,
            passed=True,
            actual_value=12.5,
            evaluated_at=datetime.now(UTC),
        )
        assert result.probe.metric == "spend_delta_pct"
        assert result.passed is True
        assert result.actual_value == 12.5

    def test_falsification_result_deterministic_only(self) -> None:
        """ADR-004: 求值一律由确定性组件完成。"""
        result = FalsificationResult(
            probe=NegativeProbe(
                probe_id=new_id(),
                metric="x",
                entity_scope="y",
                window_hours=1,
                comparator="gt",
                threshold=0.0,
            ),
            passed=True,
            evaluated_at=datetime.now(UTC),
        )
        assert result.evaluation_method == "deterministic"

    def test_falsification_result_is_frozen(self) -> None:
        result = FalsificationResult(
            probe=NegativeProbe(
                probe_id=new_id(),
                metric="x",
                entity_scope="y",
                window_hours=1,
                comparator="gt",
                threshold=0.0,
            ),
            passed=True,
            evaluated_at=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            result.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FalsificationStandard (ADR-004)
# ---------------------------------------------------------------------------


class TestFalsificationStandard:
    def test_default_min_probes(self) -> None:
        std = FalsificationStandard()
        assert std.min_probes_required >= 1

    def test_deterministic_evaluation_only(self) -> None:
        std = FalsificationStandard()
        assert std.deterministic_evaluation_only is True

    def test_standard_is_frozen(self) -> None:
        std = FalsificationStandard(min_probes_required=5)
        with pytest.raises(ValidationError):
            std.min_probes_required = 3  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Signal with falsification results
# ---------------------------------------------------------------------------


class TestSignalWithFalsification:
    def test_signal_carrying_falsification_results(self) -> None:
        probe = NegativeProbe(
            probe_id=new_id(),
            metric="anomaly_score",
            entity_scope="dept:finance",
            window_hours=48,
            comparator="gt",
            threshold=0.8,
        )
        fals_result = FalsificationResult(
            probe=probe,
            passed=False,
            actual_value=0.3,
            evaluated_at=datetime.now(UTC),
        )
        signal = Signal(
            id=new_id(),
            program_version_id=new_id(),
            detector_pack_id=new_id(),
            detector_pack_version=1,
            severity=SignalSeverity.HIGH,
            title="Potential fraud pattern",
            falsification_results=(fals_result,),
            created_at=datetime.now(UTC),
        )
        assert len(signal.falsification_results) == 1
        assert signal.falsification_results[0].passed is False
        assert signal.falsification_results[0].actual_value == 0.3
