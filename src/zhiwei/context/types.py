"""S3-T3 Context classification types.

四类上下文分类（MODELS.md §3, S3 spec §3）:
- authoritative: 客观事实、约束、任务、实体、决策、冲突、证据、操作、审批、预算、义务
- conversational: 确定性摘要 + 源事件引用
- recoverable: 仅保留 artifact ref
- opaque: 不持久化
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ContextCategory(StrEnum):
    """四类上下文分类."""

    AUTHORITATIVE = "authoritative"
    CONVERSATIONAL = "conversational"
    RECOVERABLE = "recoverable"
    OPAQUE = "opaque"


class AuthoritativeKind(StrEnum):
    """authoritative 子类型：客观内容的细分类别."""

    OBJECTIVE = "objective"
    CONSTRAINT = "constraint"
    TASK = "task"
    ENTITY = "entity"
    DECISION = "decision"
    CONFLICT = "conflict"
    EVIDENCE = "evidence"
    ACTION = "action"
    APPROVAL = "approval"
    BUDGET = "budget"
    OBLIGATION = "obligation"


class SourceRef:
    """Every context item traces back to canonical events.

    Immutable reference linking a context item to its originating event(s).
    """

    __slots__ = ("event_digest", "event_id", "event_type", "sequence_no")

    def __init__(
        self,
        event_id: str,
        sequence_no: int,
        event_type: str,
        event_digest: str,
    ) -> None:
        self.event_id = event_id
        self.sequence_no = sequence_no
        self.event_type = event_type
        self.event_digest = event_digest

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SourceRef):
            return NotImplemented
        return (
            self.event_id == other.event_id
            and self.sequence_no == other.sequence_no
            and self.event_type == other.event_type
            and self.event_digest == other.event_digest
        )

    def __hash__(self) -> int:
        return hash((self.event_id, self.sequence_no, self.event_type, self.event_digest))

    def __repr__(self) -> str:
        return (
            f"SourceRef(event_id={self.event_id!r}, sequence_no={self.sequence_no}, "
            f"event_type={self.event_type!r}, event_digest={self.event_digest!r})"
        )


class ContextItem:
    """A single classified piece of context with source traceability.

    Never persists hidden reasoning body — only artifact refs and summaries.
    """

    __slots__ = ("category", "content", "kind", "metadata", "source_refs")

    def __init__(
        self,
        category: ContextCategory,
        content: dict[str, Any],
        source_refs: tuple[SourceRef, ...] = (),
        kind: AuthoritativeKind | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.category = category
        self.kind = kind
        self.content = content
        self.source_refs = source_refs
        self.metadata = metadata or {}

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContextItem):
            return NotImplemented
        return (
            self.category == other.category
            and self.kind == other.kind
            and self.content == other.content
            and self.source_refs == other.source_refs
        )

    def __hash__(self) -> int:
        return hash((self.category, self.kind, tuple(sorted(self.content.items()))))

    def __repr__(self) -> str:
        return (
            f"ContextItem(category={self.category.value!r}, kind={self.kind}, "
            f"source_refs={len(self.source_refs)})"
        )


def classify_content(content: dict[str, Any]) -> ContextCategory:
    """Classify a raw content dict into its context category.

    Rules per S3 spec §3:
    - content with 'category' key uses that value
    - content with 'artifact_ref' and nothing else → recoverable
    - content with 'hidden_reasoning' key → opaque
    - content with 'summary' + 'source_event_ids' → conversational
    - everything else → authoritative
    """
    explicit = content.get("category")
    if explicit:
        try:
            return ContextCategory(explicit)
        except ValueError:
            pass

    if "hidden_reasoning" in content:
        return ContextCategory.OPAQUE

    if "artifact_ref" in content and len(content) == 1:
        return ContextCategory.RECOVERABLE

    if "summary" in content and "source_event_ids" in content:
        return ContextCategory.CONVERSATIONAL

    return ContextCategory.AUTHORITATIVE
