"""S6 ask-v1：Ask 行为契约场景（代码定义的 eval 单位，驱动 Ask SolutionPack task graph）。

事实源：specs/s6-evidence-ask.md §4/§6（cross-source task、clarification、conflict、
unanswerable、Fact vs Inference、partial/abstain）、solution-packs/ask/task_graph.yaml、
ADR-003、ADR-005。

场景图保持 Ask SolutionPack 的拓扑（intake→plan→retrieve×3→analyze→verify→
synthesize→emit→finish），节点 id 以场景前缀隔离行为；handler 为零模型调用的
fixture 实现（fixture planner 模式），但关键节点复用生产行为：

- ``verify_evidence`` 节点（AskVerify）内部调用生产 ``VerifyHandler`` 对场景
  bundle 复算——fixture 只提供数据，验证逻辑是生产代码；
- ``synthesize_answer`` 节点使用生产 primitive 名 ``Synthesize``，使 ADR-005 的
  「存在未解决 conflict 不得产出正常合成输出」降级门真实生效；
- 并行 retrieve 分支的合并经生产 reducer（APPEND / CONFLICT_PRESERVING）。

invariants 只读 reduced RunState 与事件序列（真相在 PG canonical projection）。
Claim boundary：fixture handler 实现 Ask 答案策略的形态（abstain/partial/clarify），
offline 不声称答案合成质量——那需要 live 模型。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict

from zhiwei.agents.task_graph import (
    MergeStrategy,
    TaskGraph,
    TaskGraphNode,
)
from zhiwei.evals.domain import RegisteredUnit
from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.canonical_values import (
    ReproducibilityLevel,
    make_canonical_int,
)
from zhiwei.evidence.claims import FactClaim
from zhiwei.evidence.refs import QueryReplayRef
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput
from zhiwei.runtime.handlers.registry import TaskHandlerRegistry
from zhiwei.runtime.handlers.verify import VerifyHandler
from zhiwei.runtime.reducer import RunState

ASK_V1_SUITE = "ask-v1"

_ASK_NAMESPACE = uuid5(NAMESPACE_URL, "zhiwei:evals:ask:v1")
_PACK_FROZEN_AT = "2026-09-04T00:00:00+00:00"
_FROZEN_TS = datetime(2026, 9, 4, tzinfo=UTC)

ASK_V1_UNITS: tuple[RegisteredUnit, ...] = tuple(
    RegisteredUnit(sample_id="ask/graph", unit_id=unit_id)
    for unit_id in (
        "cross-source",
        "unanswerable-abstain",
        "conflict-side-by-side",
        "fact-without-evidence",
        "needs-clarification",
        "partial-with-unknowns",
    )
)


# --------------------------------------------------------------------- 场景数据


def _uid(name: str) -> UUID:
    return uuid5(_ASK_NAMESPACE, name)


def _answer_digest() -> str:
    from zhiwei.contracts.canonical import digest

    return digest({"ask": "scenario-answer-v1"})


def _scenario_bundle(prefix: str) -> dict[str, Any]:
    """可验证的 cross-source 场景 bundle（Fact claim 绑定 replayable 证据）。"""
    ref = QueryReplayRef(
        ref_id=_uid(f"{prefix}:ref"),
        reproducibility_level=ReproducibilityLevel.REPLAYABLE,
        source_id=_uid(f"{prefix}:source"),
        snapshot_digest="sha256:" + "7a" * 32,
        created_at=_FROZEN_TS,
        sql="SELECT name FROM liangshan WHERE rank = ?",
        params={"positional": [7]},
    )
    claim = FactClaim(
        claim_id=_uid(f"{prefix}:claim"),
        answer_id=_uid(f"{prefix}:answer"),
        evidence_refs=(ref,),
        answer_digest=_answer_digest(),
        canonical_value=make_canonical_int(45),
        created_at=_FROZEN_TS,
        updated_at=_FROZEN_TS,
    )
    bundle = EvidenceBundle(
        bundle_id=_uid(f"{prefix}:bundle"),
        answer_id=claim.answer_id,
        evidence_refs=(ref,),
        claims=(claim,),
        created_at=_FROZEN_TS,
        schema_version=1,
    )
    return bundle.model_dump(mode="json")


def invalid_fact_bundle_dict() -> dict[str, Any]:
    """wire 层可下发的违规 bundle：Fact claim 绑定 reference_only ref。

    FactClaim 模型层不可构造该组合（ADR-003）——只能经 wire 注入，是
    「Fact 无有效 Evidence 不能 final」不变量的被测输入。
    """
    prefix = "fact-without-evidence"
    ref = QueryReplayRef(
        ref_id=_uid(f"{prefix}:ref"),
        reproducibility_level=ReproducibilityLevel.REPLAYABLE,
        source_id=_uid(f"{prefix}:source"),
        snapshot_digest="sha256:" + "7a" * 32,
        created_at=_FROZEN_TS,
        sql="SELECT name FROM liangshan WHERE rank = ?",
        params={"positional": [7]},
    )
    claim = FactClaim(
        claim_id=_uid(f"{prefix}:claim"),
        answer_id=_uid(f"{prefix}:answer"),
        evidence_refs=(ref,),
        answer_digest=_answer_digest(),
        canonical_value=make_canonical_int(45),
        created_at=_FROZEN_TS,
        updated_at=_FROZEN_TS,
    )
    bundle = EvidenceBundle(
        bundle_id=_uid(f"{prefix}:bundle"),
        answer_id=claim.answer_id,
        evidence_refs=(ref,),
        claims=(claim,),
        created_at=_FROZEN_TS,
        schema_version=1,
    )
    raw = bundle.model_dump(mode="json")
    doc_ref = dict(raw["evidence_refs"][0])
    doc_ref.update(
        {
            "ref_type": "DocRef",
            "ref_id": str(_uid(f"{prefix}:doc-ref")),
            "reproducibility_level": "reference_only",
            "document_uri": "docs/zhaoan.md",
        }
    )
    doc_ref.pop("sql", None)
    doc_ref.pop("params", None)
    raw["evidence_refs"] = [raw["evidence_refs"][0], doc_ref]
    raw["claims"][0]["evidence_refs"] = [doc_ref]
    return raw


def scenario_bundle_dict(prefix: str) -> dict[str, Any]:
    """场景 verify 节点的 bundle 输入；cross-source 为可验证 bundle。"""
    return _scenario_bundle(prefix)


_ANALYZE_OUTPUTS: dict[str, dict[str, Any]] = {
    "cross-source": {"claims": ["cross-source/claim"], "conflicts": [], "unknowns": []},
    "unanswerable-abstain": {
        "claims": [],
        "conflicts": [],
        "unknowns": ["unanswerable-abstain: 数据中不存在该条目"],
    },
    "conflict-side-by-side": {
        "claims": [],
        "conflicts": ["finding_value"],
        "unknowns": [],
    },
    "fact-without-evidence": {
        "claims": ["fact-without-evidence/fact"],
        "conflicts": [],
        "unknowns": [],
    },
    "needs-clarification": {
        "claims": [],
        "conflicts": [],
        "clarification": {
            "needed": True,
            "questions": ["needs-clarification: 需要哪个时间范围的口径？"],
        },
    },
    "partial-with-unknowns": {
        "claims": ["partial-with-unknowns/claim"],
        "conflicts": [],
        "unknowns": ["partial-with-unknowns: db 源不可用"],
    },
}

_ANSWER_OUTPUTS: dict[str, dict[str, Any]] = {
    "cross-source": {"status": "completed", "claims": ["cross-source/claim"]},
    "unanswerable-abstain": {"status": "abstained", "claims": []},
    # conflict 场景的 synthesize 节点会被生产降级门拦截，handler 不执行；
    # 若降级门失效，这里会产出 completed 并被 invariant 判失败。
    "conflict-side-by-side": {"status": "completed", "claims": []},
    "fact-without-evidence": {"status": "blocked_unverified", "claims": []},
    "needs-clarification": {"status": "needs_clarification"},
    "partial-with-unknowns": {"status": "partial", "unknowns": ["db 源不可用"]},
}

_VERIFY_INPUTS_REMOVED = None


def _verify_bundle_for(prefix: str) -> dict[str, Any] | None:
    if prefix == "cross-source":
        return _scenario_bundle(prefix)
    if prefix == "fact-without-evidence":
        return invalid_fact_bundle_dict()
    return None


# --------------------------------------------------------------------- 场景定义


class AskScenario(BaseModel):
    """一个 ask-v1 行为场景：场景图 + invariant 名 + 场景前缀。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit: RegisteredUnit
    graph: TaskGraph
    invariant: str

    @property
    def prefix(self) -> str:
        return self.unit.unit_id


