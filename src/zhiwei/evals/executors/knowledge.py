"""S5 knowledge suite executor：经生产检索路径执行并按冻结语料判分。

事实源：specs/s5-knowledge-fabric.md §5/§6/§8、ADR-006、ADR-013 决策 2。

执行路径：语料场景 → 生产 Retrieve TaskHandler（runtime/handlers/retrieve.py，即 S5 §7
的 Retrieve handler）→ Knowledge Planner → typed candidates。不设评测专用旁路：
handler/planner/ACL/freshness 全部是生产实现，本模块只做语料物化（corpus → SourceVersion
候选池）与确定性判分。

确定性约束：
- SourceVersion id 用 UUID5（固定 namespace + locator）派生；content digest 由 locator
  的 canonical JSON 复算；
- freshness clock 钉在语料冻结日（pack frozen_at=2026-09-04），aging 阈值取 pack 冻结
  策略（files=7d），使 doc-456（30d）/doc-789（2d）的 freshness 恒定；
- 池内排序、标签推导全部确定性，两次执行产出逐字节一致的 result payload。

Claim boundary（specs/s5 §9）：offline 只声明 retrieval、locator、ACL/freshness 结果。
内容类 suite（doc/code/cross-source）按 locator 命中 + score breakdown 判分
（scoring_basis=locator）；ACL/freshness suite 按系统行为推导标签后与 ground truth 比较
（scoring_basis=behavior-label）。不声称答案合成质量——那需要 live 模型。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.evals.domain import RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.knowledge_suites import (
    EXECUTOR_KIND,
    KNOWLEDGE_ACL_FRESHNESS_V1,
    PRODUCTION_RETRIEVAL_PATH,
    REPO_ROOT,
    KnowledgeItem,
    KnowledgeSuiteDefinition,
)
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceVersion,
    SourceVersionState,
)
from zhiwei.knowledge.freshness import FreshnessPolicy
from zhiwei.knowledge.planner import KnowledgePlanner
from zhiwei.knowledge.query import KnowledgeQuery, QuerySource, SortField
from zhiwei.runtime.handlers.base import TaskInput
from zhiwei.runtime.handlers.retrieve import RetrieveHandler

_PACK_PATH = REPO_ROOT / "solution-packs" / "reference-knowledge" / "pack.yaml"
_CORPUS_NAMESPACE = uuid5(NAMESPACE_URL, "zhiwei:evals:knowledge-corpus:v1")
_WORKSPACE_GROUP = "workspace-members"
# 语料冻结日（pack frozen_at）。freshness 评估必须钉在固定时刻，否则 age 随墙钟增长、
# sealed artifact 不可复现。
_PINNED_CLOCK = datetime(2026, 9, 4, tzinfo=UTC)
_FRESHNESS_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

# 冻结语料的行为标签词表：observed 系统行为 → 标签。标签值与 evals/knowledge 语料的
# ground truth 一一对应；推导方向是「行为事实 → 标签」——生产行为改变会推导出不同
# 标签并判 0 分，而不是反查语料回填答案。
_LABEL_CLASSIFICATION_CEILING = "拒绝：classification ceiling exceeded"
_LABEL_ACL_REVOKED = "拒绝：ACL revoked between index and query time"
_LABEL_CROSS_ORG = "拒绝：cross-org query requires explicit workspace grant"
_LABEL_UNKNOWN_ACL = "拒绝：unknown ACL state fail closed"
_LABEL_REVOKED_EVIDENCE = "拒绝：revoked version must not appear as evidence"
_LABEL_FRESHNESS_AGED = "freshness_score < 1.0 (aged)"
_LABEL_FRESHNESS_FRESH = "1.0"

_CONNECTOR_SOURCES: dict[str, QuerySource] = {
    "files": QuerySource.DOC,
    "github": QuerySource.GITHUB,
    "db": QuerySource.DB,
}

_EXPECTED_STRATEGIES: dict[str, str] = {
    "knowledge-doc-v1": "doc_bm25_dense_rrf_rerank",
    "knowledge-code-github-v1": "github_pr_issue_search",
    "knowledge-cross-source-v1": "cross_source_fusion",
    "knowledge-acl-freshness-v1": "doc_bm25_dense_rrf_rerank",
}


@dataclass(frozen=True, slots=True)
class PoolEntry:
    """候选池条目：物化的 SourceVersion 及其 index-time 语义标注。"""

    version: SourceVersion
    acl_state: str  # granted | unknown
    classification_declared: str


@dataclass(frozen=True, slots=True)
class _PackMetadata:
    """reference-knowledge solution pack 的冻结元数据（分类档与 freshness fixture）。"""

    classifications: dict[str, str]
    freshness_observed_at: dict[str, datetime]


def _load_pack() -> _PackMetadata:
    if not _PACK_PATH.is_file():
        raise RuntimeError(f"reference-knowledge solution pack 缺失: {_PACK_PATH}")
    pack = yaml.safe_load(_PACK_PATH.read_text(encoding="utf-8"))
    classifications = {
        doc["uri_pattern"]: doc.get("classification", "PUBLIC")
        for doc in pack["sources"]["documents"]
    }
    freshness: dict[str, datetime] = {}
    for fixture in pack["freshness"]["fixtures"]:
        # fixture 以 doc id 声明，locator 约定为 docs/specs/{doc}.md（与语料一致）
        uri = f"docs/specs/{fixture['doc']}.md"
        freshness[uri] = datetime.fromisoformat(
            str(fixture["observed_at"]).replace("Z", "+00:00")
        )
    return _PackMetadata(classifications=classifications, freshness_observed_at=freshness)


def _deterministic_uuid(*parts: str) -> UUID:
    return uuid5(_CORPUS_NAMESPACE, ":".join(parts))


def _content_digest(connector: str, uri: str) -> str:
    return digest_bytes(canonical_json({"connector": connector, "uri": uri}))


def _classification_for_uri(pack: _PackMetadata, uri: str) -> str:
    base = uri.split("#", 1)[0].split("@", 1)[0]
    return pack.classifications.get(base, "PUBLIC")


def _observed_at_for_uri(pack: _PackMetadata, uri: str) -> datetime:
    return pack.freshness_observed_at.get(uri, _FRESHNESS_EPOCH)


def _materialize(
    connector: str,
    uri: str,
    *,
    classification: str,
    acl: ACLSnapshot,
    state: SourceVersionState,
    observed_at: datetime,
    acl_state: str,
) -> PoolEntry:
    version = SourceVersion(
        id=_deterministic_uuid("version", connector, uri),
        source_object_id=_deterministic_uuid("object", connector, uri),
        version_seq=1,
        locator=Locator(connector=connector, uri=uri),
        content_digest=_content_digest(connector, uri),
        observed_at=observed_at,
        valid_at=observed_at,
        acl=acl,
        classification=Classification(classification),
        state=state,
    )
    return PoolEntry(
        version=version, acl_state=acl_state, classification_declared=classification
    )


def _entry_payload(entry: PoolEntry) -> dict[str, Any]:
    """SourceVersion 的 handler 传输形态（RetrieveHandler._parse_versions 契约）。"""
    version = entry.version
    return {
        "id": str(version.id),
        "source_object_id": str(version.source_object_id),
        "version_seq": version.version_seq,
        "locator": {
            "connector": version.locator.connector,
            "uri": version.locator.uri,
        },
        "content_digest": version.content_digest,
        "observed_at": version.observed_at.isoformat(),
        "valid_at": version.valid_at.isoformat(),
        "acl": {
            "allowed_principals": list(version.acl.allowed_principals),
            "denied_principals": list(version.acl.denied_principals),
            "allowed_groups": list(version.acl.allowed_groups),
        },
        "classification": version.classification.value,
        "state": version.state.value,
    }


def _granted_to_principal(principal: str) -> ACLSnapshot:
    return ACLSnapshot(allowed_principals=(principal,))


def _workspace_granted() -> ACLSnapshot:
    return ACLSnapshot(allowed_groups=(_WORKSPACE_GROUP,))


class KnowledgeRetrievalExecutor:
    """知识 suite executor：一个注册单位 = 语料场景 → 生产检索路径 → 确定性判分。"""

    def __init__(
        self,
        suite: KnowledgeSuiteDefinition,
        handler: RetrieveHandler | None = None,
        pack: _PackMetadata | None = None,
    ) -> None:
        self._suite = suite
        self._items_by_id: dict[str, KnowledgeItem] = {
            item.id: item for item in suite.items
        }
        self._pack = pack or _load_pack()
        if handler is None:
            planner = KnowledgePlanner(
                freshness_policies={
                    "files": FreshnessPolicy(
                        connector="files", aging_threshold=timedelta(days=7)
                    )
                },
                clock=lambda: _PINNED_CLOCK,
            )
            handler = RetrieveHandler(planner)
        self._handler = handler

    @property
    def handler(self) -> RetrieveHandler:
        return self._handler

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome:
        item = self._items_by_id.get(unit.sample_id)
        if item is None or unit.unit_id != item.independence_unit_id:
            return SampleOutcome(
                unit=unit,
                status=SampleStatus.FAILED,
                result={
                    "suite": self._suite.name,
                    "error": f"unit 未注册于 suite 语料: {unit.sample_id}/{unit.unit_id}",
                },
            )
        try:
            result = self._execute_item(item)
        except Exception as exc:
            return SampleOutcome(
                unit=unit,
                status=SampleStatus.FAILED,
                result={
                    "suite": self._suite.name,
                    "query_id": item.id,
                    "executor": EXECUTOR_KIND,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        status = (
            SampleStatus.COMPLETED if result["verdict"] == "pass" else SampleStatus.FAILED
        )
        return SampleOutcome(unit=unit, status=status, result=result)

    # ------------------------------------------------------------------ 场景物化

    def _unit_pool(self, item: KnowledgeItem) -> list[PoolEntry]:
        """物化本单位的候选池。

        内容类 suite：整个 suite 语料的 locator 物化为共享索引池（corpus-wide index
        snapshot，接近生产「索引持有全语料」的语义）；revoke 变体在该单位视图内把
        base locator 置为 REVOKED 且授予 principal——让它抵达 recheck 并携带失权标记
        （ADR-006 失权呈现），而不是被 pre-filter 静默丢弃。

        ACL/freshness suite：单位即行为场景，池只含场景目标文档，其分类/ACL/state
        由语料 target 字段驱动。
        """
        if self._suite.name == KNOWLEDGE_ACL_FRESHNESS_V1:
            return [self._materialize_acl_target(item)]
        return self._content_pool(item)

    def _content_pool(self, item: KnowledgeItem) -> list[PoolEntry]:
        revoke_keys: set[tuple[str, str]] = set()
        if item.metamorphic_variant == "revoke" and item.metamorphic_base_id:
            revoke_keys = {
                (loc.connector, loc.uri)
                for loc in self._base_item(item).expected_locators
            }
        principal = self._principal_str(item)
        entries: list[PoolEntry] = []
        seen: set[tuple[str, str]] = set()
        for source in self._suite.items:
            for loc in source.expected_locators:
                key = (loc.connector, loc.uri)
                if key in seen:
                    continue
                seen.add(key)
                if key in revoke_keys:
                    # revoke 变体：置为 REVOKED 且索引期授予 principal，让版本抵达
                    # hydration recheck 并携带失权标记（ADR-006 失权呈现），
                    # 而不是被 pre-filter 静默丢弃。
                    entries.append(
                        _materialize(
                            key[0],
                            key[1],
                            classification=self._pack_classification(key[1]),
                            acl=_granted_to_principal(principal),
                            state=SourceVersionState.REVOKED,
                            observed_at=_observed_at_for_uri(self._pack, key[1]),
                            acl_state="granted",
                        )
                    )
                    continue
                entries.append(
                    _materialize(
                        key[0],
                        key[1],
                        classification=self._pack_classification(key[1]),
                        acl=_workspace_granted(),
                        state=SourceVersionState.ACTIVE,
                        observed_at=_observed_at_for_uri(self._pack, key[1]),
                        acl_state="granted",
                    )
                )
        return entries

    def _materialize_acl_target(self, item: KnowledgeItem) -> PoolEntry:
        locators = item.expected_locators or self._base_item(item).expected_locators
        if not locators:
            raise ValueError(f"{item.id}: 行为场景缺少目标 locator")
        target = locators[0]
        principal = self._principal_str(item)
        classification = item.target_classification or self._pack_classification(
            target.uri
        )
        acl = _workspace_granted()
        acl_state = "granted"
        state = SourceVersionState.ACTIVE
        if item.query_type == "acl_pre_filter":
            # 索引期 ACL 放行 principal，让被拒维度严格落在 classification ceiling 上
            acl = _granted_to_principal(principal)
        elif item.query_type == "acl_hydration_recheck":
            # 索引期放行（快照冻结），查询期拒绝由 ACLContext 表达
            acl = _granted_to_principal(principal)
        elif item.query_type == "cross_org_query":
            acl = ACLSnapshot(allowed_groups=(item.target_org or "org-b",))
        elif item.query_type == "blind_unknown_acl":
            acl = ACLSnapshot()
            acl_state = "unknown"
        elif item.query_type == "blind_revoked":
            acl = _granted_to_principal(principal)
            state = SourceVersionState.REVOKED
        return _materialize(
            target.connector,
            target.uri,
            classification=classification,
            acl=acl,
            state=state,
            observed_at=_observed_at_for_uri(self._pack, target.uri),
            acl_state=acl_state,
        )

    def _base_item(self, item: KnowledgeItem) -> KnowledgeItem:
        if not item.metamorphic_base_id:
            raise ValueError(f"{item.id}: 缺少 metamorphic_base_id")
        base = self._items_by_id.get(item.metamorphic_base_id)
        if base is None:
            raise ValueError(f"{item.id}: metamorphic_base_id 不在语料内")
        return base

    def _pack_classification(self, uri: str) -> str:
        return _classification_for_uri(self._pack, uri)

    def _principal_str(self, item: KnowledgeItem) -> str:
        return str(
            _deterministic_uuid("principal", item.acl_principal or "eval-principal@org-a")
        )

    # ------------------------------------------------------------------ 查询构造

    def _build_query(
        self, item: KnowledgeItem, pool: list[PoolEntry]
    ) -> tuple[KnowledgeQuery, dict[str, Any]]:
        locators = item.expected_locators
        if not locators and item.metamorphic_base_id:
            locators = self._base_item(item).expected_locators
        connectors = {loc.connector for loc in locators}
        if not connectors:
            connectors = {entry.version.locator.connector for entry in pool}
        sources: list[QuerySource] = []
        for connector in sorted(connectors):
            source = _CONNECTOR_SOURCES.get(connector)
            if source is None:
                raise ValueError(f"{item.id}: 未知 connector {connector!r}")
            if source not in sources:
                sources.append(source)

        org = item.query_org or "org-a"
        organization_id = _deterministic_uuid("org", org)
        workspace_id = _deterministic_uuid("workspace", org)
        principal_id = UUID(self._principal_str(item))
        ceiling = item.acl_clearance
        if ceiling not in {member.value for member in Classification}:
            # clearance 未知（fail closed 场景）时取最保守档
            ceiling = Classification.PUBLIC.value
        if self._suite.name != KNOWLEDGE_ACL_FRESHNESS_V1:
            # 内容类 suite 的评测 principal 持全量 clearance；分类门禁由 ACL suite 覆盖
            ceiling = Classification.RESTRICTED.value

        allowed_groups = (
            frozenset({item.query_org}) if item.query_org else frozenset({_WORKSPACE_GROUP})
        )
        denied = (
            frozenset({str(principal_id)})
            if item.revoked_at_query_time
            else frozenset()
        )
        acl_payload: dict[str, Any] = {
            "principal_id": str(principal_id),
            "organization_id": str(organization_id),
            "workspace_id": str(workspace_id),
            "allowed_principals": [],
            "allowed_groups": sorted(allowed_groups),
            "denied_principals": sorted(denied),
            "classification_ceiling": ceiling,
        }
        query = KnowledgeQuery(
            query_id=item.id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            text=item.query,
            sources=tuple(sources),
            classification_ceiling=ceiling,
            top_k=max(len(pool), 1),
            sort_by=SortField.SCORE,
        )
        return query, acl_payload

    # ------------------------------------------------------------------ 执行与判分

    def _execute_item(self, item: KnowledgeItem) -> dict[str, Any]:
        pool = self._unit_pool(item)
        query, acl_payload = self._build_query(item, pool)
        task_input = TaskInput(
            task_id=f"eval:{item.id}",
            attempt_id=_deterministic_uuid("attempt", item.id),
            input_values={
                "query": query.model_dump(mode="json"),
                "acl": acl_payload,
                "candidates": [_entry_payload(entry) for entry in pool],
            },
        )
        output = self._handler.execute(task_input)
        values = output.output_values
        checks: list[str] = []
        failures: list[str] = []
        observed_label: str | None = None
        observed_numeric: float | None = None
        surfaced: dict[tuple[str, str], dict[str, Any]] = {
            (c["connector"], c["uri"]): c for c in values.get("candidates", [])
        }
        if values.get("status") != "completed":
            failures.append(
                f"handler status {values.get('status')!r} != 'completed': "
                f"{values.get('error')}"
            )
        else:
            self._check_breakdowns(surfaced, checks, failures)
            self._check_plan(values, checks, failures)
            pool_by_key = {
                (entry.version.locator.connector, entry.version.locator.uri): entry
                for entry in pool
            }
            if self._suite.name == KNOWLEDGE_ACL_FRESHNESS_V1:
                observed_label, observed_numeric = self._score_behavior(
                    item, pool_by_key, surfaced, checks, failures
                )
            else:
                observed_label, observed_numeric = self._score_locators(
                    item, surfaced, checks, failures
                )
            if failures:
                pass
            elif self._suite.name == KNOWLEDGE_ACL_FRESHNESS_V1 and (
                observed_label is None
            ):
                failures.append("未推导出可比较的行为标签")
            elif observed_label is not None and not _answer_matches(
                item, observed_label, observed_numeric
            ):
                failures.append(
                    f"observed {observed_label!r} 与 ground truth "
                    f"{item.ground_truth!r} 不符"
                )
            elif observed_label is not None:
                checks.append("ground_truth_match")

        return {
            "suite": self._suite.name,
            "query_id": item.id,
            "query_type": item.query_type,
            "template_id": item.template_id,
            "blind_holdout": item.blind_holdout,
            "metamorphic_variant": item.metamorphic_variant,
            "executor": EXECUTOR_KIND,
            "production_path": PRODUCTION_RETRIEVAL_PATH,
            "scoring_basis": "behavior-label"
            if self._suite.name == KNOWLEDGE_ACL_FRESHNESS_V1
            else "locator",
            "handler_status": values.get("status"),
            "plan_search_strategy": (values.get("plan") or {}).get("search_strategy"),
            "surfaced_locators": [
                {"connector": key[0], "uri": key[1]} for key in sorted(surfaced)
            ],
            "expected_locators": [
                {"connector": loc.connector, "uri": loc.uri}
                for loc in item.expected_locators
            ],
            "score_breakdowns": [
                surfaced[key].get("score") or {} for key in sorted(surfaced)
            ],
            "observed_label": observed_label,
            "expected_answer": item.ground_truth,
            "score": 1.0 if not failures else 0.0,
            "verdict": "pass" if not failures else "fail",
            "checks": checks,
            "failures": failures,
        }

    def _check_breakdowns(
        self,
        surfaced: dict[tuple[str, str], dict[str, Any]],
        checks: list[str],
        failures: list[str],
    ) -> None:
        for key in sorted(surfaced):
            breakdown = surfaced[key].get("score") or {}
            problems = _breakdown_problems(breakdown)
            if problems:
                failures.append(f"{key[1]}: score breakdown 无效: {'; '.join(problems)}")
            else:
                checks.append(f"breakdown_valid:{key[1]}")

    def _check_plan(
        self, values: dict[str, Any], checks: list[str], failures: list[str]
    ) -> None:
        strategy = (values.get("plan") or {}).get("search_strategy")
        expected = _EXPECTED_STRATEGIES[self._suite.name]
        if strategy != expected:
            failures.append(f"plan.search_strategy {strategy!r} != {expected!r}")
        else:
            checks.append(f"plan_strategy:{expected}")

    def _score_behavior(
        self,
        item: KnowledgeItem,
        pool: dict[tuple[str, str], PoolEntry],
        surfaced: dict[tuple[str, str], dict[str, Any]],
        checks: list[str],
        failures: list[str],
    ) -> tuple[str | None, float | None]:
        locators = item.expected_locators or self._base_item(item).expected_locators
        target_key = (locators[0].connector, locators[0].uri)
        candidate = surfaced.get(target_key)
        valid = {key for key, c in surfaced.items() if _is_valid_evidence(c)}
        observed_label: str | None = None
        observed_numeric: float | None = None

        if item.query_type == "acl_pre_filter":
            if candidate is None:
                failures.append("目标未出现在候选中，无法观察 classification ceiling 行为")
            else:
                if candidate["score"]["acl_passes_recheck"] is not True:
                    failures.append("目标被 ACL 拒绝，但本单位的被拒维度应为 classification")
                if candidate["score"]["classification_passes"] is not False:
                    failures.append("超限文档未被 classification 门禁标记")
                else:
                    observed_label = _LABEL_CLASSIFICATION_CEILING
                    checks.append("classification_ceiling_enforced")
                if candidate.get("classification") != item.target_classification:
                    failures.append(
                        f"候选分类 {candidate.get('classification')!r} 与语料声明 "
                        f"{item.target_classification!r} 不符"
                    )
        elif item.query_type == "acl_hydration_recheck":
            entry = pool.get(target_key)
            if entry is None or not entry.version.acl.allowed_principals:
                failures.append("候选池缺少索引期放行记录，行为观察不成立")
            elif target_key in valid:
                failures.append("查询期已 revoke 的文档仍作为有效证据返回")
            else:
                observed_label = _LABEL_ACL_REVOKED
                checks.append("acl_revocation_enforced_at_query_time")
        elif item.query_type in {"freshness_stale", "freshness_fresh"}:
            if candidate is None:
                failures.append("目标未出现在候选中，无法观察 freshness 行为")
            else:
                observed_numeric = candidate["score"]["freshness_score"]
                entry = pool.get(target_key)
                observed_age = (
                    (_PINNED_CLOCK - entry.version.observed_at).days if entry else None
                )
                if item.freshness_age_days is not None and observed_age != (
                    item.freshness_age_days
                ):
                    failures.append(
                        f"fixture 实际 age {observed_age!r}d 与语料声明 "
                        f"{item.freshness_age_days!r}d 不符"
                    )
                if observed_numeric is not None:
                    if item.query_type == "freshness_stale":
                        if observed_numeric < 1.0:
                            observed_label = _LABEL_FRESHNESS_AGED
                            checks.append("freshness_aged")
                        else:
                            failures.append(
                                f"aged 文档 freshness_score {observed_numeric} 未低于 1.0"
                            )
                    elif observed_numeric == 1.0:
                        observed_label = _LABEL_FRESHNESS_FRESH
                        checks.append("freshness_fresh")
                    else:
                        failures.append(
                            f"fresh 文档 freshness_score {observed_numeric} != 1.0"
                        )
                else:
                    failures.append("freshness 分数缺失，无法观察 freshness 行为")
        elif item.query_type == "cross_org_query":
            if target_key in valid:
                failures.append("跨组织文档被当作有效证据返回")
            elif item.query_org == item.target_org:
                failures.append("语料声明的跨组织场景 org 相同，行为观察不成立")
            else:
                observed_label = _LABEL_CROSS_ORG
                checks.append("cross_org_denied")
        elif item.query_type == "blind_unknown_acl":
            entry = pool.get(target_key)
            if entry is None or entry.acl_state != "unknown":
                failures.append("候选池条目 ACL 状态应为 unknown")
            elif target_key in valid:
                failures.append("unknown ACL 文档被当作有效证据返回")
            else:
                observed_label = _LABEL_UNKNOWN_ACL
                checks.append("unknown_acl_fail_closed")
        elif item.query_type == "blind_revoked":
            if target_key in valid:
                failures.append("revoked 版本被当作有效证据返回")
            else:
                observed_label = _LABEL_REVOKED_EVIDENCE
                checks.append("revoked_version_not_evidence")
                if candidate is not None and not (
                    candidate.get("is_revoked") or candidate.get("acl_access_revoked")
                ):
                    # ADR-006：失权呈现，不允许既无标记又非有效证据的第三态
                    failures.append("revoked 候选未携带失权标记")
        else:
            failures.append(f"未知行为场景 query_type: {item.query_type}")
        return observed_label, observed_numeric

    def _score_locators(
        self,
        item: KnowledgeItem,
        surfaced: dict[tuple[str, str], dict[str, Any]],
        checks: list[str],
        failures: list[str],
    ) -> tuple[str | None, float | None]:
        valid = {key for key, c in surfaced.items() if _is_valid_evidence(c)}
        if item.metamorphic_variant == "revoke":
            base = self._base_item(item)
            targets = item.expected_locators or base.expected_locators
            for loc in targets:
                key = (loc.connector, loc.uri)
                if key in valid:
                    failures.append(f"revoke 单位的目标仍为有效证据: {key[1]}")
                else:
                    checks.append(f"revoked_not_evidence:{loc.uri}")
            return None, None
        for loc in item.expected_locators:
            key = (loc.connector, loc.uri)
            if key not in valid:
                failures.append(f"期望 locator 未命中有效证据: {key[1]}")
            else:
                checks.append(f"locator_hit:{loc.uri}")
        return None, None


def _is_valid_evidence(candidate: dict[str, Any]) -> bool:
    breakdown = candidate.get("score") or {}
    return (
        not candidate.get("is_revoked")
        and not candidate.get("acl_access_revoked")
        and breakdown.get("acl_passes_recheck") is True
        and breakdown.get("classification_passes") is True
    )


def _breakdown_problems(breakdown: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    weighted = 0.0
    for field, weight in (
        ("acl_score", 0.3),
        ("freshness_score", 0.2),
        ("relevance_score", 0.35),
        ("classification_score", 0.15),
    ):
        value = breakdown.get(field)
        if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
            problems.append(f"{field} 缺失或越界: {value!r}")
            continue
        weighted += weight * float(value)
    total = breakdown.get("total_score")
    if not isinstance(total, (int, float)) or not 0.0 <= float(total) <= 1.0:
        problems.append(f"total_score 缺失或越界: {total!r}")
    elif not problems and abs(float(total) - weighted) > 1e-6:
        problems.append(f"total_score {total!r} 与加权分解 {weighted!r} 不符")
    for flag in ("acl_passes_recheck", "classification_passes"):
        if not isinstance(breakdown.get(flag), bool):
            problems.append(f"{flag} 缺失: {breakdown.get(flag)!r}")
    return problems


def _answer_matches(
    item: KnowledgeItem, observed_label: str, observed_numeric: float | None
) -> bool:
    mode = item.scoring.mode
    expected = item.ground_truth
    if mode == "numeric":
        if isinstance(expected, str):
            try:
                expected_value = float(expected)
            except ValueError:
                # predicate 形式（freshness_score < 1.0 (aged)）：退回标签等值比较
                return observed_label == expected
            if observed_numeric is None:
                return False
            return abs(observed_numeric - expected_value) <= item.scoring.tolerance
        return observed_label == expected
    if mode == "contains":
        return isinstance(expected, str) and expected in observed_label
    if mode == "set":
        return isinstance(expected, list) and observed_label == str(expected)
    return observed_label == expected
