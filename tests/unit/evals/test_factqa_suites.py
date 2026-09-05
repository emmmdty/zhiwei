"""S6 factqa-v1 suite 契约：冻结题集 → Evidence/SQL regression。

事实源：specs/s6-evidence-ask.md §6（「迁移旧 factqa-v1 为 Evidence/SQL regression，
不改变冻结资产」）、AGENTS.md 评测先行纪律。

契约面：
- suite `factqa-v1` 可解析；registered units 与冻结题集 120 行对齐，
  (sample_id, unit_id) = (id, independence_unit_id)，F5 chain 共享 unit；
- loader 对 evals/ 冻结资产严格只读（加载前后内容 digest 不变）；
- executor 对 SQL 类题（F1/F3/F4）走生产 Evidence 路径：同一冻结 snapshot 重放
  SQL → QueryReplay EvidenceRef + canonical value → verify_bundle 复算 claim，
  判分 = 验证结果与冻结 ground truth 的一致性；
- F2/F5/F6 无冻结 SQL，走确定性行为标签（scoring_basis 如实声明），不虚构 Evidence；
- 全部 120 个注册单位必须 terminal 且 verdict=pass（冻结语料上的确定性回归）；
- 同一单位两次执行 result digest 逐字节一致。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from zhiwei.evals.domain import RegisteredUnit, SampleStatus
from zhiwei.evals.executors.factqa import (
    FactQAEvidenceExecutor,
    materialize_snapshot,
)
from zhiwei.evals.factqa_suites import (
    FACTQA_V1,
    resolve_factqa_suite,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_QUESTION_FILES = (
    _REPO_ROOT / "evals" / "questions" / "shuihu.jsonl",
    _REPO_ROOT / "evals" / "questions" / "xiyouji.jsonl",
    _REPO_ROOT / "evals" / "questions" / "manual" / "shuihu.jsonl",
    _REPO_ROOT / "evals" / "questions" / "manual" / "xiyouji.jsonl",
)


def _frozen_digests() -> dict[str, str]:
    return {
        str(p.relative_to(_REPO_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in _QUESTION_FILES
    }


def _units_by_sample() -> dict[str, RegisteredUnit]:
    return {
        unit.sample_id: unit
        for unit in resolve_factqa_suite().registered_units
    }


class TestFactqaSuiteRegistry:
    def test_suite_name_is_factqa_v1(self) -> None:
        suite = resolve_factqa_suite()
        assert suite.name == FACTQA_V1

    def test_registered_units_align_with_frozen_questions(self) -> None:
        suite = resolve_factqa_suite()
        questions: list[dict[str, object]] = []
        for path in _QUESTION_FILES:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    questions.append(json.loads(line))
        assert len(suite.registered_units) == len(questions) == 120
        by_id = {str(q["id"]): q for q in questions}
        for unit in suite.registered_units:
            question = by_id[unit.sample_id]
            assert unit.unit_id == str(question["independence_unit_id"])

    def test_chain_turns_share_one_unit(self) -> None:
        suite = resolve_factqa_suite()
        unit_ids = {unit.unit_id for unit in suite.registered_units}
        # 120 行题 = 112 个 independence unit（108 单轮 + 4 条 F5 chain）
        assert len(unit_ids) == 112
        chain_units = [
            unit for unit in suite.registered_units if unit.sample_id.endswith(("-T1", "-T2", "-T3"))
        ]
        assert chain_units, "F5 chain 的三个 turn 必须注册为独立 sample"
        for unit in chain_units:
            assert unit.unit_id.startswith(("SH-F5-C", "XY-F5-C"))

    def test_unknown_suite_fails_closed(self) -> None:
        with pytest.raises(LookupError):
            resolve_factqa_suite("factqa-v2")

    def test_loader_is_read_only_over_frozen_assets(self) -> None:
        before = _frozen_digests()
        resolve_factqa_suite()
        after = _frozen_digests()
        assert before == after

    def test_corpus_digest_is_content_addressed(self) -> None:
        suite = resolve_factqa_suite()
        assert suite.corpus_digest.startswith("sha256:")
        assert len(suite.corpus_digest) == len("sha256:") + 64


class TestSnapshotMaterialization:
    def test_shuihu_snapshot_has_108_rows(self) -> None:
        snapshot = materialize_snapshot("shuihu")
        assert snapshot.digest.startswith("sha256:")
        rows = snapshot.connection.execute("SELECT COUNT(*) FROM liangshan").fetchone()
        assert rows is not None and rows[0] == 108

    def test_xiyouji_snapshot_has_81_rows(self) -> None:
        snapshot = materialize_snapshot("xiyouji")
        assert snapshot.digest.startswith("sha256:")
        rows = snapshot.connection.execute("SELECT COUNT(*) FROM nan").fetchone()
        assert rows is not None and rows[0] == 81

    def test_snapshot_digest_is_deterministic(self) -> None:
        first = materialize_snapshot("shuihu")
        second = materialize_snapshot("shuihu")
        assert first.digest == second.digest


class TestFactqaExecutorEvidencePath:
    """SQL 类题（F1/F3/F4）必须走生产 Evidence 路径并产出可验证 bundle。"""

    @pytest.fixture(autouse=True)
    def _setup_executor(self) -> Iterator[None]:
        self._executor = FactQAEvidenceExecutor(resolve_factqa_suite())
        yield

    def _execute(self, sample_id: str) -> object:
        unit = _units_by_sample()[sample_id]
        return asyncio.run(self._executor.execute(unit))  # type: ignore[attr-defined]

    def test_sql_backed_unit_builds_verified_replayable_evidence(self) -> None:
        outcome = self._execute("SH-F1-001")  # type: ignore[attr-defined]
        assert outcome.status == SampleStatus.COMPLETED  # type: ignore[attr-defined]
        result = outcome.result  # type: ignore[attr-defined]
        assert result["scoring_basis"] == "evidence-replay"
        assert result["verdict"] == "pass"
        evidence = result["evidence"]
        assert evidence["ref_type"] == "QueryReplay"
        assert evidence["reproducibility_level"] == "replayable"
        assert evidence["verification_exit_code"] == 0
        assert evidence["verification_ok"] is True
        assert evidence["replay_byte_identical"] is True
        assert evidence["bundle_digest"].startswith("sha256:")

    def test_replayed_value_matches_frozen_ground_truth(self) -> None:
        outcome = self._execute("SH-F1-001")  # type: ignore[attr-defined]
        result = outcome.result  # type: ignore[attr-defined]
        assert result["observed_value"] == "秦明"
        assert result["expected_answer"] == "秦明"

    def test_number_answer_rounds_like_validator(self) -> None:
        outcome = self._execute("SH-F3-023")  # type: ignore[attr-defined]
        result = outcome.result  # type: ignore[attr-defined]
        assert result["verdict"] == "pass"

    def test_set_answer_matches_sorted_ground_truth(self) -> None:
        outcome = self._execute("SH-F3-030")  # type: ignore[attr-defined]
        result = outcome.result  # type: ignore[attr-defined]
        assert result["verdict"] == "pass"

    def test_same_unit_executes_byte_identically(self) -> None:
        unit = _units_by_sample()["SH-F1-001"]
        first = asyncio.run(self._executor.execute(unit))  # type: ignore[attr-defined]
        second = asyncio.run(self._executor.execute(unit))  # type: ignore[attr-defined]
        assert first.result_digest == second.result_digest  # type: ignore[attr-defined]


class TestFactqaExecutorBehaviorLabels:
    """F2/F5/F6 无冻结 SQL：确定性标签判分，不虚构 Evidence。"""

    @pytest.fixture(autouse=True)
    def _setup_executor(self) -> Iterator[None]:
        self._executor = FactQAEvidenceExecutor(resolve_factqa_suite())
        yield

    def _execute(self, sample_id: str) -> object:
        unit = _units_by_sample()[sample_id]
        return asyncio.run(self._executor.execute(unit))  # type: ignore[attr-defined]

    def test_conflict_unit_reports_divergence(self) -> None:
        outcome = self._execute("SH-F2-001")  # type: ignore[attr-defined]
        result = outcome.result  # type: ignore[attr-defined]
        assert result["scoring_basis"] == "behavior-label"
        assert result["observed_label"] == "conflict"
        assert result["verdict"] == "pass"

    def test_agreement_negative_never_reports_conflict(self) -> None:
        outcome = self._execute("SH-F2-005")  # type: ignore[attr-defined]
        result = outcome.result  # type: ignore[attr-defined]
        assert result["observed_label"] == "consistent"
        assert result["verdict"] == "pass"

    def test_chain_unit_resolves_anchors_on_snapshot(self) -> None:
        outcome = self._execute("SH-F5-C1-T1")  # type: ignore[attr-defined]
        result = outcome.result  # type: ignore[attr-defined]
        assert result["scoring_basis"] == "chain-anchor"
        assert result["verdict"] == "pass"
        assert result["chain_turns_declared"] == 3
        # SH-F5-C1 的文本锚点（卢俊义/总兵都头领）必须可在 snapshot 解析；
        # 数值锚点（座次 2）只声明存在，不虚构解析。
        assert result["chain_turns_resolved"] == 2

    def test_unanswerable_unit_abstains(self) -> None:
        outcome = self._execute("SH-F6-001")  # type: ignore[attr-defined]
        result = outcome.result  # type: ignore[attr-defined]
        assert result["scoring_basis"] == "behavior-label"
        assert result["observed_label"] == "abstain"
        assert result["verdict"] == "pass"
        assert result["claims_produced"] == 0

    def test_unregistered_unit_fails(self) -> None:
        outcome = asyncio.run(  # type: ignore[attr-defined]
            self._executor.execute(
                RegisteredUnit(sample_id="SH-F1-999", unit_id="SH-F1-999")
            )
        )
        assert outcome.status == SampleStatus.FAILED  # type: ignore[attr-defined]


class TestFactqaFullRegression:
    """冻结语料上的全量确定性回归：120 单位全部 terminal + pass。"""

    def test_all_120_units_pass(self) -> None:
        suite = resolve_factqa_suite()
        executor = FactQAEvidenceExecutor(suite)
        for unit in suite.registered_units:
            outcome = asyncio.run(executor.execute(unit))
            assert outcome.status == SampleStatus.COMPLETED, (
                f"{unit.sample_id}: {outcome.result.get('failures')}"
            )
            assert outcome.result["verdict"] == "pass", unit.sample_id