def _node(
    task_id: str,
    task_type: str,
    *,
    deps: tuple[str, ...] = (),
    parallel: bool = False,
    merge: dict[str, MergeStrategy] | None = None,
) -> TaskGraphNode:
    return TaskGraphNode(
        task_id=task_id,
        task_type=task_type,
        dependencies=deps,
        parallel_safe=parallel,
        required_capability="fixture",
        output_merge_strategies=merge or {},
    )


def _ask_graph(
    prefix: str,
    *,
    retrieve_types: tuple[str, str, str],
    conflict_field: bool = False,
) -> TaskGraph:
    """Ask SolutionPack 拓扑（solution-packs/ask/task_graph.yaml）的场景实例。

    conflict_field=True 时前两个 retrieve 分支额外以 CONFLICT_PRESERVING 写
    finding_value（ADR-005：每个并行写者都必须声明策略）。
    """
    ids = {name: f"{prefix}/{name}" for name in (
        "intake", "plan", "retrieve_docs", "retrieve_code", "retrieve_db",
        "analyze_findings", "verify_evidence", "synthesize_answer",
        "emit_answer", "finish",
    )}
    retrieves = ("retrieve_docs", "retrieve_code", "retrieve_db")

    def _merge(task_type: str) -> dict[str, MergeStrategy]:
        merge = {"findings": MergeStrategy.APPEND}
        if conflict_field and task_type in ("AskRetrieveConflictA", "AskRetrieveConflictB"):
            merge["finding_value"] = MergeStrategy.CONFLICT_PRESERVING
        return merge

    return TaskGraph(
        nodes={
            ids["intake"]: _node(ids["intake"], "AskIntake"),
            ids["plan"]: _node(ids["plan"], "AskPlan", deps=(ids["intake"],)),
            ids["retrieve_docs"]: _node(
                ids["retrieve_docs"], retrieve_types[0],
                deps=(ids["plan"],), parallel=True, merge=_merge(retrieve_types[0]),
            ),
            ids["retrieve_code"]: _node(
                ids["retrieve_code"], retrieve_types[1],
                deps=(ids["plan"],), parallel=True, merge=_merge(retrieve_types[1]),
            ),
            ids["retrieve_db"]: _node(
                ids["retrieve_db"], retrieve_types[2],
                deps=(ids["plan"],), parallel=True, merge=_merge(retrieve_types[2]),
            ),
            ids["analyze_findings"]: _node(
                ids["analyze_findings"], "AskAnalyze",
                deps=tuple(ids[name] for name in retrieves),
            ),
            ids["verify_evidence"]: _node(
                ids["verify_evidence"], "AskVerify",
                deps=(ids["analyze_findings"],),
            ),
            ids["synthesize_answer"]: _node(
                ids["synthesize_answer"], "Synthesize",
                deps=(ids["verify_evidence"],),
            ),
            ids["emit_answer"]: _node(
                ids["emit_answer"], "AskEmitArtifact",
                deps=(ids["synthesize_answer"],),
            ),
            ids["finish"]: _node(
                ids["finish"], "AskFinish",
                deps=(ids["emit_answer"],),
            ),
        },
        edges={
            ids["plan"]: [ids["intake"]],
            ids["retrieve_docs"]: [ids["plan"]],
            ids["retrieve_code"]: [ids["plan"]],
            ids["retrieve_db"]: [ids["plan"]],
            ids["analyze_findings"]: [
                ids["retrieve_docs"], ids["retrieve_code"], ids["retrieve_db"],
            ],
            ids["verify_evidence"]: [ids["analyze_findings"]],
            ids["synthesize_answer"]: [ids["verify_evidence"]],
            ids["emit_answer"]: [ids["synthesize_answer"]],
            ids["finish"]: [ids["emit_answer"]],
        },
    )


