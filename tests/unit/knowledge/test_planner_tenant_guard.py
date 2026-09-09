"""F-R3-07 RED：KnowledgePlanner (query.org/ws) vs (version 归属) 一致性守卫。

finding 证据：planner.generate_candidates 不校验 version 归属 org 与
query.organization_id 一致（SourceVersion 契约不带归属，归属在 SourceObject 上，
预取层是唯一能提供归属映射的位置）——未来 DB 预取接线若遗漏 org 谓词，planner
不构成第二道防线；跨租户版本会以候选形态（含 locator/uri/digest）流向下游。

守卫设计（fail closed）：
- generate_candidates 增加可选 version_ownership（version id → (org, ws)，
  与 SourceObject 契约同形，均为非空 UUID——对抗审查 gap 2 收紧：org 级
  逃逸分支不存在于数据模型）；
- 提供时：任一版本归属与 query.org 不一致、或 ws 归属与 query.ws 不一致、
  或映射缺失条目（声明了归属却不完整 = 接线 bug）→ 抛
  KnowledgeTenantMismatchError，一个候选都不产出；
- 缺省 None 时守卫不启用：冻结 eval 语料的跨 org 场景由 ACL 语义承载
  （target org 物化为 ACLSnapshot.allowed_groups，经 pre_filter 拒绝并打
  _LABEL_CROSS_ORG 标签）——org 守卫若对该场景生效会改变冻结评测结论；
  生产预取接线（S11）必须提供归属映射，此为登记约束而非可选优化。

RED 失败模式：generate_candidates 无 version_ownership 参数（TypeError）/
planner 模块无 KnowledgeTenantMismatchError 属性——均为守卫缺失本体。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

import zhiwei.knowledge.planner as planner_module
from zhiwei.knowledge.acl import ACLContext
from zhiwei.knowledge.contracts import (
    ACLSnapshot,
    Classification,
    Locator,
    SourceVersion,
    SourceVersionState,
)
from zhiwei.knowledge.planner import KnowledgePlanner
from zhiwei.knowledge.query import EvidenceRequirement, KnowledgeQuery, QuerySource


def _version(
    *,
    acl: ACLSnapshot | None = None,
    state: SourceVersionState = SourceVersionState.ACTIVE,
    classification: Classification = Classification.PUBLIC,
) -> SourceVersion:
    return SourceVersion(
        id=uuid4(),
        source_object_id=uuid4(),
        version_seq=1,
        locator=Locator(connector="test", uri="test://doc/1"),
        content_digest="sha256:" + "a" * 64,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        valid_at=datetime(2026, 1, 1, tzinfo=UTC),
        acl=acl or ACLSnapshot(),
        classification=classification,
        state=state,
    )


def _query() -> KnowledgeQuery:
    return KnowledgeQuery(
        query_id="q-guard",
        organization_id=uuid4(),
        workspace_id=uuid4(),
        principal_id=uuid4(),
        text="tenant guard",
        sources=(QuerySource.DOC,),
        evidence_requirement=EvidenceRequirement.ANY,
    )


def _acl_allowing(query: KnowledgeQuery) -> ACLContext:
    return ACLContext(
        principal_id=query.principal_id,
        organization_id=query.organization_id,
        workspace_id=query.workspace_id,
    )


class TestTenantGuard:
    def test_cross_org_version_fails_closed(self) -> None:
        query = _query()
        version = _version()
        planner = KnowledgePlanner()
        with pytest.raises(planner_module.KnowledgeTenantMismatchError):
            planner.generate_candidates(
                query,
                [version],
                _acl_allowing(query),
                # 归属元组与 SourceObject 同形（workspace_id 非空）：
                # 跨 org 以完整 (org, ws) 表达
                version_ownership={version.id: (uuid4(), uuid4())},
            )

    def test_cross_workspace_version_fails_closed(self) -> None:
        query = _query()
        version = _version()
        planner = KnowledgePlanner()
        with pytest.raises(planner_module.KnowledgeTenantMismatchError):
            planner.generate_candidates(
                query,
                [version],
                _acl_allowing(query),
                version_ownership={version.id: (query.organization_id, uuid4())},
            )

    def test_incomplete_ownership_mapping_fails_closed(self) -> None:
        query = _query()
        version = _version()
        planner = KnowledgePlanner()
        with pytest.raises(planner_module.KnowledgeTenantMismatchError):
            planner.generate_candidates(
                query, [version], _acl_allowing(query), version_ownership={}
            )

    def test_matching_ownership_produces_candidates(self) -> None:
        query = _query()
        allowed = ACLSnapshot(allowed_principals=(str(query.principal_id),))
        version = _version(acl=allowed)
        planner = KnowledgePlanner()
        candidates = planner.generate_candidates(
            query,
            [version],
            _acl_allowing(query),
            version_ownership={version.id: (query.organization_id, query.workspace_id)},
        )
        assert len(candidates) == 1
        assert candidates[0].source_version_id == str(version.id)

    def test_guard_does_not_bypass_acl(self) -> None:
        """org 一致的版本仍受 ACL 约束（守卫是第二道防线，不是替代）。"""
        query = _query()
        version = _version()  # 默认空快照 → pre_filter unknown → 不产出
        planner = KnowledgePlanner()
        candidates = planner.generate_candidates(
            query,
            [version],
            _acl_allowing(query),
            version_ownership={version.id: (query.organization_id, query.workspace_id)},
        )
        assert candidates == []

    def test_guard_inert_without_ownership_mapping(self) -> None:
        """缺省 None：现状保持（eval 语料跨 org 场景由 ACL 语义承载）。"""
        query = _query()
        allowed = ACLSnapshot(allowed_principals=(str(query.principal_id),))
        version = _version(acl=allowed)
        planner = KnowledgePlanner()
        candidates = planner.generate_candidates(query, [version], _acl_allowing(query))
        assert len(candidates) == 1
