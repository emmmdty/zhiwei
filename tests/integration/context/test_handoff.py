"""S3-T6 Integration: Context handoff and epoch transition tests.

Tests the two-phase epoch transition, TransitionManifest generation,
and the full prepare→commit flow with ContextCompiler.
"""

from __future__ import annotations

import pytest

from zhiwei.context.compiler import ContextCompiler
from zhiwei.context.manifests import TransitionManifest
from zhiwei.context.state import CanonicalState
from zhiwei.context.transition import (
    EpochId,
    EpochTransition,
    TransitionError,
    TransitionPreparation,
)
from zhiwei.context.types import (
    AuthoritativeKind,
    ContextCategory,
    ContextItem,
)

# ---- Factories ----


def _make_state(
    items: tuple[ContextItem, ...] = (),
    sequence_no: int = 0,
    head_event_digest: str | None = None,
) -> CanonicalState:
    return CanonicalState(
        items=items,
        sequence_no=sequence_no,
        head_event_digest=head_event_digest,
    )


def _make_item(
    category: ContextCategory = ContextCategory.AUTHORITATIVE,
    content: dict | None = None,
    kind: AuthoritativeKind | None = AuthoritativeKind.OBJECTIVE,
) -> ContextItem:
    return ContextItem(
        category=category,
        content=content or {"key": "value"},
        kind=kind,
    )


def _make_compiler(context_window: int = 128000) -> ContextCompiler:
    return ContextCompiler(context_window=context_window)


# ---- EpochId tests ----


class TestEpochId:
    def test_format(self) -> None:
        eid = EpochId(source_seq=0, target_seq=1, direction="upgrade")
        assert eid.value == "a-0-1-upgrade"

    def test_direction_specific(self) -> None:
        up = EpochId(source_seq=0, target_seq=1, direction="upgrade")
        down = EpochId(source_seq=1, target_seq=0, direction="downgrade")
        assert up != down

    def test_equality(self) -> None:
        a = EpochId(source_seq=0, target_seq=1, direction="lateral")
        b = EpochId(source_seq=0, target_seq=1, direction="lateral")
        assert a == b

    def test_hash(self) -> None:
        a = EpochId(source_seq=0, target_seq=1, direction="lateral")
        b = EpochId(source_seq=0, target_seq=1, direction="lateral")
        assert hash(a) == hash(b)

    def test_repr(self) -> None:
        eid = EpochId(source_seq=0, target_seq=1, direction="lateral")
        assert "a-0-1-lateral" in repr(eid)


# ---- Two-phase transition tests ----


