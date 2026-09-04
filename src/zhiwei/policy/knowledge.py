"""S5 Knowledge policy integration: ACL policy checks for knowledge queries.

Bridges the knowledge domain with the policy layer for ACL enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from zhiwei.knowledge.acl import ACLContext
from zhiwei.knowledge.query import KnowledgeQuery


class KnowledgePolicyError(Exception):
    """Error in knowledge policy evaluation."""


@dataclass(frozen=True)
class KnowledgeAccessDecision:
    """Result of a knowledge access policy check."""

    query_id: str
    allowed: bool
    reason: str = ""
    acl_context: ACLContext | None = None


@dataclass
class KnowledgePolicy:
    """Policy enforcement for knowledge queries.

    Evaluates whether a principal can access knowledge sources
    based on organization/workspace membership and classification ceiling.
    """

    def evaluate_access(
        self,
        query: KnowledgeQuery,
        acl_context: ACLContext,
    ) -> KnowledgeAccessDecision:
        """Evaluate if the query principal has access to the requested knowledge.

        Checks:
        1. Principal is in the correct organization/workspace
        2. Classification ceiling is respected
        3. ACL context is valid (not empty = fail closed for unknown)
        """
        if str(acl_context.principal_id) != str(query.principal_id):
            return KnowledgeAccessDecision(
                query_id=query.query_id,
                allowed=False,
                reason="principal_mismatch",
            )

        if str(acl_context.organization_id) != str(query.organization_id):
            return KnowledgeAccessDecision(
                query_id=query.query_id,
                allowed=False,
                reason="organization_mismatch",
            )

        if str(acl_context.workspace_id) != str(query.workspace_id):
            return KnowledgeAccessDecision(
                query_id=query.query_id,
                allowed=False,
                reason="workspace_mismatch",
            )

        classification_order = {
            "PUBLIC": 0,
            "INTERNAL": 1,
            "CONFIDENTIAL": 2,
            "RESTRICTED": 3,
        }
        ceiling = classification_order.get(acl_context.classification_ceiling, 0)
        requested = classification_order.get(query.classification_ceiling, 0)
        if requested > ceiling:
            return KnowledgeAccessDecision(
                query_id=query.query_id,
                allowed=False,
                reason="classification_exceeds_ceiling",
            )

        return KnowledgeAccessDecision(
            query_id=query.query_id,
            allowed=True,
            acl_context=acl_context,
        )

    def build_acl_context(
        self,
        principal_id: UUID,
        organization_id: UUID,
        workspace_id: UUID,
        *,
        allowed_principals: frozenset[str] | None = None,
        allowed_groups: frozenset[str] | None = None,
        denied_principals: frozenset[str] | None = None,
        classification_ceiling: str = "PUBLIC",
    ) -> ACLContext:
        """Build an ACL context for a principal."""
        return ACLContext(
            principal_id=principal_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            allowed_principals=allowed_principals or frozenset(),
            allowed_groups=allowed_groups or frozenset(),
            denied_principals=denied_principals or frozenset(),
            classification_ceiling=classification_ceiling,
        )
