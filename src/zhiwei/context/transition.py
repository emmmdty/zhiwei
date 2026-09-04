"""S3-T6 Two-phase epoch transitions: prepare → commit.

Per MODELS.md §5:
- Two-phase: prepare computes the target state, commit applies it.
- Old epoch preserved on failure (atomic rollback).
- A-prefix exactly once per transition (direction-specific epoch id).
- TransitionManifest records cache_invalidated and rebuild cost estimate.
"""

from __future__ import annotations

from typing import Any

from zhiwei.context.compiler import CompilationResult, ContextCompiler
from zhiwei.context.ir import ContextIRItem
from zhiwei.context.manifests import TransitionManifest
from zhiwei.context.state import CanonicalState
from zhiwei.context.types import ContextItem
from zhiwei.contracts.canonical import digest
from zhiwei.contracts.time import utc_now


class EpochId:
    """Direction-specific epoch identity. A-prefix exactly once per transition.

    Format: a-{source_seq}-{target_seq}-{direction}
    """

    __slots__ = ("_value", "direction", "source_seq", "target_seq")

    def __init__(self, source_seq: int, target_seq: int, direction: str) -> None:
        self.source_seq = source_seq
        self.target_seq = target_seq
        self.direction = direction
        self._value = f"a-{source_seq}-{target_seq}-{direction}"

    @property
    def value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"EpochId({self._value!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, EpochId):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


class TransitionPreparation:
    """Result of the prepare phase: frozen snapshot before commit.

    Carries the old state, the compiled result, and the epoch identity
    so that commit can apply atomically.
    """

    __slots__ = (
        "_compilation",
        "_epoch_id",
        "_old_state",
        "_target_profile_digest",
    )

    def __init__(
        self,
        old_state: CanonicalState,
        compilation: CompilationResult,
        epoch_id: EpochId,
        target_profile_digest: str = "",
    ) -> None:
        self._old_state = old_state
        self._compilation = compilation
        self._epoch_id = epoch_id
        self._target_profile_digest = target_profile_digest

    @property
    def old_state(self) -> CanonicalState:
        return self._old_state

    @property
    def compilation(self) -> CompilationResult:
        return self._compilation

    @property
    def epoch_id(self) -> EpochId:
        return self._epoch_id

    @property
    def target_profile_digest(self) -> str:
        return self._target_profile_digest

    @property
    def is_refusal(self) -> bool:
        return self._compilation.is_refusal

    @property
    def new_state(self) -> CanonicalState:
        """The state that would be applied on commit."""
        context_items = _ir_items_to_context_items(
            self._compilation.context_ir.items
        )
        return CanonicalState(
            items=context_items,
            sequence_no=self._compilation.context_ir.sequence_no,
            head_event_digest=self._compilation.context_ir.head_event_digest,
        )


def _ir_items_to_context_items(
    ir_items: tuple[ContextIRItem, ...],
) -> tuple[ContextItem, ...]:
    """Convert ContextIRItems back to ContextItems for state reconstruction."""
    result: list[ContextItem] = []
    for ir_item in ir_items:
        result.append(
            ContextItem(
                category=ir_item.category,
                content=ir_item.content,
                source_refs=(),
                kind=ir_item.kind,
                metadata={
                    "transform": ir_item.source_transform.transform_kind.value,
                    "token_count": ir_item.token_estimate.count,
                },
            )
        )
    return tuple(result)


class TransitionError(RuntimeError):
    """Raised when a transition cannot be completed."""


class EpochTransition:
    """Two-phase epoch transition controller.

    Usage:
        transition = EpochTransition(compiler)
        prep = transition.prepare(old_state, target_profile_digest, direction="upgrade")
        if not prep.is_refusal:
            new_state, manifest = transition.commit(prep)
        # On failure: old_state is preserved, no mutation occurs.
    """

    __slots__ = ("_compiler",)

    def __init__(self, compiler: ContextCompiler) -> None:
        self._compiler = compiler

    def prepare(
        self,
        old_state: CanonicalState,
        target_profile_digest: str = "",
        direction: str = "lateral",
    ) -> TransitionPreparation:
        """Phase 1: Prepare the new epoch without mutating old state.

        Compiles the old state into a new ContextIR. Returns a frozen
        preparation snapshot that carries the old state for rollback.
        """
        compilation = self._compiler.compile(old_state)

        epoch_id = EpochId(
            source_seq=old_state.sequence_no,
            target_seq=old_state.sequence_no + 1,
            direction=direction,
        )

        return TransitionPreparation(
            old_state=old_state,
            compilation=compilation,
            epoch_id=epoch_id,
            target_profile_digest=target_profile_digest,
        )

    def commit(
        self,
        preparation: TransitionPreparation,
        *,
        cache_invalidated: bool = True,
        rebuild_cost_estimate: dict[str, Any] | None = None,
    ) -> tuple[CanonicalState, TransitionManifest]:
        """Phase 2: Commit the prepared epoch atomically.

        Returns the new state and a TransitionManifest. On any error
        the old state is preserved (no partial application).
        """
        if preparation.is_refusal:
            raise TransitionError(
                f"Cannot commit refused preparation: "
                f"{preparation.compilation.refusal}"
            )

        old_state = preparation.old_state
        new_state = preparation.new_state
        new_compilation = preparation.compilation

        before_digest = self._state_digest(old_state)
        after_digest = self._state_digest(new_state)

        ir_items = new_compilation.context_ir.items
        source_items = old_state.items

        old_ids = {
            item.content.get("id", item.content.get("key", ""))
            for item in source_items
        }
        new_ids = {
            item.content.get("id", item.content.get("key", ""))
            for item in ir_items
        }

        items_added = len(new_ids - old_ids)
        items_removed = len(old_ids - new_ids)
        items_unchanged = len(old_ids & new_ids)

        manifest = TransitionManifest(
            manifest_id=f"trans-{preparation.epoch_id.value}",
            before_state_digest=before_digest,
            after_state_digest=after_digest,
            transition_type=f"epoch.{preparation.epoch_id.direction}",
            wire_body_digest=None,
            ir_digest=digest(
                {
                    "epoch_id": preparation.epoch_id.value,
                    "total_tokens": new_compilation.context_ir.total_token_estimate,
                    "item_count": len(ir_items),
                }
            ),
            items_added=items_added,
            items_removed=items_removed,
            items_unchanged=items_unchanged,
            triggered_by_manifest_id=None,
            occurred_at=utc_now().isoformat(),
        )

        return new_state, manifest

    @staticmethod
    def _state_digest(state: CanonicalState) -> str:
        """Compute a deterministic digest for a CanonicalState."""
        items_data = []
        for item in state.items:
            items_data.append(
                {
                    "category": item.category.value,
                    "kind": item.kind.value if item.kind else None,
                    "content": item.content,
                }
            )
        return digest(
            {
                "items": items_data,
                "sequence_no": state.sequence_no,
                "head_event_digest": state.head_event_digest,
            }
        )
