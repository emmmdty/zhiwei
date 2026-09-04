"""S7-T4 Memory Activity for Temporal: memory retrieval and write through Activity boundary.

Per S7 spec §4/§5:
- Memory retrieval goes through Memory Activity
- Typed candidates produced through canonical events
- Write pipeline: policy evaluation → candidate queue or auto-confirm

事实源：S7 spec §4、§5、ADR-009。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from zhiwei.memory.candidates import CandidateQueue, DedupKey
from zhiwei.memory.conflicts import TemporalConflictManager
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
    SourceRef,
)
from zhiwei.memory.policy import evaluate_write_policy
from zhiwei.memory.retrieval import HardFilters, MemoryRetriever

logger = logging.getLogger(__name__)


@dataclass
class MemoryActivityInput:
    """Input for a memory activity execution.

    Carries the action (retrieve or write), query/filters, and ACL context
    for the Memory Activity to process.
    """

    run_id: str
    task_id: str
    attempt_no: int
    organization_id: str
    workspace_id: str
    principal_id: str
    action: str  # "retrieve" | "write"
    query: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    actor_ref: str = "agent-runtime:worker"


@dataclass
class MemoryActivityOutput:
    """Output from a memory activity execution.

    Carries the retrieval results or write outcome for the workflow
    to interpret and record as canonical events.
    """

    task_id: str
    status: str  # completed | refused | error
    action: str
    results: list[dict[str, Any]] = field(default_factory=list)
    result_count: int = 0
    record_id: str | None = None
    decision: str | None = None
    refusal_reason: str | None = None
    error: str | None = None


class MemoryActivity:
    """Temporal activity boundary for memory operations.

    Orchestrates:
    1. Retrieve: parse filters → hard filter → multi-stage retrieval → rerank
    2. Write: parse memory → evaluate policy → candidate queue or auto-confirm
    """

    def __init__(
        self,
        retriever: MemoryRetriever | None = None,
        queue: CandidateQueue | None = None,
        conflict_manager: TemporalConflictManager | None = None,
    ) -> None:
        self._retriever = retriever or MemoryRetriever()
        self._queue = queue or CandidateQueue()
        self._conflict_manager = conflict_manager or TemporalConflictManager()

    async def execute(self, input: MemoryActivityInput) -> MemoryActivityOutput:
        """Execute a memory activity.

        Args:
            input: Memory activity input with action, query/filters, and memory data.

        Returns:
            MemoryActivityOutput with results or write outcome.
        """
        try:
            if input.action == "retrieve":
                return self._execute_retrieve(input)
            elif input.action == "write":
                return self._execute_write(input)
            else:
                return MemoryActivityOutput(
                    task_id=input.task_id,
                    status="error",
                    action=input.action,
                    error=f"unknown action: {input.action}",
                )
        except Exception as exc:
            logger.exception("Memory activity execution failed")
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="error",
                action=input.action,
                error=str(exc),
            )

    def _execute_retrieve(self, input: MemoryActivityInput) -> MemoryActivityOutput:
        """Execute a memory retrieval."""
        filters = self._build_filters(input.filters)
        query_text = input.query.get("text", "")
        query_key = input.query.get("key")
        query_embedding = input.query.get("embedding")
        top_k = input.query.get("top_k", 10)

        response = self._retriever.retrieve(
            query_text=query_text,
            filters=filters,
            query_key=query_key,
            query_embedding=query_embedding,
            top_k=top_k,
        )

        result_dicts = [
            {
                "record_id": str(r.record.id),
                "score": r.score,
                "reason": r.reason,
                "provenance": list(r.provenance),
                "conflicts": [str(c) for c in r.conflicts],
                "freshness_seconds": r.freshness_seconds,
                "subject": r.record.subject,
                "key": r.record.key,
                "canonical_value": r.record.canonical_value,
                "status": r.record.status.value,
            }
            for r in response.results
        ]

        return MemoryActivityOutput(
            task_id=input.task_id,
            status="completed",
            action="retrieve",
            results=result_dicts,
            result_count=response.total_passed,
        )

    def _execute_write(self, input: MemoryActivityInput) -> MemoryActivityOutput:
        """Execute a memory write."""
        memory_dict = input.memory
        actor_id_str = input.principal_id

        try:
            actor_id = UUID(actor_id_str)
        except ValueError:
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="refused",
                action="write",
                refusal_reason=f"invalid principal_id: {actor_id_str}",
            )

        try:
            record = self._build_record(memory_dict, actor_id)
        except Exception as exc:
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="refused",
                action="write",
                refusal_reason=f"invalid memory record: {exc}",
            )

        policy_result = evaluate_write_policy(
            scope=record.scope,
            mem_type=record.type,
            sensitivity=record.sensitivity,
            subject=record.subject,
            canonical_value=record.canonical_value,
        )

        if policy_result.decision == "forbidden":
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="refused",
                action="write",
                refusal_reason=policy_result.reason,
                decision=policy_result.decision,
            )

        try:
            if policy_result.decision == "auto_confirm":
                confirmed = record.model_copy(update={"status": MemoryStatus.CONFIRMED})
                dedup = DedupKey.from_record(confirmed)
                self._queue.records[dedup.as_tuple()] = confirmed
                final_record = confirmed
            else:
                final_record = self._queue.add_candidate(record)
        except Exception as exc:
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="refused",
                action="write",
                refusal_reason=f"queue error: {exc}",
                decision=policy_result.decision,
            )

        return MemoryActivityOutput(
            task_id=input.task_id,
            status="completed",
            action="write",
            record_id=str(final_record.id),
            decision=policy_result.decision,
        )

    @staticmethod
    def _build_filters(filters_dict: dict[str, Any]) -> HardFilters:
        """Build HardFilters from dict representation."""
        return HardFilters(
            organization_id=UUID(filters_dict["organization_id"])
            if "organization_id" in filters_dict
            else None,
            workspace_id=UUID(filters_dict["workspace_id"])
            if "workspace_id" in filters_dict
            else None,
            scope_subject_id=UUID(filters_dict["scope_subject_id"])
            if "scope_subject_id" in filters_dict
            else None,
            allowed_principals=frozenset(filters_dict.get("allowed_principals", [])),
            max_sensitivity=SensitivityLevel(filters_dict["max_sensitivity"])
            if "max_sensitivity" in filters_dict
            else None,
            allowed_statuses=frozenset(
                MemoryStatus(s) for s in filters_dict["allowed_statuses"]
            )
            if "allowed_statuses" in filters_dict
            else None,
            allowed_profile_refs=frozenset(filters_dict.get("allowed_profile_refs", []))
            if "allowed_profile_refs" in filters_dict
            else None,
        )

    @staticmethod
    def _build_record(memory_dict: dict[str, Any], actor_id: UUID) -> MemoryRecord:
        """Build a MemoryRecord from a dict."""
        now_str = memory_dict.get("created_at", "2025-01-01T00:00:00+00:00")

        source_refs_raw = memory_dict.get("source_refs", [])
        source_refs = tuple(
            SourceRef(
                source_id=sr.get("source_id", ""),
                source_type=sr.get("source_type", ""),
                description=sr.get("description", ""),
            )
            for sr in source_refs_raw
        )

        return MemoryRecord(
            id=UUID(memory_dict["id"]) if "id" in memory_dict else UUID(int=0),
            version=memory_dict.get("version", 1),
            organization_id=UUID(memory_dict["organization_id"]),
            workspace_id=UUID(memory_dict["workspace_id"]),
            scope=MemoryScope(memory_dict["scope"]),
            scope_subject_id=UUID(memory_dict["scope_subject_id"]),
            type=MemoryType(memory_dict["type"]),
            subject=memory_dict["subject"],
            key=memory_dict["key"],
            canonical_value=memory_dict["canonical_value"],
            source_refs=source_refs,
            observed_at=memory_dict.get("observed_at", now_str),
            confidence=memory_dict.get("confidence", 0.5),
            sensitivity=SensitivityLevel(memory_dict.get("sensitivity", "low")),
            status=MemoryStatus(memory_dict.get("status", "candidate")),
            author_ref=UUID(memory_dict.get("author_ref", str(actor_id))),
            created_at=memory_dict.get("created_at", now_str),
            updated_at=memory_dict.get("updated_at", now_str),
        )
