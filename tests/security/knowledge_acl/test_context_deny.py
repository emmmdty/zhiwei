"""S5 Security: query-time context deny 覆盖索引期授权（ADR-006 killer 测试）。

「ACL revoked between index and query time」的唯一表达通道是
ACLContext.denied_principals：索引期 snapshot 允许、查询期 context deny 名单
含该 principal 时，check_acl_snapshot 必须拒绝（deny overrides allow）。
删除该分支的变异体不得通过本测试（corpus 场景 KT-AF-002 暴露的缺口）。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from zhiwei.knowledge.acl import ACLContext, check_acl_snapshot
from zhiwei.knowledge.contracts import ACLSnapshot

_ORG_ID = UUID("11111111-1111-4111-8111-111111111111")
_WS_ID = UUID("22222222-2222-4222-8222-222222222222")


def _context(
    principal_id: UUID,
    *,
    denied_principals: frozenset[str] = frozenset(),
) -> ACLContext:
    return ACLContext(
        principal_id=principal_id,
        organization_id=_ORG_ID,
        workspace_id=_WS_ID,
        denied_principals=denied_principals,
    )


class TestContextDenyOverridesSnapshotAllow:
    def test_query_time_deny_rejects_index_time_allow(self) -> None:
        """snapshot 侧 allowed + context deny 名单含该 principal → REJECTED_ACL。"""
        pid = uuid4()
        snapshot = ACLSnapshot(allowed_principals=(str(pid),))
        result = check_acl_snapshot(
            snapshot, _context(pid, denied_principals=frozenset({str(pid)}))
        )
        assert result.allowed is False
        assert result.reason == "denied_principal"

    def test_deny_set_without_principal_leaves_snapshot_verdict(self) -> None:
        """正向对照：deny 集非空但不含该 principal → 仍按 snapshot 判定（允许）。"""
        pid = uuid4()
        snapshot = ACLSnapshot(allowed_principals=(str(pid),))
        result = check_acl_snapshot(
            snapshot, _context(pid, denied_principals=frozenset({str(uuid4())}))
        )
        assert result.allowed is True