def _scenario(
    unit_index: int,
    invariant: str,
    retrieve_types: tuple[str, str, str],
    *,
    conflict_field: bool = False,
) -> AskScenario:
    unit = ASK_V1_UNITS[unit_index]
    return AskScenario(
        unit=unit,
        graph=_ask_graph(
            unit.unit_id, retrieve_types=retrieve_types, conflict_field=conflict_field
        ),
        invariant=invariant,
    )


ASK_CONTRACT_SCENARIOS: tuple[AskScenario, ...] = (
    _scenario(0, "cross_source_findings_present", ("AskRetrieveDocs", "AskRetrieveCode", "AskRetrieveDB")),
    _scenario(1, "unanswerable_abstains", ("AskRetrieveNone", "AskRetrieveNone", "AskRetrieveNone")),
    _scenario(
        2,
        "conflict_preserved_not_arbitrated",
        ("AskRetrieveConflictA", "AskRetrieveConflictB", "AskRetrieveNone"),
        conflict_field=True,
    ),
    _scenario(3, "unverified_fact_blocks_final", ("AskRetrieveDocs", "AskRetrieveCode", "AskRetrieveDB")),
    _scenario(4, "insufficient_sources_require_clarification", ("AskRetrieveNone", "AskRetrieveNone", "AskRetrieveNone")),
    _scenario(5, "partial_reports_unknowns", ("AskRetrieveDocs", "AskRetrieveCode", "AskRetrieveNone")),
)

