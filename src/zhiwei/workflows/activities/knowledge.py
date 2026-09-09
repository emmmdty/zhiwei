"""S5 Knowledge Activity for Temporal: knowledge retrieval through Activity boundary.

Per S5 spec §5/§7:
- Knowledge queries go through Knowledge Activity
- Typed candidates produced through canonical events
- ACL pre-filter + hydration re-check

事实源：S5 spec §5、§7、ADR-006。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from zhiwei.knowledge.acl import ACLContext, UnknownACLError
from zhiwei.knowledge.contracts import SourceVersion
from zhiwei.knowledge.planner import KnowledgePlanner
from zhiwei.knowledge.query import (
    KnowledgeQuery,
)
from zhiwei.telemetry.traces import SpanNames, start_span

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeActivityInput:
    """Input for a knowledge activity execution.

    Carries the query, ACL context, and pre-fetched candidates
    for the Knowledge Planner to process.
    """

    run_id: str
    task_id: str
    attempt_no: int
    organization_id: str
    workspace_id: str
    principal_id: str
    query: dict[str, Any] = field(default_factory=dict)
    acl: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    actor_ref: str = "agent-runtime:worker"


@dataclass
class KnowledgeActivityOutput:
    """Output from a knowledge activity execution.

    Carries the planned query, scored candidates, and ACL status
    for the workflow to interpret and record as canonical events.
    """

    task_id: str
    query_id: str
    status: str  # completed | acl_revoked | acl_unknown | error
    candidates: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    candidate_count: int = 0
    error: str | None = None


class KnowledgeActivity:
    """Temporal activity boundary for knowledge retrieval.

    Orchestrates:
    1. Parse input into KnowledgeQuery and ACLContext
    2. Route through Knowledge Planner
    3. Pre-filter by ACL
    4. Generate scored candidates with ACL re-check
    5. Return canonical candidates
    """

    def __init__(
        self,
        planner: KnowledgePlanner | None = None,
    ) -> None:
        self._planner = planner or KnowledgePlanner()

    async def execute(self, input: KnowledgeActivityInput) -> KnowledgeActivityOutput:
        """Execute a knowledge activity through the Knowledge Planner.

        Args:
            input: Knowledge activity input with query, ACL, and candidates.

        Returns:
            KnowledgeActivityOutput with scored candidates and plan.
        """
        try:
            query = KnowledgeQuery.model_validate(input.query)
            acl_context = self._build_acl_context(input.acl)
            versions = self._parse_versions(input.candidates)

            # S9 §6 retrieval span：production 检索路径的 Activity 边界（S5 §7）。
            # Retrieve TaskHandler（runtime/handlers/retrieve.py）与 eval executor
            # 共用同一 planner，但 planner 是 run 无关的共享域服务；run/workspace
            # 身份只在 Activity 输入上存在，seam 选在身份可得的这一层，经 W3C
            # context 与 api/run span 关联。metadata-only：标识与规模计数，
            # query 文本与候选内容绝不进 span。start_span 不吞异常——ACL/执行
            # 异常照常被下方 except 捕获并映射为 output 状态。
            with start_span(
                SpanNames.RETRIEVAL,
                {
                    "run_id": input.run_id,
                    "workspace_id": input.workspace_id,
                    "source_version_count": len(versions),
                },
            ) as span:
                plan = self._planner.plan(query)
                candidates = self._planner.generate_candidates(
                    query, versions, acl_context
                )
                span.set_attribute("candidate_count", len(candidates))

            candidate_dicts = [c.model_dump(mode="json") for c in candidates]

            return KnowledgeActivityOutput(
                task_id=input.task_id,
                query_id=query.query_id,
                status="completed",
                candidates=candidate_dicts,
                plan=plan.model_dump(mode="json"),
                candidate_count=len(candidate_dicts),
            )

        except UnknownACLError as exc:
            logger.warning("ACL unknown during knowledge activity: %s", exc)
            return KnowledgeActivityOutput(
                task_id=input.task_id,
                query_id=input.query.get("query_id", "unknown"),
                status="acl_unknown",
                error=str(exc),
            )

        except Exception as exc:
            logger.exception("Knowledge activity execution failed")
            return KnowledgeActivityOutput(
                task_id=input.task_id,
                query_id=input.query.get("query_id", "unknown"),
                status="error",
                error=str(exc),
            )

    def _build_acl_context(self, acl_dict: dict[str, Any]) -> ACLContext:
        """Build ACLContext from dict representation."""
        return ACLContext(
            principal_id=UUID(acl_dict["principal_id"]),
            organization_id=UUID(acl_dict["organization_id"]),
            workspace_id=UUID(acl_dict["workspace_id"]),
            allowed_principals=frozenset(acl_dict.get("allowed_principals", [])),
            allowed_groups=frozenset(acl_dict.get("allowed_groups", [])),
            denied_principals=frozenset(acl_dict.get("denied_principals", [])),
            classification_ceiling=acl_dict.get("classification_ceiling", "PUBLIC"),
        )

    def _parse_versions(self, raw: list[dict[str, Any]]) -> list[SourceVersion]:
        """Parse raw candidate dicts into SourceVersion objects."""
        from datetime import datetime

        from zhiwei.knowledge.contracts import (
            ACLSnapshot,
            Classification,
            Locator,
            SourceVersion,
            SourceVersionState,
        )

        versions: list[SourceVersion] = []
        for raw_dict in raw:
            try:
                acl_raw = raw_dict.get("acl", {})
                acl = ACLSnapshot(
                    allowed_principals=tuple(acl_raw.get("allowed_principals", [])),
                    denied_principals=tuple(acl_raw.get("denied_principals", [])),
                    allowed_groups=tuple(acl_raw.get("allowed_groups", [])),
                )

                locator_raw = raw_dict.get("locator", {})
                locator = Locator(
                    connector=locator_raw.get("connector", "unknown"),
                    uri=locator_raw.get("uri", "unknown"),
                )

                version = SourceVersion(
                    id=UUID(raw_dict["id"]),
                    source_object_id=UUID(raw_dict["source_object_id"]),
                    version_seq=raw_dict.get("version_seq", 1),
                    locator=locator,
                    content_digest=raw_dict["content_digest"],
                    observed_at=datetime.fromisoformat(raw_dict["observed_at"]),
                    valid_at=datetime.fromisoformat(
                        raw_dict.get("valid_at", raw_dict["observed_at"])
                    ),
                    acl=acl,
                    classification=Classification(
                        raw_dict.get("classification", "PUBLIC")
                    ),
                    state=SourceVersionState(raw_dict.get("state", "active")),
                )
                versions.append(version)
            except Exception as exc:
                logger.warning("Failed to parse candidate in activity: %s", exc)
        return versions