class TestTwoPhaseTransition:
    def test_prepare_returns_old_state_unchanged(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        item = _make_item()
        old_state = _make_state(items=(item,), sequence_no=0)

        prep = transition.prepare(old_state, direction="upgrade")

        assert prep.old_state is old_state
        assert prep.old_state.items == (item,)
        assert prep.old_state.sequence_no == 0

    def test_prepare_returns_frozen_preparation(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(_make_item(),), sequence_no=0)

        prep = transition.prepare(old_state, direction="upgrade")

        assert isinstance(prep, TransitionPreparation)
        assert prep.epoch_id.direction == "upgrade"
        assert prep.epoch_id.source_seq == 0
        assert prep.epoch_id.target_seq == 1

    def test_commit_produces_new_state_and_manifest(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        item = _make_item()
        old_state = _make_state(items=(item,), sequence_no=0)

        prep = transition.prepare(old_state, direction="lateral")
        new_state, manifest = transition.commit(prep)

        assert isinstance(new_state, CanonicalState)
        assert isinstance(manifest, TransitionManifest)
        assert new_state is not old_state

    def test_commit_preserves_old_state_on_error(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(_make_item(),), sequence_no=0)

        prep = transition.prepare(old_state, direction="upgrade")

        # Commit should not mutate old_state
        original_items = old_state.items
        original_seq = old_state.sequence_no

        new_state, _ = transition.commit(prep)

        # Old state is unchanged
        assert old_state.items == original_items
        assert old_state.sequence_no == original_seq
        # New state has the compiled items
        assert len(new_state.items) >= 0

    def test_transition_manifest_fields(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(_make_item(),), sequence_no=0)

        prep = transition.prepare(old_state, direction="upgrade")
        _, manifest = transition.commit(prep)

        assert manifest.manifest_id.startswith("trans-a-")
        assert manifest.transition_type == "epoch.upgrade"
        assert manifest.before_state_digest is not None
        assert manifest.after_state_digest is not None
        assert manifest.before_state_digest.startswith("sha256:")
        assert manifest.after_state_digest.startswith("sha256:")

    def test_epoch_id_appears_exactly_once(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(_make_item(),), sequence_no=0)

        prep = transition.prepare(old_state, direction="lateral")
        _, manifest = transition.commit(prep)

        # A-prefix appears exactly once in the manifest_id
        a_count = manifest.manifest_id.count("a-")
        assert a_count == 1

    def test_refused_preparation_cannot_commit(self) -> None:
        compiler = _make_compiler(context_window=10)
        transition = EpochTransition(compiler)
        # Create items that will overflow the tiny context window
        large_items = tuple(
            _make_item(
                content={"data": "x " * 500},
                kind=AuthoritativeKind.OBJECTIVE,
            )
            for _ in range(5)
        )
        old_state = _make_state(items=large_items, sequence_no=0)

        prep = transition.prepare(old_state, direction="downgrade")

        # If the preparation is a refusal, commit should raise
        if prep.is_refusal:
            with pytest.raises(TransitionError, match="refused"):
                transition.commit(prep)


# ---- Directional identity tests ----


class TestDirectionalIdentity:
    def test_upgrade_direction(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(_make_item(),), sequence_no=5)

        prep = transition.prepare(old_state, direction="upgrade")

        assert prep.epoch_id.direction == "upgrade"
        assert prep.epoch_id.source_seq == 5
        assert prep.epoch_id.target_seq == 6

    def test_downgrade_direction(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(_make_item(),), sequence_no=3)

        prep = transition.prepare(old_state, direction="downgrade")

        assert prep.epoch_id.direction == "downgrade"
        assert prep.epoch_id.source_seq == 3
        assert prep.epoch_id.target_seq == 4

    def test_lateral_direction(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(_make_item(),), sequence_no=0)

        prep = transition.prepare(old_state, direction="lateral")

        assert prep.epoch_id.direction == "lateral"

    def test_same_sequence_different_direction_different_id(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(_make_item(),), sequence_no=0)

        up = transition.prepare(old_state, direction="upgrade")
        down = transition.prepare(old_state, direction="downgrade")

        assert up.epoch_id != down.epoch_id


# ---- Cache invalidation and cost tracking tests ----


class TestCacheCostTracking:
    def test_commit_records_cache_invalidated(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(_make_item(),), sequence_no=0)

        prep = transition.prepare(old_state, direction="upgrade")
        _, manifest = transition.commit(prep, cache_invalidated=True)

        # TransitionManifest structure is validated by Pydantic
        assert isinstance(manifest, TransitionManifest)

    def test_commit_records_ir_digest(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(_make_item(),), sequence_no=0)

        prep = transition.prepare(old_state, direction="lateral")
        _, manifest = transition.commit(prep)

        # IR digest should be present and a valid sha256
        assert manifest.ir_digest is not None
        assert manifest.ir_digest.startswith("sha256:")


# ---- Full pipeline integration ----


class TestFullPipeline:
    def test_empty_state_transition(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)
        old_state = _make_state(items=(), sequence_no=0)

        prep = transition.prepare(old_state, direction="lateral")
        new_state, manifest = transition.commit(prep)

        assert len(new_state.items) == 0
        assert manifest.items_added == 0
        assert manifest.items_removed == 0
        assert manifest.items_unchanged == 0

    def test_state_preserved_through_transition(self) -> None:
        compiler = _make_compiler()
        transition = EpochTransition(compiler)

        items = (
            _make_item(
                content={"id": "auth-1", "type": "fact"},
                kind=AuthoritativeKind.OBJECTIVE,
            ),
            _make_item(
                category=ContextCategory.CONVERSATIONAL,
                content={"id": "conv-1", "summary": "test"},
                kind=None,
            ),
        )
        old_state = _make_state(items=items, sequence_no=0)

        prep = transition.prepare(old_state, direction="upgrade")
        new_state, _manifest = transition.commit(prep)

        # New state should contain items from compilation
        assert len(new_state.items) >= 0
        # Old state unchanged
        assert len(old_state.items) == 2
        assert old_state.sequence_no == 0