_SCENARIO_BY_UNIT: dict[tuple[str, str], AskScenario] = {
    (s.unit.sample_id, s.unit.unit_id): s for s in ASK_CONTRACT_SCENARIOS
}


def scenario_for_unit(unit: RegisteredUnit) -> AskScenario:
    try:
        return _SCENARIO_BY_UNIT[(unit.sample_id, unit.unit_id)]
    except KeyError as exc:
        raise LookupError(f"unknown ask contract unit: {unit.sample_id}/{unit.unit_id}") from exc


# --------------------------------------------------------------------- fixture handlers


class AskIntakeHandler(TaskHandler):
    @property
    def primitive_type(self) -> str:
        return "AskIntake"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values={"question_id": input.task_id, "parsed_scope": {}})


class AskPlanHandler(TaskHandler):
    @property
    def primitive_type(self) -> str:
        return "AskPlan"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(
            output_values={"plan_id": input.task_id, "retrieval_steps": ["documents", "code", "db"]}
        )


class _AskFindingHandler(TaskHandler):
    """按 primitive 类型固定产出一个来源的 finding（APPEND 合并）。"""

    source_kind = "documents"

    def _primitive(self) -> str:  # pragma: no cover - 子类覆盖
        return "AskRetrieveDocs"

    @property
    def primitive_type(self) -> str:
        return self._primitive()

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(
            output_values={
                "findings": [{"source": self.source_kind, "task_id": input.task_id}]
            }
        )


class AskRetrieveDocsHandler(_AskFindingHandler):
    source_kind = "documents"

    def _primitive(self) -> str:
        return "AskRetrieveDocs"


class AskRetrieveCodeHandler(_AskFindingHandler):
    source_kind = "code"

    def _primitive(self) -> str:
        return "AskRetrieveCode"


class AskRetrieveDBHandler(_AskFindingHandler):
    source_kind = "db"

    def _primitive(self) -> str:
        return "AskRetrieveDB"


class AskRetrieveConflictHandler(_AskFindingHandler):
    """conflict 场景分支：写 conflict_preserving 字段 finding_value（值由子类定）。"""

    finding_value: int = 0

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(
            output_values={
                "finding_value": self.finding_value,
                "findings": [{"source": "conflict-branch", "task_id": input.task_id}],
            }
        )


class AskRetrieveConflictAHandler(AskRetrieveConflictHandler):
    finding_value = 4

    def _primitive(self) -> str:
        return "AskRetrieveConflictA"


class AskRetrieveConflictBHandler(AskRetrieveConflictHandler):
    finding_value = 6

    def _primitive(self) -> str:
        return "AskRetrieveConflictB"


