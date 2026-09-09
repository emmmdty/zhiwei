"""S10 change-brief-v1 suite 注册表：冻结行为语料 → registered eval units。

事实源：specs/s10-studio-third-app.md §4/§7、ADR-013 决策 2（suite id 是真实产品
能力，评测先行——语料先于被评测能力细化）、solution-packs/change-brief/evals/
change-brief-v1.yaml（pack 侧 suite 声明）。

- registered units 从冻结语料 evals/change-brief/ 构造：(sample_id, unit_id) 与
  fixture unit_id 对齐（全部 single 单位）；
- 语料是冻结资产：解析期即校验 schema（extra=forbid）、unit_id 与文件名词干对齐、
  数量与 pack 声明的 registered_unit_count_hint 一致，任何漂移在加载期拒绝
  （fail closed），不在这里修语料；
- corpus digest 是 fixture 文件字节的内容寻址，供 EvalRun dataset payload 作密封
  provenance；
- executor 绑定生产路径（pack task graph 经 RunCommandService → AgentRunWorkflow
  执行），判分语义在 executors/change_brief.py，本模块只登记「有哪些单位、多少
  单位、走哪条路径」。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from zhiwei.agents.pack_files import load_pack_dir
from zhiwei.contracts.canonical import digest_bytes
from zhiwei.evals.domain import RegisteredUnit

REPO_ROOT = Path(__file__).resolve().parents[3]
CORPUS_DIR = REPO_ROOT / "evals" / "change-brief"
PACK_DIR = REPO_ROOT / "solution-packs" / "change-brief"

CHANGE_BRIEF_V1 = "change-brief-v1"

# suite 绑定的生产路径与 executor 种类（路径契约的事实源，executor 模块引用之）。
EXECUTOR_KIND = "change-brief-pack"
PRODUCTION_CHANGE_BRIEF_PATH = (
    "RunCommandService->AgentRunWorkflow->"
    "Retrieve->Analyze(impact-analysis skill)->VerifyHandler->Synthesize->EmitArtifact"
)


class CommitOrPr(BaseModel):
    """触发引用：commit SHA 或 PR 号（字符串）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["commit", "pull_request"]
    ref: str = Field(min_length=1)


class TriggerPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    commit_or_pr: CommitOrPr


class FileChange(BaseModel):
    """一个改动文件及其 before/after 符号清单（影响分析的触达面）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    symbols_before: tuple[str, ...] = ()
    symbols_after: tuple[str, ...] = ()


class SnapshotSymbol(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kind: Literal["function", "class", "module", "constant"]
    file_path: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    code_digest: str = Field(min_length=1)
    # callers 语义：列出「谁调用了本符号」——影响闭包沿该边向调用方传播。
    callers: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _line_end_gte_start(self) -> SnapshotSymbol:
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


class SnapshotDependency(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    version_constraint: str = Field(min_length=1)
    consumers: tuple[str, ...] = ()


class SnapshotPr(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    pr_number: int = Field(ge=1)
    touches: tuple[str, ...] = ()


class SnapshotIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1)
    issue_number: int = Field(ge=1)
    mentions: tuple[str, ...] = ()


class SnapshotCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    status: Literal["passed", "failed", "pending"]
    on_refs: tuple[str, ...] = ()


class KnowledgeSnapshot(BaseModel):
    """代码知识快照子集：检索候选集的事实源（symbols 必需，其余可选）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[SnapshotSymbol, ...] = Field(min_length=1)
    dependencies: tuple[SnapshotDependency, ...] = ()
    prs: tuple[SnapshotPr, ...] = ()
    issues: tuple[SnapshotIssue, ...] = ()
    checks: tuple[SnapshotCheck, ...] = ()


class ExpectedBrief(BaseModel):
    """brief 形状断言数据（判分器消费；规则推导的可复算事实源见 fixture 注释）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    affected_symbols: tuple[str, ...] = ()
    affected_dependencies: tuple[str, ...] = ()
    affected_tests: tuple[str, ...] = ()
    failed_tests: tuple[str, ...] = ()
    related_prs: tuple[int, ...] = ()
    related_issues: tuple[int, ...] = ()
    related_checks: tuple[str, ...] = ()
    risks_severities: tuple[str, ...] = ()
    unknowns_empty: bool = False
    unknowns_contain: tuple[str, ...] = ()
    min_code_refs: int = Field(default=0, ge=0)
    min_github_refs: int = Field(default=0, ge=0)
    # 绝不允许出现在 affected_symbols 里的名字（编造守卫；unknown-symbol 场景核心）。
    no_fabricated_symbols: tuple[str, ...] = ()


class ChangeBriefUnit(BaseModel):
    """一个 change-brief-v1 行为单位：触发 + 快照 + expected 断言数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_id: str = Field(min_length=1)
    trigger: TriggerPayload
    files_changed: tuple[FileChange, ...] = Field(min_length=1)
    snapshot: KnowledgeSnapshot
    expected: ExpectedBrief


