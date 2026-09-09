"""ChangeBrief pack runtime：检索规划 + Core task handler 工厂 + 生产验证链。

事实源：solution-packs/change-brief/task_graph.yaml（Retrieve/Analyze/Verify/
EmitArtifact/Finish 任务声明）、specs/s10 §4。

与 Core 的全部交互走公共扩展点：
- handler 经 zhiwei.runtime.handlers.base.TaskHandler 协议暴露，由 executor 按
  task_graph.yaml 的 primitive 类型注册进 TaskHandlerRegistry——与 ask_contracts
  的 fixture handler 同一机制，没有 ChangeBrief 专属 Core handler；
- Verify 链内部调用生产 VerifyHandler（zhiwei.runtime.handlers.verify）对从
  impact 报告构造的 EvidenceBundle 复算——CodeRef/GitHubRef 是 zhiwei.evidence
  的公共 ref 类型，验证逻辑是生产代码，pack 只供数据。

注入纪律：skill entry（impact_analysis.analyze_impact）以工厂参数注入而不是
跨模块 import——pack runtime 各模块经 executor 的 importlib 装载（pack 目录不是
python 包），显式注入让依赖方向在装载期可见。

重试安全：production runtime 在任务间传递 input_values={}，handler 从场景绑定
（按 task_id 前缀解析）确定性重推导输入——与 ask_contracts 的场景重推导同型；
全链路零模型调用、零墙钟（时钟钉在 pack 冻结日，id 全部 UUID5 派生）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from zhiwei.contracts.canonical import digest
from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.canonical_values import ReproducibilityLevel, make_canonical_text
from zhiwei.evidence.claims import FactClaim
from zhiwei.evidence.refs import CodeRef, GitHubRef
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput
from zhiwei.runtime.handlers.verify import VerifyHandler

# pack 冻结日（pack.yaml frozen_at）：evidence 构造钉在固定时刻，两次执行产出
# 逐字节一致的 bundle/claim/ref id 与时间戳。
_FROZEN_TS = datetime(2026, 9, 6, tzinfo=UTC)
_NAMESPACE = uuid5(NAMESPACE_URL, "zhiwei:pack:change-brief:v1")

AnalyzeImpactFn = Callable[[str, Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]
PlanRetrievalFn = Callable[[Mapping[str, Any]], dict[str, Any]]


def _uid(*parts: str) -> UUID:
    return uuid5(_NAMESPACE, ":".join(parts))


def _scenario(scenarios: Mapping[str, Mapping[str, Any]], prefix: str) -> dict[str, Any]:
    """按 task_id 前缀解析场景绑定；未知前缀 fail closed（不猜默认场景）。"""
    scenario = scenarios.get(prefix)
    if scenario is None:
        raise KeyError(f"change-brief pack: 未注册的场景前缀 {prefix!r}")
    return dict(scenario)


def _trigger(scenario: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    trigger = scenario["trigger"]
    return str(trigger["repository"]), dict(trigger["commit_or_pr"])


# ----------------------------------------------------------------- 检索规划


def plan_retrieval(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """场景绑定 → 检索候选集（代码知识快照的子集 + 触发上下文，确定性排序）。

    快照即检索结果：fixture 绑定的知识快照子集由生产检索语义物化，本函数只做
    结构校验与稳定排序——排序确定，下游分析的输入就确定。
    """
    snapshot = scenario.get("snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("symbols"):
        raise ValueError("change-brief pack: 场景缺少代码知识快照（snapshot.symbols）")
    files_changed = scenario.get("files_changed")
    if not isinstance(files_changed, list) or not files_changed:
        raise ValueError("change-brief pack: 场景缺少改动清单（files_changed）")
    candidates: dict[str, Any] = {
        # 触发上下文随候选集下发：analyze_impact 的触达面与检索结果同源。
        "files_changed": sorted(files_changed, key=lambda fc: str(fc.get("path", "")))
    }
    for key, sort_key in (
        ("symbols", lambda s: str(s.get("name", ""))),
        ("dependencies", lambda d: str(d.get("name", ""))),
        ("prs", lambda p: p.get("pr_number", 0)),
        ("issues", lambda i: i.get("issue_number", 0)),
        ("checks", lambda c: str(c.get("name", ""))),
    ):
        entries = snapshot.get(key) or []
        if not isinstance(entries, list):
            raise ValueError(f"change-brief pack: snapshot.{key} 必须是列表")
        candidates[key] = sorted(entries, key=sort_key)
    return candidates


# ----------------------------------------------------------------- handler 工厂


class _RetrieveHandler(TaskHandler):
    """Retrieve 任务：物化候选集并落 canonical（task_graph 的 candidates 输出）。"""

    def __init__(self, scenarios: Mapping[str, Mapping[str, Any]], plan: PlanRetrievalFn) -> None:
        self._scenarios = scenarios
        self._plan = plan

    @property
    def primitive_type(self) -> str:
        return "Retrieve"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        scenario = _scenario(self._scenarios, input.task_id.split("/", 1)[0])
        return TaskOutput(output_values={"candidates": self._plan(scenario)})


class _AnalyzeHandler(TaskHandler):
    """Analyze 任务：skill entry 对候选集做影响分析（impact 输出）。"""

    def __init__(
        self,
        scenarios: Mapping[str, Mapping[str, Any]],
        *,
        analyze_impact: AnalyzeImpactFn,
        plan: PlanRetrievalFn,
    ) -> None:
        self._scenarios = scenarios
        self._analyze_impact = analyze_impact
        self._plan = plan

    @property
    def primitive_type(self) -> str:
        return "Analyze"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        scenario = _scenario(self._scenarios, input.task_id.split("/", 1)[0])
        repository, commit_or_pr = _trigger(scenario)
        impact = self._analyze_impact(repository, commit_or_pr, self._plan(scenario))
        return TaskOutput(output_values={"impact": impact})


class _PackVerifyHandler(TaskHandler):
    """Verify 任务：impact 报告 → EvidenceBundle → 生产 VerifyHandler 复算。

    verified_impact 原样回传（Verify 不篡改分析结果），verification_result 携带
    生产验证判定（verification_ok/exit_code/check_count/bundle_id）。
    """

    _production = VerifyHandler()

    def __init__(
        self,
        scenarios: Mapping[str, Mapping[str, Any]],
        *,
        analyze_impact: AnalyzeImpactFn,
        plan: PlanRetrievalFn,
    ) -> None:
        self._scenarios = scenarios
        self._analyze_impact = analyze_impact
        self._plan = plan

    @property
    def primitive_type(self) -> str:
        return "Verify"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        prefix = input.task_id.split("/", 1)[0]
        scenario = _scenario(self._scenarios, prefix)
        repository, commit_or_pr = _trigger(scenario)
        impact = self._analyze_impact(repository, commit_or_pr, self._plan(scenario))
        verification = verify_impact(prefix, impact)
        return TaskOutput(
            output_values={
                "verification_result": verification,
                "verified_impact": impact,
            }
        )


class _EmitArtifactHandler(TaskHandler):
    """EmitArtifact 任务：brief artifact 的确定性标识与类型声明。"""

    @property
    def primitive_type(self) -> str:
        return "EmitArtifact"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(
            output_values={
                "artifact_id": f"artifact:{input.task_id}",
                "artifact_kind": "verified-brief",
                "schema_id": "verified-brief",
            }
        )


class _FinishHandler(TaskHandler):
    @property
    def primitive_type(self) -> str:
        return "Finish"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        return TaskOutput(output_values={"status": "completed"})


def build_retrieve_handler(
    scenarios: Mapping[str, Mapping[str, Any]],
    *,
    plan_retrieval: PlanRetrievalFn,
) -> TaskHandler:
    return _RetrieveHandler(scenarios, plan_retrieval)


def build_analyze_handler(
    scenarios: Mapping[str, Mapping[str, Any]],
    *,
    analyze_impact: AnalyzeImpactFn,
    plan_retrieval: PlanRetrievalFn,
) -> TaskHandler:
    return _AnalyzeHandler(scenarios, analyze_impact=analyze_impact, plan=plan_retrieval)


def build_verify_handler(
    scenarios: Mapping[str, Mapping[str, Any]],
    *,
    analyze_impact: AnalyzeImpactFn,
    plan_retrieval: PlanRetrievalFn,
) -> TaskHandler:
    return _PackVerifyHandler(scenarios, analyze_impact=analyze_impact, plan=plan_retrieval)


def build_emit_artifact_handler() -> TaskHandler:
    return _EmitArtifactHandler()


def build_finish_handler() -> TaskHandler:
    return _FinishHandler()


# ----------------------------------------------------------------- 生产验证链


def verify_impact(unit_prefix: str, impact: Mapping[str, Any]) -> dict[str, Any]:
    """impact 报告 → EvidenceBundle → 生产 VerifyHandler 的确定性验证结果。

    每个受影响符号一条 FactClaim，绑定该符号快照位置的 CodeRef；触发引用本身
    以 GitHubRef 进 bundle（no-impact 场景 claims 为空、仅携带触发 provenance）。
    全部 id 由 UUID5（pack namespace + unit + 内容键）派生，时钟钉在冻结日。
    """
    repository = str(impact["repository"])
    code_refs = [
        CodeRef(
            ref_id=_uid(unit_prefix, "code", str(ref["file_path"]), str(ref["line_start"])),
            reproducibility_level=ReproducibilityLevel.REPLAYABLE,
            source_id=_uid(unit_prefix, "source", repository),
            created_at=_FROZEN_TS,
            file_path=str(ref["file_path"]),
            line_start=int(ref["line_start"]),
            line_end=int(ref["line_end"]),
            code_digest=str(ref["code_digest"]),
        )
        for ref in impact["code_refs"]
    ]
    github_refs = [
        GitHubRef(
            ref_id=_uid(unit_prefix, "github", str(ref["path"])),
            reproducibility_level=ReproducibilityLevel.REPLAYABLE,
            source_id=_uid(unit_prefix, "source", repository),
            created_at=_FROZEN_TS,
            repository=str(ref["repository"]),
            commit_sha=ref.get("commit_sha"),
            pr_number=ref.get("pr_number"),
            path=str(ref["path"]),
        )
        for ref in impact["github_refs"]
    ]
    ref_by_span = {(ref.file_path, ref.line_start, ref.line_end): ref for ref in code_refs}
    claims = []
    for index, symbol in enumerate(impact["affected_symbols"]):
        span_key = (symbol["file_path"], symbol["line_start"], symbol["line_end"])
        ref = ref_by_span.get(span_key)
        if ref is None:
            # fail closed：受影响符号必须有可验证的代码证据，缺位置即拒绝。
            raise ValueError(
                f"change-brief pack: 受影响符号 {symbol['name']!r} 缺少 CodeRef 证据"
            )
        claims.append(
            FactClaim(
                claim_id=_uid(unit_prefix, "claim", str(symbol["name"]), str(index)),
                answer_id=_uid(unit_prefix, "answer"),
                evidence_refs=(ref,),
                answer_digest=digest(
                    {
                        "claim": "affected_symbol",
                        "repository": repository,
                        "ref": impact["commit_or_pr"]["ref"],
                        "symbol": symbol["name"],
                    }
                ),
                canonical_value=make_canonical_text(
                    f"{repository}:{impact['commit_or_pr']['ref']}:affected:{symbol['name']}"
                ),
                created_at=_FROZEN_TS,
                updated_at=_FROZEN_TS,
            )
        )
    bundle = EvidenceBundle(
        bundle_id=_uid(unit_prefix, "bundle"),
        answer_id=_uid(unit_prefix, "answer"),
        evidence_refs=(*code_refs, *github_refs),
        claims=tuple(claims),
        created_at=_FROZEN_TS,
        schema_version=1,
    )
    output = VerifyHandler().execute(
        TaskInput(
            task_id=f"change-brief:{unit_prefix}:verify",
            attempt_id=_uid(unit_prefix, "attempt"),
            input_values={"bundle": bundle.model_dump(mode="json")},
        )
    )
    values = output.output_values
    return {
        "verification_ok": values.get("verification_ok"),
        "exit_code": values.get("exit_code"),
        "check_count": values.get("check_count"),
        "bundle_id": str(bundle.bundle_id),
    }
