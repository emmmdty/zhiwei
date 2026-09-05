"""S6 factqa-v1 suite 注册表：冻结题集 → Evidence/SQL regression eval units。

事实源：specs/s6-evidence-ask.md §6（「迁移旧 factqa-v1 为 Evidence/SQL regression，
不改变冻结资产」）、ADR-013 决策 2。

- registered units 从 `evals/questions/` 四个冻结 JSONL **只读**构造：
  (sample_id, unit_id) = (id, independence_unit_id)，F5 chain 的三个 turn 共享
  unit（与冻结的统计单位契约一致，evals/scripts/validate_corpus.py 是同一口径）。
- 题集是冻结资产：解析期即校验 suite 必需字段、id 唯一性与单位对齐，任何漂移在
  加载期拒绝（fail closed），不在这里修题集。
- corpus digest 是四个 JSONL 文件字节的内容寻址，供 EvalRun dataset payload 作
  密封 provenance。
- executor 绑定生产 Evidence 路径（QueryReplay 重放 → canonical value →
  verify_bundle 复算 claim），判分语义在 executors/factqa.py。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.evals.domain import RegisteredUnit

REPO_ROOT = Path(__file__).resolve().parents[3]
_QUESTIONS_DIR = REPO_ROOT / "evals" / "questions"

FACTQA_V1 = "factqa-v1"

# suite 绑定的生产路径与 executor 种类（路径契约的事实源，executor 模块引用之）。
EXECUTOR_KIND = "evidence-sql-replay"
PRODUCTION_EVIDENCE_PATH = "FrozenSnapshotReplay->QueryReplayRef->EvidenceVerifier"

_QUESTION_FILES: tuple[str, ...] = (
    "shuihu.jsonl",
    "xiyouji.jsonl",
    "manual/shuihu.jsonl",
    "manual/xiyouji.jsonl",
)

_REQUIRED_FIELDS = (
    "id",
    "book",
    "type",
    "template_id",
    "independence_unit_id",
    "unit_kind",
    "ground_truth",
    "answer_kind",
)
_SQL_TYPES = frozenset({"F1", "F3", "F4"})


class FactqaQuestion(BaseModel):
    """一条冻结 factqa 题目的显式只读视图（最小必需字段，extra 容忍）。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str = Field(min_length=1)
    book: str = Field(min_length=1)
    type: str = Field(min_length=1)
    template_id: str = Field(min_length=1)
    independence_unit_id: str = Field(min_length=1)
    unit_kind: str
    ground_truth: Any
    answer_kind: str = Field(min_length=1)
    source_sql: str | None = None
    source_params: tuple[Any, ...] = ()
    scoring_mode: str = "exact"
    scoring_tolerance: float = 0.0
    conflict_id: str | None = None

    @property
    def has_ground_truth_sql(self) -> bool:
        return self.source_sql is not None and self.type in _SQL_TYPES


@dataclass(frozen=True, slots=True)
class FactqaSuiteDefinition:
    """factqa-v1 的冻结视图：题集、注册单位与生产 Evidence 路径绑定。"""

    name: str
    corpus_paths: tuple[Path, ...]
    corpus_digest: str
    questions: tuple[FactqaQuestion, ...]
    registered_units: tuple[RegisteredUnit, ...]
    executor_kind: str
    production_path: str


def _parse_question(line_no: int, line: str, path: Path) -> FactqaQuestion:
    raw: dict[str, Any]
    try:
        raw = json.loads(line)
    except ValueError as exc:
        raise ValueError(f"{path.name}:{line_no}: 题集行不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}:{line_no}: 题集行必须是 JSON object")
    missing = [field for field in _REQUIRED_FIELDS if field not in raw]
    if missing:
        raise ValueError(f"{path.name}:{line_no}: 题集行缺少必需字段 {missing}")
    if raw["unit_kind"] == "single" and raw["independence_unit_id"] != raw["id"]:
        raise ValueError(
            f"{path.name}:{line_no}: single 单位的 independence_unit_id 必须等于 id"
        )
    if raw["unit_kind"] == "chain" and raw["independence_unit_id"] != raw.get("chain_id"):
        raise ValueError(
            f"{path.name}:{line_no}: chain 单位的 independence_unit_id 必须等于 chain_id"
        )
    scoring = raw.get("scoring") or {}
    return FactqaQuestion(
        id=str(raw["id"]),
        book=str(raw["book"]),
        type=str(raw["type"]),
        template_id=str(raw["template_id"]),
        independence_unit_id=str(raw["independence_unit_id"]),
        unit_kind=str(raw["unit_kind"]),
        ground_truth=raw["ground_truth"],
        answer_kind=str(raw["answer_kind"]),
        source_sql=raw.get("source_sql"),
        source_params=tuple(raw.get("source_params") or ()),
        scoring_mode=str(scoring.get("mode", "exact")),
        scoring_tolerance=float(scoring.get("tolerance", 0.0)),
        conflict_id=raw.get("conflict_id"),
    )


@cache
def _load_suite(suite: str) -> FactqaSuiteDefinition:
    if suite != FACTQA_V1:
        raise LookupError(f"未知 factqa suite: {suite}")
    paths = tuple(_QUESTIONS_DIR / relative for relative in _QUESTION_FILES)
    questions: list[FactqaQuestion] = []
    seen_ids: set[str] = set()
    for path in paths:
        if not path.is_file():
            raise LookupError(f"factqa 冻结题集缺失: {path}")
        for line_no, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            question = _parse_question(line_no, line, path)
            if question.id in seen_ids:
                raise ValueError(f"{path.name}: 题目 id 重复: {question.id}")
            seen_ids.add(question.id)
            questions.append(question)
    if not questions:
        raise ValueError("factqa 题集为空")
    corpus_bytes = b"".join(path.read_bytes() for path in paths)
    return FactqaSuiteDefinition(
        name=suite,
        corpus_paths=paths,
        corpus_digest=digest_bytes(corpus_bytes),
        questions=tuple(questions),
        registered_units=tuple(
            RegisteredUnit(sample_id=q.id, unit_id=q.independence_unit_id)
            for q in questions
        ),
        executor_kind=EXECUTOR_KIND,
        production_path=PRODUCTION_EVIDENCE_PATH,
    )


def resolve_factqa_suite(suite: str = FACTQA_V1) -> FactqaSuiteDefinition:
    """按名解析 factqa suite；未知名称 fail closed（LookupError）。"""
    return _load_suite(suite)


def factqa_suite_units(suite: str = FACTQA_V1) -> tuple[RegisteredUnit, ...]:
    """一个 suite 的 registered units（与 resolve_factqa_suite 同源）。"""
    return _load_suite(suite).registered_units