@dataclass(frozen=True, slots=True)
class ChangeBriefSuiteDefinition:
    """change-brief-v1 的冻结视图：语料、注册单位与生产路径绑定。"""

    name: str
    pack_dir: Path
    corpus_path: Path
    corpus_digest: str
    units: tuple[ChangeBriefUnit, ...]
    registered_units: tuple[RegisteredUnit, ...]
    executor_kind: str
    production_path: str


def _parse_corpus() -> tuple[ChangeBriefUnit, ...]:
    if not CORPUS_DIR.is_dir():
        raise LookupError(f"change-brief suite 语料缺失: {CORPUS_DIR}")
    units: list[ChangeBriefUnit] = []
    seen: set[str] = set()
    for path in sorted(CORPUS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"{path.name}: 语料不是合法 YAML: {exc}") from exc
        unit = ChangeBriefUnit.model_validate(data)
        if unit.unit_id != path.stem:
            raise ValueError(f"{path.name}: unit_id {unit.unit_id!r} 与文件名词干不符")
        if unit.unit_id in seen:
            raise ValueError(f"change-brief 语料 unit 重复: {unit.unit_id}")
        seen.add(unit.unit_id)
        units.append(unit)
    if len(units) != _declared_unit_count_hint():
        raise ValueError(
            f"change-brief 语料 fixture 数 {len(units)} 与 pack 声明的 "
            f"registered_unit_count_hint {_declared_unit_count_hint()} 不符"
        )
    return tuple(units)


def _declared_unit_count_hint() -> int:
    """pack 侧 suite 声明的单位数（corpus_ref 指向本语料；对齐校验防双侧漂移）。"""
    bundle = load_pack_dir(PACK_DIR)
    for declaration in bundle.evals:
        if declaration.suite_id == CHANGE_BRIEF_V1:
            if declaration.corpus_ref != "evals/change-brief/":
                raise ValueError(
                    f"pack 的 corpus_ref {declaration.corpus_ref!r} 与冻结语料不符"
                )
            return declaration.registered_unit_count_hint
    raise LookupError(f"pack 未声明 suite {CHANGE_BRIEF_V1}")


def _corpus_digest(units: tuple[ChangeBriefUnit, ...]) -> str:
    joined = b"".join(
        (CORPUS_DIR / f"{unit.unit_id}.yaml").read_bytes()
        for unit in sorted(units, key=lambda u: u.unit_id)
    )
    return digest_bytes(joined)


@cache
def _load_suite(suite: str) -> ChangeBriefSuiteDefinition:
    if suite != CHANGE_BRIEF_V1:
        raise LookupError(f"未知 change-brief suite: {suite}")
    units = _parse_corpus()
    return ChangeBriefSuiteDefinition(
        name=suite,
        pack_dir=PACK_DIR,
        corpus_path=CORPUS_DIR,
        corpus_digest=_corpus_digest(units),
        units=units,
        registered_units=tuple(
            RegisteredUnit(sample_id=unit.unit_id, unit_id=unit.unit_id)
            for unit in units
        ),
        executor_kind=EXECUTOR_KIND,
        production_path=PRODUCTION_CHANGE_BRIEF_PATH,
    )


def resolve_change_brief_suite(suite: str) -> ChangeBriefSuiteDefinition:
    """按名解析 suite；未知名称 fail closed（LookupError）。"""
    return _load_suite(suite)


def registered_change_brief_units() -> tuple[RegisteredUnit, ...]:
    """suite 的 registered units（与 resolve_change_brief_suite 同源）。"""
    return _load_suite(CHANGE_BRIEF_V1).registered_units


def unit_fixture_payload(unit: ChangeBriefUnit) -> dict[str, Any]:
    """单位 → pack runtime 场景绑定（handler 工厂消费的纯数据形态）。"""
    return unit.model_dump(mode="json")
