"""S3-T4 RED: Token budget estimation and context fit tests."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from zhiwei.context.budget import (
    ContextFitCheck,
    count_by_category,
    estimate_tokens_content,
    estimate_tokens_items,
    estimate_tokens_text,
)
from zhiwei.context.ir import TokenEstimate
from zhiwei.context.types import ContextCategory, ContextItem, SourceRef

# ---- Helpers ----


def _make_item(
    item_id: str,
    category: ContextCategory = ContextCategory.AUTHORITATIVE,
    kind: str = "objective",
    **extra: object,
) -> ContextItem:
    content = {"id": item_id, "kind": kind, **extra}
    return ContextItem(category=category, content=content)


def _source_ref(event_id: str = "e1", seq: int = 1) -> SourceRef:
    return SourceRef(event_id, seq, "context.created", "sha256:abc")


# ---- Token estimation tests ----


class TestEstimateTokensText:
    def test_empty_string(self) -> None:
        assert estimate_tokens_text("") == 0

    def test_single_char(self) -> None:
        assert estimate_tokens_text("a") == 1

    def test_four_chars(self) -> None:
        assert estimate_tokens_text("abcd") == 1

    def test_five_chars(self) -> None:
        assert estimate_tokens_text("abcde") == 2

    def test_longer_text(self) -> None:
        text = "a" * 100
        assert estimate_tokens_text(text) == 25

    def test_whitespace(self) -> None:
        text = "   "
        assert estimate_tokens_text(text) == 1


class TestEstimateTokensContent:
    def test_empty_content(self) -> None:
        est = estimate_tokens_content({})
        assert est.count >= 0
        assert est.margin >= 0

    def test_simple_content(self) -> None:
        est = estimate_tokens_content({"id": "item-1", "name": "test"})
        assert est.count > 0
        assert est.margin > 0

    def test_margin_is_proportional(self) -> None:
        small = estimate_tokens_content({"a": "x"})
        large = estimate_tokens_content({"a": "x" * 1000})
        assert large.margin > small.margin

    def test_level_is_calibrated(self) -> None:
        from zhiwei.context.ir import TokenCountingLevel
        est = estimate_tokens_content({"key": "value"})
        assert est.level == TokenCountingLevel.CALIBRATED


class TestEstimateTokensItems:
    def test_empty_items(self) -> None:
        est = estimate_tokens_items(())
        assert est.count == 0
        assert est.margin == 0

    def test_single_item(self) -> None:
        items = (_make_item("i1"),)
        est = estimate_tokens_items(items)
        assert est.count > 0

    def test_multiple_items_aggregate(self) -> None:
        items = (_make_item("i1"), _make_item("i2"))
        est = estimate_tokens_items(items)
        single = estimate_tokens_items((_make_item("i1"),))
        assert est.count >= single.count


class TestCountByCategory:
    def test_empty(self) -> None:
        result = count_by_category(())
        assert result == {}

    def test_mixed_categories(self) -> None:
        items = (
            _make_item("a1", ContextCategory.AUTHORITATIVE),
            _make_item("c1", ContextCategory.CONVERSATIONAL),
            _make_item("r1", ContextCategory.RECOVERABLE),
        )
        result = count_by_category(items)
        assert ContextCategory.AUTHORITATIVE in result
        assert ContextCategory.CONVERSATIONAL in result
        assert ContextCategory.RECOVERABLE in result

    def test_authoritative_only(self) -> None:
        items = (_make_item("a1", ContextCategory.AUTHORITATIVE),)
        result = count_by_category(items)
        assert len(result) == 1
        assert ContextCategory.AUTHORITATIVE in result


# ---- ContextFitCheck tests ----


class TestContextFitCheck:
    def test_basic_fit(self) -> None:
        check = ContextFitCheck(context_window=10000)
        items = (_make_item("i1"),)
        fits, _estimate = check.fits(items)
        assert fits is True

    def test_over_budget(self) -> None:
        check = ContextFitCheck(context_window=5)
        big_content = {"id": "big", "data": "x" * 1000}
        items = (ContextItem(category=ContextCategory.AUTHORITATIVE, content=big_content),)
        fits, _estimate = check.fits(items)
        assert fits is False

    def test_system_reserve(self) -> None:
        check = ContextFitCheck(context_window=100, system_reserve=90)
        assert check.available_tokens == 10

    def test_output_reserve(self) -> None:
        check = ContextFitCheck(context_window=100, output_reserve=50)
        assert check.available_tokens == 50

    def test_both_reserves(self) -> None:
        check = ContextFitCheck(context_window=100, system_reserve=30, output_reserve=20)
        assert check.available_tokens == 50

    def test_invalid_window(self) -> None:
        import pytest
        with pytest.raises(ValueError):
            ContextFitCheck(context_window=0)

    def test_fits_after_compression(self) -> None:
        check = ContextFitCheck(context_window=10000)
        items = (_make_item("i1"),)
        fits, _estimate, dropped = check.fits_after_compression(items)
        assert fits is True
        assert dropped is False

    def test_fits_after_compression_with_authoritative_dropped(self) -> None:
        check = ContextFitCheck(context_window=10000)
        compressed = (_make_item("i2", ContextCategory.CONVERSATIONAL),)
        removed = (_make_item("i1", ContextCategory.AUTHORITATIVE),)
        _fits, _estimate, dropped = check.fits_after_compression(compressed, removed)
        assert dropped is True


# ---- Property tests ----


class TestPropertyBudget:
    @given(
        text=st.text(min_size=0, max_size=500),
    )
    @settings(max_examples=50)
    def test_token_estimate_non_negative(self, text: str) -> None:
        count = estimate_tokens_text(text)
        assert count >= 0

    @given(
        count=st.integers(min_value=0, max_value=10000),
    )
    @settings(max_examples=30)
    def test_token_estimate_upper_bound(self, count: int) -> None:
        from zhiwei.context.ir import TokenCountingLevel
        est = TokenEstimate(count=count, level=TokenCountingLevel.CALIBRATED, margin=count // 5)
        assert est.upper_bound == count + count // 5

    @given(
        window=st.integers(min_value=1, max_value=100000),
    )
    @settings(max_examples=30)
    def test_empty_items_always_fit(self, window: int) -> None:
        check = ContextFitCheck(context_window=window)
        fits, _ = check.fits(())
        assert fits is True
