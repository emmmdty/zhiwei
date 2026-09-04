"""S3-T4 Context Compiler: 8-step pipeline from canonical projection to wire-ready IR.

Per MODELS.md §4:
1. Load canonical head (from reducer)
2. Build authoritative inventory
3. Classify content priority (authoritative > conversational > recoverable > opaque)
4. Estimate tokens per category (3-level counting)
5. Apply compression (artifactize → remove recoverable → summarize → task split)
6. Build ContextIR with source/transform map
7. Pre-send validation (all authoritative present or refusal)
8. Serialize for target wire protocol
"""

from __future__ import annotations

from typing import Any

from zhiwei.context.budget import (
    ContextFitCheck,
    count_by_category,
    estimate_tokens_items,
)
from zhiwei.context.compression import CompressionPipeline
from zhiwei.context.inventory import AuthoritativeInventory
from zhiwei.context.ir import (
    ContextIR,
    ContextIRItem,
    ContextRefusalKind,
    SourceTransform,
    TokenEstimate,
    TransformKind,
)
from zhiwei.context.state import CanonicalState
from zhiwei.context.types import (
    ContextCategory,
    ContextItem,
)


class CompilationResult:
    """Result of the Context Compiler pipeline."""

    __slots__ = (
        "context_ir",
        "inventory",
        "refusal",
        "removal_log",
        "token_estimates_by_category",
    )

    def __init__(
        self,
        context_ir: ContextIR,
        inventory: AuthoritativeInventory,
        token_estimates_by_category: dict[ContextCategory, TokenEstimate],
        refusal: ContextRefusalKind | None = None,
        removal_log: list[dict[str, Any]] | None = None,
    ) -> None:
        self.context_ir = context_ir
        self.inventory = inventory
        self.token_estimates_by_category = token_estimates_by_category
        self.refusal = refusal
        self.removal_log = removal_log or []

    @property
    def is_refusal(self) -> bool:
        return self.refusal is not None

    def manifest(self) -> dict[str, Any]:
        """Produce a ContextManifest entry."""
        return {
            "sequence_no": self.context_ir.sequence_no,
            "head_event_digest": self.context_ir.head_event_digest,
            "total_tokens": self.context_ir.total_token_estimate,
            "is_refusal": self.is_refusal,
            "refusal_kind": self.refusal.value if self.refusal else None,
            "inventory_summary": self.inventory.summary(),
            "token_estimates_by_category": {
                cat.value: {"count": est.count, "margin": est.margin}
                for cat, est in self.token_estimates_by_category.items()
            },
            "removal_log": self.removal_log,
            "item_count": len(self.context_ir.items),
        }


class ContextCompiler:
    """8-step Context Compiler pipeline.

    Takes a CanonicalState and produces a ContextIR (or refusal).
    """

    __slots__ = (
        "_compression",
        "_context_window",
        "_fit_check",
        "_output_reserve",
        "_system_reserve",
    )

    def __init__(
        self,
        context_window: int,
        system_reserve: int = 0,
        output_reserve: int = 0,
        compression: CompressionPipeline | None = None,
    ) -> None:
        if context_window <= 0:
            raise ValueError("context_window must be positive")
        self._context_window = context_window
        self._system_reserve = system_reserve
        self._output_reserve = output_reserve
        self._fit_check = ContextFitCheck(
            context_window=context_window,
            system_reserve=system_reserve,
            output_reserve=output_reserve,
        )
        self._compression = compression or CompressionPipeline()

    def compile(
        self,
        state: CanonicalState,
    ) -> CompilationResult:
        """Execute the 8-step compilation pipeline.

        Steps per MODELS.md §4:
        1. Load canonical head
        2. Build authoritative inventory
        3. Classify content priority
        4. Estimate tokens per category
        5. Apply compression
        6. Build ContextIR
        7. Pre-send validation
        8. (serialize — deferred to transport)
        """
        # Step 1: Load canonical head
        items = state.items

        # Step 2: Build authoritative inventory
        inventory = AuthoritativeInventory.from_state(state)

        # Step 3: Classify content priority
        # Items are already classified by the reducer via ContextCategory.
        # Priority order: authoritative > conversational > recoverable > opaque

        # Step 4: Estimate tokens per category (3-level counting)
        token_estimates = count_by_category(items)

        # Step 5: Apply compression
        fits, _pre_compression_estimate = self._fit_check.fits(items)

        all_removed: tuple[ContextItem, ...] = ()
        removal_log: list[dict[str, Any]] = []
        if not fits:
            compressed, removed, _hit_max = self._compression.compress(items)
            all_removed = removed
            removal_log = self._compression.removal_log
            items = compressed

            # Re-check fit after compression
            fits, _post_estimate = self._fit_check.fits(items)
            if not fits:
                # Step 7 (pre-check): authoritative check
                refusal = self._determine_refusal(items, all_removed)
                ir = self._build_ir(
                    items, state, refusal=refusal
                )
                return CompilationResult(
                    context_ir=ir,
                    inventory=inventory,
                    token_estimates_by_category=token_estimates,
                    refusal=refusal,
                    removal_log=removal_log,
                )

        # Step 6: Build ContextIR with source/transform map
        ir = self._build_ir(items, state)

        # Step 7: Pre-send validation — all authoritative present or refusal
        refusal = self._validate_authoritative(ir, inventory)

        if refusal is not None:
            ir = self._build_ir(items, state, refusal=refusal)

        return CompilationResult(
            context_ir=ir,
            inventory=inventory,
            token_estimates_by_category=token_estimates,
            refusal=refusal,
            removal_log=removal_log,
        )

    def _build_ir(
        self,
        items: tuple[ContextItem, ...],
        state: CanonicalState,
        refusal: ContextRefusalKind | None = None,
    ) -> ContextIR:
        """Step 6: Build ContextIR from items with source/transform map."""
        ir_items: list[ContextIRItem] = []
        for item in items:
            token_est = estimate_tokens_items((item,))
            source_transform = SourceTransform(
                source_item=item,
                transform_kind=TransformKind.ORIGINAL,
            )
            ir_items.append(
                ContextIRItem(
                    category=item.category,
                    content=item.content,
                    source_transform=source_transform,
                    token_estimate=token_est,
                    kind=item.kind,
                )
            )

        return ContextIR(
            items=tuple(ir_items),
            sequence_no=state.sequence_no,
            head_event_digest=state.head_event_digest,
            refusal=refusal,
        )

    def _validate_authoritative(
        self,
        ir: ContextIR,
        inventory: AuthoritativeInventory,
    ) -> ContextRefusalKind | None:
        """Step 7: Pre-send validation.

        All authoritative content from the inventory must be present in the IR.
        If any authoritative items are missing, return refusal.
        """
        ir_authoritative_ids: set[str] = set()
        for item in ir.authoritative_items():
            item_id = item.content.get("id", "")
            if item_id:
                ir_authoritative_ids.add(item_id)

        for _kind, entry in inventory.entries().items():
            for item_id in entry.item_ids:
                if item_id not in ir_authoritative_ids:
                    return ContextRefusalKind.AUTHORITATIVE_WAIVED

        return None

    def _determine_refusal(
        self,
        compressed_items: tuple[ContextItem, ...],
        removed_items: tuple[ContextItem, ...],
    ) -> ContextRefusalKind:
        """Determine refusal kind after compression."""
        refusal = self._compression.check_refusal(compressed_items, removed_items)
        return refusal or ContextRefusalKind.EPOCH_ROLLBACK
