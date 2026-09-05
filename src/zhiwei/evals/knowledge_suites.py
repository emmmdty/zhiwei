"""S5 knowledge suite 注册表：冻结 JSONL 语料 → registered eval units。

事实源：specs/s5-knowledge-fabric.md §6/§8、ADR-013 决策 2（Gate 命令引用的 suite id
是真实产品能力，评测先行——suite 与语料先于被评测能力细化）。

- 每个 suite 的 registered units 从 `evals/knowledge/` 对应 JSONL 的查询构造：
  (sample_id, unit_id) 与语料字段 (id, independence_unit_id) 对齐（全部 unit_kind=single，
  与 legacy validator 的单轮单位契约一致）。
- 语料是冻结资产：解析期即校验 suite 字段、id 唯一性与单位对齐，任何漂移在加载期拒绝
  （fail closed），不在这里修语料。
- corpus digest 是 JSONL 文件字节的内容寻址，供 EvalRun dataset payload 作密封 provenance。
- executor 绑定生产检索路径（Retrieve TaskHandler → Knowledge Planner），判分语义在
  executors/knowledge.py，本模块只登记「有哪些 suite、多少单位、走哪条路径」。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.evals.domain import RegisteredUnit

REPO_ROOT = Path(__file__).resolve().parents[3]
_KNOWLEDGE_DIR = REPO_ROOT / "evals" / "knowledge"

KNOWLEDGE_DOC_V1 = "knowledge-doc-v1"
KNOWLEDGE_CODE_GITHUB_V1 = "knowledge-code-github-v1"
KNOWLEDGE_CROSS_SOURCE_V1 = "knowledge-cross-source-v1"
KNOWLEDGE_ACL_FRESHNESS_V1 = "knowledge-acl-freshness-v1"

KNOWLEDGE_SUITE_NAMES: frozenset[str] = frozenset(
    {
        KNOWLEDGE_DOC_V1,
        KNOWLEDGE_CODE_GITHUB_V1,
        KNOWLEDGE_CROSS_SOURCE_V1,
        KNOWLEDGE_ACL_FRESHNESS_V1,
    }
)

_SUITE_CORPUS_FILES: dict[str, str] = {
    KNOWLEDGE_DOC_V1: "doc_table_v1.jsonl",
    KNOWLEDGE_CODE_GITHUB_V1: "code_github_v1.jsonl",
    KNOWLEDGE_CROSS_SOURCE_V1: "cross_source_v1.jsonl",
    KNOWLEDGE_ACL_FRESHNESS_V1: "acl_freshness_v1.jsonl",
}

# knowledge suite 绑定的生产检索路径（specs/s5 §8）：Retrieve TaskHandler →
# Knowledge Planner。定义在注册表（路径契约的事实源），executor 模块引用之。
EXECUTOR_KIND = "knowledge-retrieval"
PRODUCTION_RETRIEVAL_PATH = "RetrieveTaskHandler->KnowledgePlanner"


class KnowledgeLocator(BaseModel):
    """语料声明的一个 source-native locator 目标。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connector: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    span: tuple[int, int] | None = None


class KnowledgeScoring(BaseModel):
    """语料声明的判分配置；语义解释在 executor/score 层。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: str
    tolerance: float = 0.0


class KnowledgeItem(BaseModel):
    """一条冻结的 knowledge eval 查询（JSONL 行的显式模式）。

    通用字段全 suite 一致；ACL/freshness/cross-org 场景字段（acl_*、freshness_*、
    query_org/target_org）按 query_type 可选出现。extra=forbid：语料引入未登记字段
    即加载失败，防止「schema 之外的第二套语义」静默混入。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    suite: str
    type: str
    template_id: str = Field(min_length=1)
    independence_unit_id: str = Field(min_length=1)
    unit_kind: str
    query: str = Field(min_length=1)
    ground_truth: str | list[str]
    answer_kind: str
    scoring: KnowledgeScoring
    trace_required: bool
    expected_locators: tuple[KnowledgeLocator, ...] = ()
    field_class: str
    targets_perturbed_field: bool
    query_type: str
    metamorphic_variant: str | None = None
    metamorphic_base_id: str | None = None
    blind_holdout: bool = False
    acl_principal: str | None = None
    acl_clearance: str | None = None
    target_classification: str | None = None
    revoked_at_query_time: bool | None = None
    freshness_age_days: int | None = None
    aging_threshold_days: int | None = None
    query_org: str | None = None
    target_org: str | None = None

    @field_validator("expected_locators", mode="before")
    @classmethod
    def _coerce_locators(cls, value: Any) -> Any:
        if value is None:
            return ()
        return value


@dataclass(frozen=True, slots=True)
class KnowledgeSuiteDefinition:
    """一个 knowledge suite 的冻结视图：语料、注册单位与生产路径绑定。"""

    name: str
    corpus_path: Path
    corpus_digest: str
    items: tuple[KnowledgeItem, ...]
    registered_units: tuple[RegisteredUnit, ...]
    executor_kind: str
    production_path: str


def _parse_corpus(suite: str, path: Path) -> tuple[KnowledgeItem, ...]:
    items: list[KnowledgeItem] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            items.append(KnowledgeItem.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"{path.name}:{line_no}: 语料行不符合冻结 schema: {exc}") from exc
    if not items:
        raise ValueError(f"{path.name}: 语料为空")
    seen_ids: set[str] = set()
    for item in items:
        if item.suite != suite:
            raise ValueError(
                f"{path.name}:{item.id}: suite 字段 {item.suite!r} 与注册名 {suite!r} 不符"
            )
        if item.id in seen_ids:
            raise ValueError(f"{path.name}: id 重复: {item.id}")
        seen_ids.add(item.id)
        if item.unit_kind == "single" and item.independence_unit_id != item.id:
            raise ValueError(
                f"{path.name}:{item.id}: single 单位的 independence_unit_id 必须等于 id"
            )
    return tuple(items)


@cache
def _load_suite(suite: str) -> KnowledgeSuiteDefinition:
    if suite not in KNOWLEDGE_SUITE_NAMES:
        raise LookupError(f"未知 knowledge suite: {suite}")
    path = _KNOWLEDGE_DIR / _SUITE_CORPUS_FILES[suite]
    if not path.is_file():
        raise LookupError(f"knowledge suite 语料缺失: {path}")
    items = _parse_corpus(suite, path)
    return KnowledgeSuiteDefinition(
        name=suite,
        corpus_path=path,
        corpus_digest=digest_bytes(path.read_bytes()),
        items=items,
        registered_units=tuple(
            RegisteredUnit(sample_id=item.id, unit_id=item.independence_unit_id)
            for item in items
        ),
        executor_kind="knowledge-retrieval",
        production_path="RetrieveTaskHandler->KnowledgePlanner",
    )


def resolve_knowledge_suite(suite: str) -> KnowledgeSuiteDefinition:
    """按名解析 suite；未知名称 fail closed（LookupError）。"""
    return _load_suite(suite)


def knowledge_suite_units(suite: str) -> tuple[RegisteredUnit, ...]:
    """一个 suite 的 registered units（与 resolve_knowledge_suite 同源）。"""
    return _load_suite(suite).registered_units