class AskRetrieveNoneHandler(TaskHandler):
    """来源不可用：不产出 finding（source 缺失 → abstain/clarify/partial 的输入）。"""

    @property
    def primitive_type(self) -> str:
        return "AskRetrieveNone"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values={"candidates": [], "findings": []})


class AskAnalyzeHandler(TaskHandler):
    """analyze 节点：按场景前缀产出 findings 的 claims/conflicts/unknowns 形态。"""

    @property
    def primitive_type(self) -> str:
        return "AskAnalyze"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        prefix = input.task_id.split("/", 1)[0]
        output = _ANALYZE_OUTPUTS.get(prefix)
        if output is None:
            raise RuntimeError(f"unknown ask analyze scenario: {input.task_id}")
        return TaskOutput(output_values=dict(output))


class AskVerifyHandler(TaskHandler):
    """verify 节点：调用生产 VerifyHandler 复算场景 bundle（fixture 只供数据）。"""

    _production = VerifyHandler()

    @property
    def primitive_type(self) -> str:
        return "AskVerify"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        prefix = input.task_id.split("/", 1)[0]
        bundle = _verify_bundle_for(prefix)
        if bundle is None:
            return TaskOutput(
                output_values={
                    "verification": {
                        "verification_ok": True,
                        "exit_code": 0,
                        "claims_verified": 0,
                    },
                    "verified_claims": [],
                    "failed_claims": [],
                }
            )
        result = self._production.execute(
            TaskInput(
                task_id=input.task_id,
                attempt_id=input.attempt_id,
                input_values={"bundle": bundle},
            )
        )
        values = result.output_values
        return TaskOutput(
            output_values={
                "verification": {
                    "verification_ok": values.get("verification_ok"),
                    "exit_code": values.get("exit_code"),
                    "check_count": values.get("check_count"),
                },
                "verified_claims": [] if not values.get("verification_ok") else ["claim"],
                "failed_claims": [] if values.get("verification_ok") else ["claim"],
            }
        )


class AskSynthesizeHandler(TaskHandler):
    """synthesize 节点：生产 primitive 名 Synthesize——存在未解决 conflict 时
    会被 ADR-005 降级门拦截（handler 不执行、canonical 落 synthesize_downgraded）。"""

    @property
    def primitive_type(self) -> str:
        return "Synthesize"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        prefix = input.task_id.split("/", 1)[0]
        answer = _ANSWER_OUTPUTS.get(prefix)
        if answer is None:
            raise RuntimeError(f"unknown ask synthesize scenario: {input.task_id}")
        return TaskOutput(output_values={"answer": dict(answer)})


class AskEmitArtifactHandler(TaskHandler):
    @property
    def primitive_type(self) -> str:
        return "AskEmitArtifact"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values={"artifact_id": f"artifact:{input.task_id}"})


class AskFinishHandler(TaskHandler):
    @property
    def primitive_type(self) -> str:
        return "AskFinish"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values={"status": "completed"})


def build_ask_contract_registry() -> TaskHandlerRegistry:
    """注册全部 ask 契约 handler（primitive_type 唯一，重复注册拒绝）。"""
    registry = TaskHandlerRegistry()
    for handler in (
        AskIntakeHandler(),
        AskPlanHandler(),
        AskRetrieveDocsHandler(),
        AskRetrieveCodeHandler(),
        AskRetrieveDBHandler(),
        AskRetrieveConflictAHandler(),
        AskRetrieveConflictBHandler(),
        AskRetrieveNoneHandler(),
        AskAnalyzeHandler(),
        AskVerifyHandler(),
        AskSynthesizeHandler(),
        AskEmitArtifactHandler(),
        AskFinishHandler(),
    ):
        registry.register(handler)
    return registry


# --------------------------------------------------------------------- invariants


def _errors_cross_source(state: RunState, events: list[Any]) -> list[str]:
    errors: list[str] = []
    if state.status != "completed":
        errors.append(f"run status {state.status!r} != 'completed'")
    findings = state.canonical.get("findings")
    sources = {f.get("source") for f in findings} if isinstance(findings, list) else set()
    missing = {"documents", "code", "db"} - sources
    if missing:
        errors.append(f"cross-source completion 缺源: {sorted(missing)}")
    answer = state.canonical.get("answer")
    if not isinstance(answer, dict) or answer.get("status") != "completed":
        errors.append(f"answer status {answer!r} != 'completed'")
    return errors


