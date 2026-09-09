"""S6 Evidence 可见性服务：ADR-006 时态语义在 Evidence 域的服务侧等价物。

事实源：specs/s6-evidence-ask.md §3/§6、ADR-006。

- 可复算性与可见性解耦：Evidence 永远可被系统复算（审计/eval 通道），
  但对用户的可见性按**当前** ACL 重新校验并 fail closed；
- 失权呈现：不可见的 ref 渲染 ``evidence_access_revoked`` 占位（带 reason），
  不携带任何内容字段，也不从结果集中静默移除；
- Auditor 走独立审计通道可见；ACL 未知（None）对用户 fail closed 为占位；
- reference_only 不得支撑 Fact 类 claim：服务级拒绝（ADR-003），挡住
  模型层不可构造、只能经 wire 注入的组合。

ACL 判定复用 zhiwei.knowledge.acl.check_acl_snapshot（deny-override/unknown
语义单一实现），本模块不复制第二套 ACL 逻辑。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.claims import FactClaim, QuoteClaim
from zhiwei.evidence.errors import ClaimLevelViolationError
from zhiwei.evidence.refs import EvidenceRef
from zhiwei.knowledge.acl import ACLContext, check_acl_snapshot
from zhiwei.knowledge.contracts import ACLSnapshot

REVOKED_PLACEHOLDER_STATUS = "evidence_access_revoked"
VISIBLE_STATUS = "visible"


class PrincipalKind(StrEnum):
    """Evidence 可见性通道（ADR-006）。"""

    USER = "user"
    AUDITOR = "auditor"
    EVAL_RECOMPUTE = "eval_recompute"


@dataclass(frozen=True, slots=True)
class EvidencePrincipal:
    """发起 Evidence 查询的主体的通道与当前 ACL 上下文。"""

    kind: PrincipalKind
    acl_context: ACLContext


@dataclass(frozen=True, slots=True)
class EvidenceView:
    """单个 EvidenceRef 对查询主体的可见性视图（占位不含内容）。"""

    ref_id: UUID
    status: str
    reason: str = ""
    content: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        if self.status != VISIBLE_STATUS:
            # 失权呈现：占位只携带元数据，任何内容字段都不得出现
            return {
                "ref_id": str(self.ref_id),
                "status": self.status,
                "reason": self.reason,
            }
        payload: dict[str, Any] = {
            "ref_id": str(self.ref_id),
            "status": self.status,
            "reason": self.reason,
        }
        if self.content is not None:
            payload.update(self.content)
        return payload


def _visible_content(ref: EvidenceRef) -> dict[str, Any]:
    """可见视图的载荷：ref 的完整绑定元数据（locator/查询/digest）。

    EvidenceRef 本身不携带 claim 或 answer 内容；占位视图则连 ref 绑定
    字段也不出现（见 ``EvidenceView.as_dict``）。
    """
    return ref.model_dump(mode="json")


def resolve_evidence_views(
    bundle: EvidenceBundle,
    principal: EvidencePrincipal,
    *,
    current_acl: Callable[[EvidenceRef], ACLSnapshot | None],
) -> tuple[EvidenceView, ...]:
    """按当前 ACL 解析 bundle 内每个 ref 对主体的可见性（ADR-006）。

    - AUDITOR / EVAL_RECOMPUTE：审计与系统复算通道，不受用户可见性 ACL 约束；
    - USER：逐 ref 按当前 ACL 重新校验，deny/unknown 一律渲染
      ``evidence_access_revoked`` 占位并 fail closed，条目不消失。
    """
    if principal.kind in (PrincipalKind.AUDITOR, PrincipalKind.EVAL_RECOMPUTE):
        return tuple(
            EvidenceView(
                ref_id=ref.ref_id,
                status=VISIBLE_STATUS,
                reason="audit_channel" if principal.kind is PrincipalKind.AUDITOR
                else "eval_recompute_channel",
                content=_visible_content(ref),
            )
            for ref in bundle.evidence_refs
        )
    views: list[EvidenceView] = []
    for ref in bundle.evidence_refs:
        snapshot = current_acl(ref)
        if snapshot is None:
            views.append(
                EvidenceView(
                    ref_id=ref.ref_id,
                    status=REVOKED_PLACEHOLDER_STATUS,
                    reason="acl_unknown",
                )
            )
            continue
        result = check_acl_snapshot(snapshot, principal.acl_context)
        if result.allowed:
            views.append(
                EvidenceView(
                    ref_id=ref.ref_id,
                    status=VISIBLE_STATUS,
                    reason=result.reason,
                    content=_visible_content(ref),
                )
            )
        else:
            views.append(
                EvidenceView(
                    ref_id=ref.ref_id,
                    status=REVOKED_PLACEHOLDER_STATUS,
                    reason=result.reason or "acl_not_granted",
                )
            )
    return tuple(views)


def service_rejects_reference_only_fact(bundle_dict: dict[str, Any]) -> bool:
    """服务级 claim 等级检查（ADR-003）。

    wire bundle 在进入 final 落账前复检：Fact/Quote claim 绑定 reference_only
    ref 即拒绝（ClaimLevelViolationError）。模型层不可构造该组合，只能经
    wire 注入——这正是本检查要挡的输入。解析失败原样抛出（fail closed）。
    """
    bundle = EvidenceBundle.model_validate(bundle_dict)
    for claim in bundle.claims:
        if isinstance(claim, (FactClaim, QuoteClaim)):
            for ref in claim.evidence_refs:
                if ref.reproducibility_level.value == "reference_only":
                    raise ClaimLevelViolationError(
                        f"{claim.claim_type.value} claim requires replayable or "
                        f"copy_frozen evidence; got reference_only on {ref.ref_type}"
                    )
    return True
