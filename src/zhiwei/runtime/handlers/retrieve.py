"""S5 Retrieve handler: Task input → Knowledge Activity → typed candidates → canonical events.

事实源：S5 spec §5/§7、ADR-006。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from zhiwei.knowledge.acl import ACLContext, UnknownACLError
from zhiwei.knowledge.contracts import SourceVersion
from zhiwei.knowledge.planner import KnowledgePlanner
from zhiwei.knowledge.query import (
    KnowledgeQuery,
)
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput

logger = logging.getLogger(__name__)


class RetrieveHandler(TaskHandler):
    """Handler for the Retrieve primitive.

    Executes knowledge queries through the Knowledge Planner:
    1. Parse input_values into a KnowledgeQuery
    2. Plan the query
    3. Pre-filter by ACL
    4. Generate scored candidates
    5. Return canonical candidates in TaskOutput
    """

    def __init__(self, planner: KnowledgePlanner | None = None) -> None:
        self._planner = planner or KnowledgePlanner()

    @property
    def primitive_type(self) -> str:
        return "Retrieve"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        """Execute a retrieve task.

        Input values must contain:
        - query: dict representation of KnowledgeQuery
        - candidates: list of dicts representing SourceVersions (pre-fetched)
        - acl: dict representation of ACLContext

        Output values contain:
        - candidates: list of QueryCandidate dicts
        - query_id: the original query id
        - status: completed | acl_revoked | error
        """
        values = input.input_values

        query_dict = values.get("query")
        if not query_dict:
            return TaskOutput(
                output_values={
                    "status": "error",
                    "error": "missing query in input_values",
                    "candidates": [],
                }
            )

        acl_dict = values.get("acl")
        if not acl_dict:
            return TaskOutput(
                output_values={
                    "status": "error",
                    "error": "missing acl in input_values",
                    "candidates": [],
                }
            )

        try:
            query = KnowledgeQuery.model_validate(query_dict)
            acl_context = ACLContext(
                principal_id=UUID(acl_dict["principal_id"]),
                organization_id=UUID(acl_dict["organization_id"]),
                workspace_id=UUID(acl_dict["workspace_id"]),
                allowed_principals=frozenset(acl_dict.get("allowed_principals", [])),
                allowed_groups=frozenset(acl_dict.get("allowed_groups", [])),
                denied_principals=frozenset(acl_dict.get("denied_principals", [])),
                classification_ceiling=acl_dict.get("classification_ceiling", "PUBLIC"),
            )
        except Exception as exc:
            return TaskOutput(
                output_values={
                    "status": "error",
                    "error": f"invalid input: {exc}",
                    "candidates": [],
                }
            )

        raw_candidates = values.get("candidates", [])
        versions = self._parse_versions(raw_candidates)

        try:
            plan = self._planner.plan(query)
            candidates = self._planner.generate_candidates(
                query, versions, acl_context
            )
        except UnknownACLError as exc:
            return TaskOutput(
                output_values={
                    "status": "error",
                    "error": f"acl_unknown: {exc}",
                    "candidates": [],
                    "query_id": query.query_id,
                }
            )
        except Exception as exc:
            logger.exception("Knowledge planner execution failed")
            return TaskOutput(
                output_values={
                    "status": "error",
                    "error": str(exc),
                    "candidates": [],
                    "query_id": query.query_id,
                }
            )

        candidate_dicts = [c.model_dump(mode="json") for c in candidates]

        return TaskOutput(
            output_values={
                "status": "completed",
                "query_id": query.query_id,
                "candidates": candidate_dicts,
                "plan": plan.model_dump(mode="json"),
                "candidate_count": len(candidate_dicts),
            }
        )

    def _parse_versions(self, raw: list[dict[str, Any]]) -> list[SourceVersion]:
        """Parse raw candidate dicts into SourceVersion objects."""
        versions: list[SourceVersion] = []
        for raw_dict in raw:
            try:
                from datetime import datetime

                from zhiwei.knowledge.contracts import (
                    ACLSnapshot,
                    Classification,
                    Locator,
                    SourceVersion,
                    SourceVersionState,
                )

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
                    valid_at=datetime.fromisoformat(raw_dict.get("valid_at", raw_dict["observed_at"])),
                    acl=acl,
                    classification=Classification(raw_dict.get("classification", "PUBLIC")),
                    state=SourceVersionState(raw_dict.get("state", "active")),
                )
                versions.append(version)
            except Exception as exc:
                logger.warning("Failed to parse candidate: %s", exc)
        return versions