def _errors_unanswerable(state: RunState, events: list[Any]) -> list[str]:
    errors: list[str] = []
    if state.status != "completed":
        errors.append(f"run status {state.status!r} != 'completed'")
    answer = state.canonical.get("answer")
    if not isinstance(answer, dict) or answer.get("status") != "abstained":
        errors.append(f"unanswerable 必须 abstain，got {answer!r}")
    elif answer.get("claims"):
        errors.append("abstain 不允许携带 claims")
    unknowns = state.canonical.get("unknowns")
    if not unknowns:
        errors.append("abstain 必须披露 unknowns，不允许静默空答")
    return errors


def _errors_conflict(state: RunState, events: list[Any]) -> list[str]:
    errors: list[str] = []
    if state.status != "completed":
        errors.append(f"run status {state.status!r} != 'completed'")
    conflicts = [c for c in state.conflicts if c.field == "finding_value"]
    if not conflicts:
        errors.append("并行分支冲突必须落 ConflictRecord，不允许静默仲裁")
    else:
        values: set[str] = set()
        for conflict in conflicts:
            values.update(str(v) for v in conflict.values.values())
        if len(values) < 2:
            errors.append(f"ConflictRecord 必须并列保留双方取值，got {values}")
    if state.canonical.get("synthesize_downgraded") is not True:
        errors.append(
            "存在未解决 conflict 时 Synthesize 必须被降级门拦截"
            "（canonical.synthesize_downgraded != true）"
        )
    return errors


def _errors_unverified_fact(state: RunState, events: list[Any]) -> list[str]:
    errors: list[str] = []
    if state.status != "completed":
        errors.append(f"run status {state.status!r} != 'completed'")
    verification = state.canonical.get("verification")
    if not isinstance(verification, dict):
        errors.append("verify 节点必须落 verification 结果")
    elif verification.get("verification_ok") is not False:
        errors.append("Fact 无有效 Evidence 必须验证失败")
    answer = state.canonical.get("answer")
    if isinstance(answer, dict) and answer.get("status") == "completed":
        errors.append("验证失败的 Fact claim 不允许 final（status=completed）")
    return errors


def _errors_clarification(state: RunState, events: list[Any]) -> list[str]:
    errors: list[str] = []
    if state.status != "completed":
        errors.append(f"run status {state.status!r} != 'completed'")
    answer = state.canonical.get("answer")
    if not isinstance(answer, dict) or answer.get("status") != "needs_clarification":
        errors.append(f"来源不足必须 needs_clarification，got {answer!r}")
    clarification = state.canonical.get("clarification")
    questions = clarification.get("questions") if isinstance(clarification, dict) else None
    if not questions:
        errors.append("clarify 必须给出澄清问题列表")
    return errors


def _errors_partial(state: RunState, events: list[Any]) -> list[str]:
    errors: list[str] = []
    if state.status != "completed":
        errors.append(f"run status {state.status!r} != 'completed'")
    answer = state.canonical.get("answer")
    if not isinstance(answer, dict) or answer.get("status") != "partial":
        errors.append(f"部分来源不可用必须 partial，got {answer!r}")
    unknowns = state.canonical.get("unknowns")
    if not unknowns:
        errors.append("partial 必须披露 unknowns，不允许静默补全")
    findings = state.canonical.get("findings")
    if not findings:
        errors.append("partial 仍必须呈现可用来源的 findings")
    return errors


_INVARIANTS: dict[str, Callable[[RunState, list[Any]], list[str]]] = {
    "cross_source_findings_present": _errors_cross_source,
    "unanswerable_abstains": _errors_unanswerable,
    "conflict_preserved_not_arbitrated": _errors_conflict,
    "unverified_fact_blocks_final": _errors_unverified_fact,
    "insufficient_sources_require_clarification": _errors_clarification,
    "partial_reports_unknowns": _errors_partial,
}


def check_invariant(name: str, state: RunState, events: list[Any]) -> list[str]:
    try:
        check = _INVARIANTS[name]
    except KeyError as exc:
        raise LookupError(f"unknown ask invariant: {name!r}") from exc
    return check(state, events)
