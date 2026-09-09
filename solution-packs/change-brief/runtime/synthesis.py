"""ChangeBrief pack runtime：VerifiedBrief 组装（schemas/verified-brief.yaml 契约）。

事实源：solution-packs/change-brief/schemas/verified-brief.yaml（brief 的 10 个
必需字段与 CodeRef/GitHubRef 词汇）、task_graph.yaml 的 synthesize_brief 任务
（inputs: verified_impact + verification_result → outputs: brief）。

组装纪律：
- brief 字段全部来自 verified_impact——Synthesize 不发明内容、不截断 unknowns；
- verification 失败时按 pack.yaml escalation_rules（verification_failed → high）
  追加风险，绝不静默产出「已验证」外观的 brief；
- 组装结果经 jsonschema 对 pack 自带的冻结 schema 校验，违规即抛
  ValidationError（fail closed）——brief 结构漂移在产出前被拒绝。

依赖纪律：pack runtime 约束（tests/architecture/test_app_boundaries.py）——
不导入 DB / model provider / 基础设施工具；schema 从 pack 目录相对解析。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput

BriefKeys = (
    "affected_symbols",
    "affected_dependencies",
    "affected_tests",
    "related_prs",
    "related_issues",
    "related_checks",
    "risks",
    "unknowns",
    "code_refs",
    "github_refs",
)

VerificationFailureRisk = {
    "description": "verification failed; brief claims are unverified",
    "severity": "high",
}


@cache
def _brief_schema() -> dict[str, Any]:
    """pack 自带冻结 schema（相对本模块解析——schema 归 pack 所有，不进 Core）。"""
    schema_path = Path(__file__).resolve().parents[1] / "schemas" / "verified-brief.yaml"
    declaration = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    if not isinstance(declaration, dict) or not isinstance(declaration.get("schema"), dict):
        raise ValueError("change-brief pack: verified-brief.yaml 结构非法")
    return declaration["schema"]


def synthesize_brief(
    verified_impact: Mapping[str, Any],
    verification_result: Mapping[str, Any],
) -> dict[str, Any]:
    """VerifiedBrief 组装：impact 字段 → brief 结构 → 冻结 schema 校验。"""
    brief: dict[str, Any] = {}
    for key in BriefKeys:
        if key not in verified_impact:
            raise ValueError(f"change-brief pack: impact 缺少 brief 字段 {key!r}")
        brief[key] = list(verified_impact[key])
    if verification_result.get("verification_ok") is not True:
        brief["risks"] = [*brief["risks"], dict(VerificationFailureRisk)]
    jsonschema.validate(brief, _brief_schema())
    return brief


def build_synthesize_handler(
    scenarios: Mapping[str, Mapping[str, Any]],
    *,
    analyze_impact: Callable[[str, Mapping[str, Any], Mapping[str, Any]], dict[str, Any]],
    plan_retrieval: Callable[[Mapping[str, Any]], dict[str, Any]],
    verify_impact: Callable[[str, Mapping[str, Any]], dict[str, Any]],
) -> TaskHandler:
    """Synthesize 任务工厂：确定性重推导 verified_impact/verification_result。

    production runtime 在任务间传 input_values={}（S2/S6 语义），handler 按场景
    前缀重推导输入——组装输入与 Verify 任务输出恒等（同一纯函数链）。
    """

    class _SynthesizeHandler(TaskHandler):
        @property
        def primitive_type(self) -> str:
            return "Synthesize"

        @property
        def handler_version(self) -> int:
            return 1

        def execute(self, input: TaskInput) -> TaskOutput:
            prefix = input.task_id.split("/", 1)[0]
            scenario = scenarios.get(prefix)
            if scenario is None:
                raise KeyError(f"change-brief pack: 未注册的场景前缀 {prefix!r}")
            trigger = scenario["trigger"]
            candidates = plan_retrieval(scenario)
            impact = analyze_impact(
                str(trigger["repository"]), dict(trigger["commit_or_pr"]), candidates
            )
            verification = verify_impact(prefix, impact)
            brief = synthesize_brief(impact, verification)
            return TaskOutput(output_values={"brief": brief})

    return _SynthesizeHandler()
