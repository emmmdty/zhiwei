"""S5 knowledge suite 注册表契约：冻结 JSONL 语料 → registered eval units。

事实源：specs/s5-knowledge-fabric.md §6/§8、ADR-013 决策 2（suite 注册是能力缺口补齐，
禁止以改 spec 消除；评测先行）。

契约面：
- Gate 命令引用的四个 suite 名必须可解析（specs/s5 §8）；
- registered units 与 JSONL 行数一致，(sample_id, unit_id) 与语料字段 (id,
  independence_unit_id) 对齐；
- 未知 suite 一律 fail closed（LookupError），包括既有 suite 名也不得混入知识注册表；
- doc 与 cross-source 两个 suite 必须绑定生产检索路径
  （Retrieve TaskHandler → Knowledge Planner，specs/s5 §8）；
- acl_freshness_v1 必须覆盖 ACL pre-filter、freshness stale、cross-org 拒绝三类单位；
- corpus digest 是语料文件的内容寻址（sha256:<hex>），作为密封 provenance。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from zhiwei.evals.executors.knowledge import (
    EXECUTOR_KIND,
    PRODUCTION_RETRIEVAL_PATH,
    KnowledgeRetrievalExecutor,
)
from zhiwei.evals.knowledge_suites import (
    KNOWLEDGE_SUITE_NAMES,
    knowledge_suite_units,
    resolve_knowledge_suite,
)
from zhiwei.runtime.handlers.retrieve import RetrieveHandler

_REPO_ROOT = Path(__file__).resolve().parents[3]
_KNOWLEDGE_DIR = _REPO_ROOT / "evals" / "knowledge"

_SUITE_FILES = {
    "knowledge-doc-v1": "doc_table_v1.jsonl",
    "knowledge-code-github-v1": "code_github_v1.jsonl",
    "knowledge-cross-source-v1": "cross_source_v1.jsonl",
    "knowledge-acl-freshness-v1": "acl_freshness_v1.jsonl",
}


def _jsonl_rows(suite: str) -> list[dict[str, object]]:
    path = _KNOWLEDGE_DIR / _SUITE_FILES[suite]
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def test_four_gate_suites_resolve() -> None:
    assert frozenset(_SUITE_FILES) == KNOWLEDGE_SUITE_NAMES
    for name in _SUITE_FILES:
        suite = resolve_knowledge_suite(name)
        assert suite.name == name
        assert suite.executor_kind == EXECUTOR_KIND
        assert suite.production_path == PRODUCTION_RETRIEVAL_PATH
        assert suite.corpus_path.name == _SUITE_FILES[name]


def test_registered_units_match_jsonl_rows() -> None:
    for name in _SUITE_FILES:
        suite = resolve_knowledge_suite(name)
        rows = _jsonl_rows(name)
        assert suite.registered_units == knowledge_suite_units(name)
        assert len(suite.registered_units) == len(rows)
        assert {(u.sample_id, u.unit_id) for u in suite.registered_units} == {
            (str(row["id"]), str(row["independence_unit_id"])) for row in rows
        }


def test_unknown_suite_fails_closed() -> None:
    for name in ("legacy-assets", "runtime-contract-v1", "knowledge-doc-v2", "factqa", ""):
        with pytest.raises(LookupError):
            resolve_knowledge_suite(name)


def test_doc_and_cross_source_bind_production_retrieval_path() -> None:
    for name in ("knowledge-doc-v1", "knowledge-cross-source-v1"):
        executor = KnowledgeRetrievalExecutor(resolve_knowledge_suite(name))
        assert isinstance(executor.handler, RetrieveHandler)


def test_acl_freshness_suite_covers_gate_families() -> None:
    suite = resolve_knowledge_suite("knowledge-acl-freshness-v1")
    query_types = {item.query_type for item in suite.items}
    assert {"acl_pre_filter", "freshness_stale", "cross_org_query"}.issubset(query_types)


def test_corpus_digest_is_content_addressed() -> None:
    for name, filename in _SUITE_FILES.items():
        suite = resolve_knowledge_suite(name)
        content = (_KNOWLEDGE_DIR / filename).read_bytes()
        assert suite.corpus_digest == "sha256:" + hashlib.sha256(content).hexdigest()
