"""S7-T4 WriteMemoryCandidate handler: typed task → Memory Activity/policy → candidate/refusal.

Ask/Discover 不直接调用 repository；Context retrieval 由 Context Compiler 的 Memory port 完成。

事实源：S7 spec §3（write policy rules）、§7（required tests）。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from zhiwei.contracts.identifiers import new_id
from zhiwei.memory.candidates import CandidateQueue, DedupKey
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
    SourceRef,
)
from zhiwei.memory.policy import evaluate_write_policy
from zhiwei.runtime.handlers.base import TaskHandler, TaskInput, TaskOutput

logger = logging.getLogger(__name__)


class WriteMemoryCandidateHandler(TaskHandler):
    """Handler for the WriteMemoryCandidate primitive.

    Routes typed memory write tasks through the write policy to produce
    either a candidate record (queued for confirmation) or a refusal event.
    """

    def __init__(self, queue: CandidateQueue | None = None) -> None:
        self._queue = queue or CandidateQueue()

    @property
    def primitive_type(self) -> str:
        return "WriteMemoryCandidate"

    @property
    def handler_version(self) -> int:
        return 1

    def execute(self, input: TaskInput) -> TaskOutput:
        """Execute a write-memory-candidate task.

        input_values must contain:
        - memory: dict representation of a MemoryRecord to write
        - actor_id: str UUID of the actor performing the write

        Output values contain:
        - status: completed | refused
        - record_id: UUID of the created record (if completed)
        - refusal_reason: reason string (if refused)
        - decision: auto_confirm | candidate | forbidden
        """
        values = input.input_values
        memory_dict = values.get("memory")
        if not memory_dict:
            return TaskOutput(
                output_values={
                    "status": "refused",
                    "refusal_reason": "missing memory in input_values",
                    "decision": "forbidden",
                }
            )

        actor_id_str = values.get("actor_id")
        if not actor_id_str:
            return TaskOutput(
                output_values={
                    "status": "refused",
                    "refusal_reason": "missing actor_id in input_values",
                    "decision": "forbidden",
                }
            )

        try:
            actor_id = UUID(actor_id_str)
        except ValueError:
            return TaskOutput(
                output_values={
                    "status": "refused",
                    "refusal_reason": f"invalid actor_id: {actor_id_str}",
                    "decision": "forbidden",
                }
            )

        # Build MemoryRecord from dict
        try:
            record = self._build_record(memory_dict, actor_id)
        except Exception as exc:
            return TaskOutput(
                output_values={
                    "status": "refused",
                    "refusal_reason": f"invalid memory record: {exc}",
                    "decision": "forbidden",
                }
            )

        # Evaluate write policy
        policy_result = evaluate_write_policy(
            scope=record.scope,
            mem_type=record.type,
            sensitivity=record.sensitivity,
            subject=record.subject,
            canonical_value=record.canonical_value,
        )

        if policy_result.decision == "forbidden":
            logger.info(
                "Memory write forbidden: record_id=%s reason=%s",
                record.id,
                policy_result.reason,
            )
            return TaskOutput(
                output_values={
                    "status": "refused",
                    "refusal_reason": policy_result.reason,
                    "decision": policy_result.decision,
                }
            )

        # Apply policy: auto-confirm or queue as candidate
        try:
            if policy_result.decision == "auto_confirm":
                confirmed = record.model_copy(update={"status": MemoryStatus.CONFIRMED})
                dedup = DedupKey.from_record(confirmed)
                self._queue.records[dedup.as_tuple()] = confirmed
                final_record = confirmed
            else:
                final_record = self._queue.add_candidate(record)
        except Exception as exc:
            logger.exception("Failed to write memory candidate")
            return TaskOutput(
                output_values={
                    "status": "refused",
                    "refusal_reason": f"queue error: {exc}",
                    "decision": policy_result.decision,
                }
            )

        logger.info(
            "Memory write %s: record_id=%s decision=%s",
            policy_result.decision,
            final_record.id,
            policy_result.decision,
        )

        return TaskOutput(
            output_values={
                "status": "completed",
                "record_id": str(final_record.id),
                "decision": policy_result.decision,
                "reason": policy_result.reason,
            }
        )

    @staticmethod
    def _build_record(memory_dict: dict[str, Any], actor_id: UUID) -> MemoryRecord:
        """Build a MemoryRecord from a dict, filling defaults for missing fields."""
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
            id=UUID(memory_dict["id"]) if "id" in memory_dict else new_id(),
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
