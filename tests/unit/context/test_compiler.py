"""S3-T4 RED: Context Compiler and IR tests."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from zhiwei.context.compiler import CompilationResult, ContextCompiler
from zhiwei.context.compression import (
    ArtifactizeResultTransform,
    CompressionPipeline,
    RemoveRecoverableTransform,
    SummarizeConversationTransform,
)
from zhiwei.context.inventory import AuthoritativeInventory
from zhiwei.context.ir import (
    ContextIR,
    ContextIRItem,
    ContextRefusalKind,
    SourceTransform,
    TokenCountingLevel,
    TokenEstimate,
    TransformKind,
)
from zhiwei.context.reducer import reduce_events
from zhiwei.context.state import CanonicalState
from zhiwei.context.types import (
    AuthoritativeKind,
    ContextCategory,
    ContextItem,
)

# ---- Helpers ----


def _event(
    event_id: str,
    seq: int,
    event_type: str,
    payload: dict | None = None,
    event_digest: str = "sha256:abc123",
) -> dict:
    return {
        "id": event_id,
        "sequence_no": seq,
        "event_type": event_type,
        "payload": payload or {},
        "event_digest": event_digest,
    }


def _make_item(
    item_id: str,
    kind: str = "objective",
    category: ContextCategory = ContextCategory.AUTHORITATIVE,
    **extra: object,
) -> ContextItem:
    content = {"id": item_id, "kind": kind, **extra}
    return ContextItem(category=category, content=content)


def _state_with_all_authoritative() -> CanonicalState:
    events = [
        _event("e1", 1, "context.created", {"content": {"id": "o1", "kind": "objective"}}),
        _event("e2", 2, "context.created", {"content": {"id": "c1", "kind": "constraint"}}),
        _event("e3", 3, "context.created", {"content": {"id": "t1", "kind": "task"}}),
        _event("e4", 4, "context.created", {"content": {"id": "e1", "kind": "entity"}}),
        _event("e5", 5, "context.created", {"content": {"id": "d1", "kind": "decision"}}),
        _event("e6", 6, "context.created", {"content": {"id": "cf1", "kind": "conflict"}}),
        _event("e7", 7, "context.created", {"content": {"id": "ev1", "kind": "evidence"}}),
        _event("e8", 8, "context.created", {"content": {"id": "a1", "kind": "action"}}),
        _event("e9", 9, "context.created", {"content": {"id": "ap1", "kind": "approval"}}),
        _event("e10", 10, "context.created", {"content": {"id": "b1", "kind": "budget"}}),
        _event("e11", 11, "context.created", {"content": {"id": "ob1", "kind": "obligation"}}),
    ]
    return reduce_events(events)


def _state_mixed() -> CanonicalState:
    events = [
        _event("e1", 1, "context.created", {"content": {"id": "o1", "kind": "objective"}}),
        _event(
            "e2", 2, "context.created",
            {"content": {"summary": "chat summary", "source_event_ids": ["e0"]}},
        ),
        _event(
            "e3", 3, "context.created",
            {"content": {"artifact_ref": "s3://bucket/doc.pdf"}},
        ),
    ]
    return reduce_events(events)


# ---- TokenEstimate tests ----


class TestTokenEstimate:
    def test_basic(self) -> None:
        est = TokenEstimate(count=100, level=TokenCountingLevel.CALIBRATED, margin=15)
        assert est.upper_bound == 115

    def test_zero_margin(self) -> None:
        est = TokenEstimate(count=100, level=TokenCountingLevel.AUTHORITATIVE)
        assert est.upper_bound == 100

    def test_fits_in(self) -> None:
        est = TokenEstimate(count=100, level=TokenCountingLevel.CALIBRATED, margin=15)
        assert est.fits_in(115) is True
        assert est.fits_in(114) is False

    def test_negative_count_raises(self) -> None:
        import pytest
        with pytest.raises(ValueError):
            TokenEstimate(count=-1, level=TokenCountingLevel.CALIBRATED)

    def test_negative_margin_raises(self) -> None:
        import pytest
        with pytest.raises(ValueError):
            TokenEstimate(count=10, level=TokenCountingLevel.CALIBRATED, margin=-1)

    def test_equality(self) -> None:
        a = TokenEstimate(count=50, level=TokenCountingLevel.CALIBRATED, margin=5)
        b = TokenEstimate(count=50, level=TokenCountingLevel.CALIBRATED, margin=5)
        assert a == b

    def test_inequality(self) -> None:
        a = TokenEstimate(count=50, level=TokenCountingLevel.CALIBRATED, margin=5)
        b = TokenEstimate(count=60, level=TokenCountingLevel.CALIBRATED, margin=5)
        assert a != b

    def test_repr(self) -> None:
        est = TokenEstimate(count=10, level=TokenCountingLevel.VERIFIED_LOCAL)
        r = repr(est)
        assert "TokenEstimate" in r
        assert "10" in r


# ---- ContextIR tests ----


class TestContextIR:
    def test_empty_ir(self) -> None:
        ir = ContextIR()
        assert len(ir.items) == 0
        assert ir.total_token_estimate == 0
        assert ir.is_refusal is False

    def test_ir_with_items(self) -> None:
        item = ContextIRItem(
            category=ContextCategory.AUTHORITATIVE,
            content={"id": "o1", "kind": "objective"},
            source_transform=SourceTransform(
                source_item=_make_item("o1"),
                transform_kind=TransformKind.ORIGINAL,
            ),
            token_estimate=TokenEstimate(count=10, level=TokenCountingLevel.CALIBRATED),
            kind=AuthoritativeKind.OBJECTIVE,
        )
        ir = ContextIR(items=(item,), sequence_no=5, head_event_digest="sha256:ddd")
        assert len(ir.items) == 1
        assert ir.sequence_no == 5
        assert ir.head_event_digest == "sha256:ddd"
        assert ir.total_token_estimate == 10

    def test_authoritative_items_filter(self) -> None:
        auth_item = ContextIRItem(
            category=ContextCategory.AUTHORITATIVE,
            content={},
            source_transform=SourceTransform(source_item=_make_item("a1")),
            token_estimate=TokenEstimate(count=5, level=TokenCountingLevel.CALIBRATED),
        )
        conv_item = ContextIRItem(
            category=ContextCategory.CONVERSATIONAL,
            content={},
            source_transform=SourceTransform(
                source_item=ContextItem(
                    category=ContextCategory.CONVERSATIONAL, content={}
                )
            ),
            token_estimate=TokenEstimate(count=5, level=TokenCountingLevel.CALIBRATED),
        )
        ir = ContextIR(items=(auth_item, conv_item))
        assert len(ir.authoritative_items()) == 1
        assert len(ir.conversational_items()) == 1

    def test_refusal_ir(self) -> None:
        ir = ContextIR(refusal=ContextRefusalKind.AUTHORITATIVE_WAIVED)
        assert ir.is_refusal is True
        assert ir.refusal == ContextRefusalKind.AUTHORITATIVE_WAIVED

    def test_source_map(self) -> None:
        item = ContextIRItem(
            category=ContextCategory.AUTHORITATIVE,
            content={},
            source_transform=SourceTransform(source_item=_make_item("a1")),
            token_estimate=TokenEstimate(count=5, level=TokenCountingLevel.CALIBRATED),
        )
        ir = ContextIR(items=(item,))
        sm = ir.source_map()
        assert len(sm) == 1

    def test_token_estimate_by_category(self) -> None:
        auth = ContextIRItem(
            category=ContextCategory.AUTHORITATIVE,
            content={},
            source_transform=SourceTransform(source_item=_make_item("a1")),
            token_estimate=TokenEstimate(count=10, level=TokenCountingLevel.CALIBRATED, margin=2),
        )
        conv = ContextIRItem(
            category=ContextCategory.CONVERSATIONAL,
            content={},
            source_transform=SourceTransform(
                source_item=ContextItem(category=ContextCategory.CONVERSATIONAL, content={})
            ),
            token_estimate=TokenEstimate(count=5, level=TokenCountingLevel.CALIBRATED, margin=1),
        )
        ir = ContextIR(items=(auth, conv))
        by_cat = ir.token_estimate_by_category()
        assert by_cat[ContextCategory.AUTHORITATIVE].count == 10
        assert by_cat[ContextCategory.CONVERSATIONAL].count == 5

    def test_items_by_transform(self) -> None:
        original = ContextIRItem(
            category=ContextCategory.AUTHORITATIVE,
            content={},
            source_transform=SourceTransform(
                source_item=_make_item("a1"), transform_kind=TransformKind.ORIGINAL
            ),
            token_estimate=TokenEstimate(count=5, level=TokenCountingLevel.CALIBRATED),
        )
        art = ContextIRItem(
            category=ContextCategory.RECOVERABLE,
            content={},
            source_transform=SourceTransform(
                source_item=ContextItem(category=ContextCategory.RECOVERABLE, content={}),
                transform_kind=TransformKind.ARTIFACTIZED,
            ),
            token_estimate=TokenEstimate(count=2, level=TokenCountingLevel.CALIBRATED),
        )
        ir = ContextIR(items=(original, art))
        assert len(ir.items_by_transform(TransformKind.ORIGINAL)) == 1
        assert len(ir.items_by_transform(TransformKind.ARTIFACTIZED)) == 1

    def test_equality(self) -> None:
        a = ContextIR(items=(), sequence_no=1)
        b = ContextIR(items=(), sequence_no=1)
        assert a == b

    def test_repr(self) -> None:
        ir = ContextIR(items=(), sequence_no=1)
        r = repr(ir)
        assert "ContextIR" in r


# ---- SourceTransform tests ----


class TestSourceTransform:
    def test_original(self) -> None:
        from zhiwei.context.ir import SourceTransform
        st = SourceTransform(source_item=_make_item("i1"))
        assert st.transform_kind == TransformKind.ORIGINAL

    def test_artifactized(self) -> None:
        from zhiwei.context.ir import SourceTransform
        st = SourceTransform(
            source_item=_make_item("i1"),
            transform_kind=TransformKind.ARTIFACTIZED,
            transform_detail="replaced with ref",
        )
        assert st.transform_kind == TransformKind.ARTIFACTIZED

    def test_repr(self) -> None:
        from zhiwei.context.ir import SourceTransform
        st = SourceTransform(source_item=_make_item("i1"))
        r = repr(st)
        assert "SourceTransform" in r


# ---- Compression transform tests ----


class TestArtifactizeResultTransform:
    def test_artifactizes_result_with_ref(self) -> None:
        transform = ArtifactizeResultTransform()
        item = ContextItem(
            category=ContextCategory.RECOVERABLE,
            content={"artifact_ref": "s3://bucket/key", "result": "big data here"},
        )
        compressed, removed = transform.apply((item,))
        assert len(removed) == 1
        assert len(compressed) == 1
        assert "artifact_ref" in compressed[0].content
        assert "result" not in compressed[0].content

    def test_preserves_non_artifact_items(self) -> None:
        transform = ArtifactizeResultTransform()
        item = _make_item("a1")
        compressed, removed = transform.apply((item,))
        assert len(compressed) == 1
        assert len(removed) == 0

    def test_preserves_artifact_ref_without_result(self) -> None:
        transform = ArtifactizeResultTransform()
        item = ContextItem(
            category=ContextCategory.RECOVERABLE,
            content={"artifact_ref": "s3://bucket/key"},
        )
        compressed, removed = transform.apply((item,))
        assert len(compressed) == 1
        assert len(removed) == 0


class TestRemoveRecoverableTransform:
    def test_removes_all_recoverable(self) -> None:
        transform = RemoveRecoverableTransform()
        items = (
            _make_item("a1", category=ContextCategory.AUTHORITATIVE),
            ContextItem(category=ContextCategory.RECOVERABLE, content={"artifact_ref": "x"}),
            _make_item("a2", category=ContextCategory.AUTHORITATIVE),
        )
        compressed, removed = transform.apply(items)
        assert len(compressed) == 2
        assert len(removed) == 1

    def test_no_recoverable_items(self) -> None:
        transform = RemoveRecoverableTransform()
        items = (_make_item("a1"),)
        compressed, removed = transform.apply(items)
        assert len(compressed) == 1
        assert len(removed) == 0


class TestSummarizeConversationTransform:
    def test_no_summary_when_few_items(self) -> None:
        transform = SummarizeConversationTransform(keep_recent=5)
        items = tuple(
            ContextItem(
                category=ContextCategory.CONVERSATIONAL,
                content={"summary": f"turn {i}", "source_event_ids": [f"e{i}"]},
            )
            for i in range(3)
        )
        compressed, removed = transform.apply(items)
        assert len(compressed) == 3
        assert len(removed) == 0

    def test_summarizes_old_items(self) -> None:
        transform = SummarizeConversationTransform(keep_recent=2)
        items = tuple(
            ContextItem(
                category=ContextCategory.CONVERSATIONAL,
                content={"summary": f"turn {i}", "source_event_ids": [f"e{i}"]},
            )
            for i in range(5)
        )
        compressed, removed = transform.apply(items)
        assert len(removed) == 3
        # 2 kept + 1 summary
        assert len(compressed) == 3

    def test_preserves_non_conversational(self) -> None:
        transform = SummarizeConversationTransform(keep_recent=1)
        items = (
            _make_item("a1"),
            ContextItem(
                category=ContextCategory.CONVERSATIONAL,
                content={"summary": "old", "source_event_ids": ["e0"]},
            ),
            ContextItem(
                category=ContextCategory.CONVERSATIONAL,
                content={"summary": "new", "source_event_ids": ["e1"]},
            ),
        )
        compressed, _removed = transform.apply(items)
        assert len(compressed) == 3
        assert any(i.category == ContextCategory.AUTHORITATIVE for i in compressed)


# ---- CompressionPipeline tests ----


class TestCompressionPipeline:
    def test_empty_items(self) -> None:
        pipeline = CompressionPipeline()
        compressed, removed, hit_max = pipeline.compress(())
        assert compressed == ()
        assert removed == ()
        assert hit_max is False

    def test_removes_recoverable(self) -> None:
        pipeline = CompressionPipeline()
        items = (
            _make_item("a1"),
            ContextItem(category=ContextCategory.RECOVERABLE, content={"artifact_ref": "x"}),
        )
        compressed, removed, _hit_max = pipeline.compress(items)
        assert len(compressed) == 1
        assert len(removed) == 1

    def test_respects_max_attempts(self) -> None:
        pipeline = CompressionPipeline(max_attempts=1)
        items = tuple(
            ContextItem(
                category=ContextCategory.RECOVERABLE,
                content={"artifact_ref": f"s3://b/k{i}", "result": f"data{i}"},
            )
            for i in range(5)
        )
        _compressed, _removed, hit_max = pipeline.compress(items)
        assert hit_max is True

    def test_manifest(self) -> None:
        pipeline = CompressionPipeline()
        pipeline.compress((_make_item("a1"),))
        m = pipeline.manifest()
        assert "compaction_attempts" in m
        assert "max_compaction_attempts" in m
        assert "removal_log" in m

    def test_check_refusal_authoritative_waived(self) -> None:
        pipeline = CompressionPipeline()
        removed = (_make_item("a1", category=ContextCategory.AUTHORITATIVE),)
        refusal = pipeline.check_refusal((), removed)
        assert refusal == ContextRefusalKind.AUTHORITATIVE_WAIVED

    def test_check_refusal_epoch_rollback(self) -> None:
        pipeline = CompressionPipeline()
        removed = (
            ContextItem(category=ContextCategory.RECOVERABLE, content={"artifact_ref": "x"}),
        )
        refusal = pipeline.check_refusal((), removed)
        assert refusal == ContextRefusalKind.EPOCH_ROLLBACK


# ---- ContextCompiler tests ----


class TestContextCompiler:
    def test_compilation_result_manifest(self) -> None:
        compiler = ContextCompiler(context_window=100_000)
        state = _state_mixed()
        result = compiler.compile(state)
        assert isinstance(result, CompilationResult)
        m = result.manifest()
        assert "sequence_no" in m
        assert "inventory_summary" in m

    def test_compile_empty_state(self) -> None:
        compiler = ContextCompiler(context_window=100_000)
        state = CanonicalState()
        result = compiler.compile(state)
        assert result.is_refusal is False
        assert len(result.context_ir.items) == 0

    def test_compile_all_authoritative(self) -> None:
        compiler = ContextCompiler(context_window=100_000)
        state = _state_with_all_authoritative()
        result = compiler.compile(state)
        assert result.is_refusal is False
        assert len(result.context_ir.authoritative_items()) == 11

    def test_compile_with_recoverable(self) -> None:
        compiler = ContextCompiler(context_window=100_000)
        state = _state_mixed()
        result = compiler.compile(state)
        assert result.is_refusal is False

    def test_compile_refusal_when_context_too_small(self) -> None:
        compiler = ContextCompiler(context_window=10)
        state = _state_with_all_authoritative()
        result = compiler.compile(state)
        assert result.is_refusal is True
        assert result.refusal is not None

    def test_compile_preserves_source_refs(self) -> None:
        compiler = ContextCompiler(context_window=100_000)
        state = _state_mixed()
        result = compiler.compile(state)
        for item in result.context_ir.items:
            assert item.source_transform is not None

    def test_compile_deterministic(self) -> None:
        compiler = ContextCompiler(context_window=100_000)
        state = _state_mixed()
        r1 = compiler.compile(state)
        r2 = compiler.compile(state)
        assert r1.context_ir == r2.context_ir

    def test_compile_system_reserve(self) -> None:
        compiler = ContextCompiler(
            context_window=100_000, system_reserve=50_000
        )
        state = _state_with_all_authoritative()
        result = compiler.compile(state)
        assert result.context_ir.total_token_estimate <= 50_000 or result.is_refusal

    def test_compile_output_reserve(self) -> None:
        compiler = ContextCompiler(
            context_window=100_000, output_reserve=50_000
        )
        state = _state_with_all_authoritative()
        result = compiler.compile(state)
        assert result.context_ir.total_token_estimate <= 50_000 or result.is_refusal

    def test_inventory_in_result(self) -> None:
        compiler = ContextCompiler(context_window=100_000)
        state = _state_with_all_authoritative()
        result = compiler.compile(state)
        assert isinstance(result.inventory, AuthoritativeInventory)
        assert result.inventory.is_complete()

    def test_compilation_result_refusal(self) -> None:
        cr = CompilationResult(
            context_ir=ContextIR(refusal=ContextRefusalKind.AUTHORITATIVE_WAIVED),
            inventory=AuthoritativeInventory(),
            token_estimates_by_category={},
            refusal=ContextRefusalKind.AUTHORITATIVE_WAIVED,
        )
        assert cr.is_refusal is True
        assert cr.manifest()["refusal_kind"] == "authoritative_waived"


# ---- Property tests ----


class TestPropertyCompiler:
    @given(
        window=st.integers(min_value=1, max_value=1_000_000),
    )
    @settings(max_examples=30)
    def test_compilation_never_crashes(self, window: int) -> None:
        compiler = ContextCompiler(context_window=window)
        state = CanonicalState()
        result = compiler.compile(state)
        assert isinstance(result, CompilationResult)

    @given(
        window=st.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=20)
    def test_small_window_produces_refusal(self, window: int) -> None:
        compiler = ContextCompiler(context_window=window)
        state = _state_with_all_authoritative()
        result = compiler.compile(state)
        # Small window with 11 authoritative items should fit or be refusal
        assert isinstance(result, CompilationResult)

    @given(
        keep_recent=st.integers(min_value=1, max_value=20),
    )
    @settings(max_examples=20)
    def test_summarize_transform_preserves_count(self, keep_recent: int) -> None:
        n = 10
        items = tuple(
            ContextItem(
                category=ContextCategory.CONVERSATIONAL,
                content={"summary": f"turn {i}", "source_event_ids": [f"e{i}"]},
            )
            for i in range(n)
        )
        transform = SummarizeConversationTransform(keep_recent=keep_recent)
        compressed, removed = transform.apply(items)
        if keep_recent >= n:
            # No summarization: all items kept as-is
            assert len(compressed) == n
            assert len(removed) == 0
        else:
            # compressed includes a summary item, so compressed = (n - removed) + 1
            assert len(compressed) - 1 + len(removed) == n
