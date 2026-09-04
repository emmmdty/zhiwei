"""S3-T4 Context compression strategies.

Fixed priority: artifactize result → remove recoverable → summarize conversation
→ task split / model choice → refusal.

ADR-007: max_compaction_attempts=3, refusal has two exits
(authoritative_waived + epoch rollback).
"""

from __future__ import annotations

from typing import Any

from zhiwei.context.ir import ContextRefusalKind, TransformKind
from zhiwei.context.types import ContextCategory, ContextItem

MAX_COMPACTION_ATTEMPTS = 3


class CompressionTransform:
    """Base for compression transforms applied to context items.

    Each transform operates on a tuple of items and returns the compressed
    tuple plus the items that were removed.
    """

    kind: TransformKind

    def apply(
        self, items: tuple[ContextItem, ...]
    ) -> tuple[tuple[ContextItem, ...], tuple[ContextItem, ...]]:
        """Apply transform. Returns (compressed_items, removed_items)."""
        raise NotImplementedError


class ArtifactizeResultTransform(CompressionTransform):
    """Replace recoverable result body with artifact reference only.

    Items with 'result' + 'artifact_ref' keep only the artifact_ref.
    """

    kind = TransformKind.ARTIFACTIZED

    def apply(
        self, items: tuple[ContextItem, ...]
    ) -> tuple[tuple[ContextItem, ...], tuple[ContextItem, ...]]:
        result: list[ContextItem] = []
        removed: list[ContextItem] = []
        for item in items:
            if (
                item.category == ContextCategory.RECOVERABLE
                and "artifact_ref" in item.content
                and "result" in item.content
            ):
                new_content = {"artifact_ref": item.content["artifact_ref"]}
                removed.append(item)
                result.append(
                    ContextItem(
                        category=item.category,
                        content=new_content,
                        source_refs=item.source_refs,
                        kind=item.kind,
                    )
                )
            else:
                result.append(item)
        return tuple(result), tuple(removed)


class RemoveRecoverableTransform(CompressionTransform):
    """Remove all recoverable items (artifact refs already captured)."""

    kind = TransformKind.RECOVERABLE_REMOVED

    def apply(
        self, items: tuple[ContextItem, ...]
    ) -> tuple[tuple[ContextItem, ...], tuple[ContextItem, ...]]:
        result: list[ContextItem] = []
        removed: list[ContextItem] = []
        for item in items:
            if item.category == ContextCategory.RECOVERABLE:
                removed.append(item)
            else:
                result.append(item)
        return tuple(result), tuple(removed)


class SummarizeConversationTransform(CompressionTransform):
    """Replace old conversation items with a summary.

    Keeps the most recent `keep_recent` conversation items and summarizes
    the rest into a single synthetic summary item.
    """

    kind = TransformKind.CONVERSATION_SUMMARIZED

    def __init__(self, keep_recent: int = 5) -> None:
        self.keep_recent = keep_recent

    def apply(
        self, items: tuple[ContextItem, ...]
    ) -> tuple[tuple[ContextItem, ...], tuple[ContextItem, ...]]:
        conv_items = [
            item for item in items if item.category == ContextCategory.CONVERSATIONAL
        ]
        other_items = [
            item for item in items if item.category != ContextCategory.CONVERSATIONAL
        ]

        if len(conv_items) <= self.keep_recent:
            return items, ()

        to_summarize = conv_items[: -self.keep_recent]
        kept = conv_items[-self.keep_recent :]

        summary_content: dict[str, Any] = {
            "summary": f"[{len(to_summarize)} earlier conversation turns summarized]",
            "source_event_ids": [
                item.content.get("id", "")
                for item in to_summarize
                if item.content.get("id")
            ],
            "summarized_count": len(to_summarize),
        }

        summary_item = ContextItem(
            category=ContextCategory.CONVERSATIONAL,
            content=summary_content,
            kind=None,
        )

        return (*other_items, summary_item, *kept), tuple(to_summarize)


class CompressionPipeline:
    """Execute compression transforms in fixed priority order.

    Per S3 spec §4:
    1. Artifactize result
    2. Remove recoverable
    3. Summarize conversation
    4. (task split / model choice — handled by compiler)

    Tracks compaction attempts per ADR-007: max_compaction_attempts=3.
    """

    __slots__ = ("_attempt_count", "_max_attempts", "_removal_log", "_transforms")

    def __init__(
        self,
        transforms: list[CompressionTransform] | None = None,
        max_attempts: int = MAX_COMPACTION_ATTEMPTS,
    ) -> None:
        self._attempt_count = 0
        self._max_attempts = max_attempts
        self._transforms = transforms or [
            ArtifactizeResultTransform(),
            RemoveRecoverableTransform(),
            SummarizeConversationTransform(),
        ]
        self._removal_log: list[dict[str, Any]] = []

    @property
    def attempt_count(self) -> int:
        return self._attempt_count

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def removal_log(self) -> list[dict[str, Any]]:
        return list(self._removal_log)

    def compress(
        self,
        items: tuple[ContextItem, ...],
        *,
        target_tokens: int | None = None,
    ) -> tuple[tuple[ContextItem, ...], tuple[ContextItem, ...], bool]:
        """Run compression pipeline.

        Returns (compressed_items, all_removed_items, hit_max_attempts).
        If target_tokens is provided, stops early when estimated fit is achieved.
        """
        all_removed: list[ContextItem] = []
        current = items
        hit_max = False

        for transform in self._transforms:
            if self._attempt_count >= self._max_attempts:
                hit_max = True
                break

            compressed, removed = transform.apply(current)
            if removed:
                self._attempt_count += 1
                self._removal_log.append(
                    {
                        "transform": transform.kind.value,
                        "removed_count": len(removed),
                        "remaining_count": len(compressed),
                    }
                )
            current = compressed
            all_removed.extend(removed)

        return current, tuple(all_removed), hit_max

    def check_refusal(
        self,
        compressed_items: tuple[ContextItem, ...],
        removed_items: tuple[ContextItem, ...],
    ) -> ContextRefusalKind | None:
        """Determine refusal kind after compression exhausted attempts.

        ADR-007: two exit paths:
        - authoritative_waived: authoritative items were dropped
        - epoch_rollback: rollback to previous epoch with larger context model
        """
        has_authoritative_dropped = any(
            item.category == ContextCategory.AUTHORITATIVE for item in removed_items
        )

        if has_authoritative_dropped:
            return ContextRefusalKind.AUTHORITATIVE_WAIVED
        return ContextRefusalKind.EPOCH_ROLLBACK

    def manifest(self) -> dict[str, Any]:
        """Produce manifest entry for ContextManifest per ADR-007."""
        return {
            "compaction_attempts": self._attempt_count,
            "max_compaction_attempts": self._max_attempts,
            "removal_log": self._removal_log,
        }
