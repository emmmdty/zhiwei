"""S0-T2 RED：canonical JSON 与 digest。

全项目的 digest 都建立在这一层上——ContextManifest 的 wire digest、Evidence 的 result digest、
artifact manifest、canonical event 链。这里错一个字节，下游所有"可复算"声明同时失效。

三条不变量：

1. **字段顺序无关**：同一份数据无论以什么插入顺序构造，字节输出必须一致。
2. **Unicode 规范等价的文本产生同一个 digest**：`rfc8785` 本身不做 NFC 归一（实测 NFD 的
   "é" 序列化为 `e\\xcc\\x81`，NFC 的序列化为 `\\xc3\\xa9`），所以归一必须由本层补上。
   否则同一段中文/带重音文本从不同来源进来会得到两个 digest，"内容寻址"直接失效。
3. **不能表示的东西一律拒绝，不做静默降级**：超出 IEEE-754 安全区的整数、非有限 float、
   bytes、Decimal、naive datetime——全部抛错并指向对应的显式编码函数。
"""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from zhiwei.contracts.canonical import (
    CanonicalizationError,
    canonical_json,
    digest,
    digest_bytes,
    encode_bytes,
    encode_datetime,
    encode_decimal,
    encode_float,
    encode_integer,
    encode_text,
)

# JCS 的数字域就是 IEEE-754 双精度的安全整数区；超出的整数必须走 encode_integer。
SAFE_INT_MAX = 2**53 - 1


# --------------------------------------------------------------------------- 字段顺序无关


def test_key_order_does_not_affect_output() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_nested_key_order_does_not_affect_output() -> None:
    left = {"z": [3, {"y": 1, "x": 2}], "a": {"n": None}}
    right = {"a": {"n": None}, "z": [3, {"x": 2, "y": 1}]}
    assert canonical_json(left) == canonical_json(right)


def test_list_order_is_significant() -> None:
    """数组是有序的——顺序无关只适用于对象的键。"""
    assert canonical_json([1, 2]) != canonical_json([2, 1])


# --------------------------------------------------------------------------- Unicode NFC


@pytest.mark.parametrize(
    ("nfc", "nfd"),
    [
        ("é", "é"),
        ("ñ", "ñ"),
        ("가", "가"),
        ("Ω", "Ω"),
    ],
)
def test_canonically_equivalent_text_produces_one_digest(nfc: str, nfd: str) -> None:
    """规范等价的两种写法必须落到同一个 digest。"""
    assert unicodedata.normalize("NFC", nfd) == unicodedata.normalize("NFC", nfc)
    assert digest({"t": nfc}) == digest({"t": nfd})


def test_object_keys_are_also_nfc_normalized() -> None:
    """键和值走同一套归一——只归一值会留下一条绕过路径。"""
    assert canonical_json({"é": 1}) == canonical_json({"é": 1})


def test_nfc_key_collision_is_rejected() -> None:
    """归一后相同的两个原始键不能靠覆盖其中一个来“解决”。"""
    with pytest.raises(CanonicalizationError):
        canonical_json({"é": 1, "é": 2})


def test_nfc_normalization_survives_nesting() -> None:
    assert digest({"a": [{"é": ["é"]}]}) == digest({"a": [{"é": ["é"]}]})


def test_encode_text_returns_nfc() -> None:
    assert encode_text("é") == "é"
    assert unicodedata.is_normalized("NFC", encode_text("가"))


# --------------------------------------------------------------------------- digest 形状


def test_digest_is_prefixed_sha256_hex() -> None:
    value = digest({"a": 1})
    algorithm, _, hexdigest = value.partition(":")
    assert algorithm == "sha256"
    assert len(hexdigest) == 64
    assert set(hexdigest) <= set("0123456789abcdef")


def test_digest_matches_sha256_of_canonical_bytes() -> None:
    """digest 必须就是 canonical_json 输出的 SHA-256，没有额外的盐或包装。"""
    payload = {"a": 1, "b": ["x", None, True]}
    expected = "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()
    assert digest(payload) == expected


def test_digest_bytes_hashes_raw_input() -> None:
    assert digest_bytes(b"abc") == "sha256:" + hashlib.sha256(b"abc").hexdigest()


def test_digest_of_empty_object_is_stable() -> None:
    """对空对象的 digest 钉死——它是"空 Run 也能封存"的基线。"""
    assert canonical_json({}) == b"{}"
    assert digest({}) == "sha256:" + hashlib.sha256(b"{}").hexdigest()


# --------------------------------------------------------------------------- 拒绝不可表示的输入


def test_integer_beyond_safe_domain_is_rejected() -> None:
    """超出安全整数区不得静默变成 float——那是无声的精度损失。"""
    with pytest.raises(CanonicalizationError) as exc:
        canonical_json({"n": SAFE_INT_MAX + 1})
    assert "encode_integer" in str(exc.value), "错误信息必须指出正确的替代路径"


def test_safe_domain_boundary_is_accepted() -> None:
    assert canonical_json({"n": SAFE_INT_MAX}) == b'{"n":9007199254740991}'
    assert canonical_json({"n": -SAFE_INT_MAX}) == b'{"n":-9007199254740991}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_float_is_rejected(value: float) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json({"f": value})


@pytest.mark.parametrize(
    "value",
    [b"bytes", Decimal("1.5"), {1, 2}, datetime(2026, 8, 13, 12, 0, 0), object()],
)
def test_unsupported_types_are_rejected(value: Any) -> None:
    """不认识的类型一律拒绝，绝不 str() 兜底。

    `str()` 兜底会让 `Decimal("1.50")` 和 `"1.50"` 撞成同一个 digest，也会让对象的内存地址
    进入 digest——两者都是灾难。
    """
    with pytest.raises(CanonicalizationError):
        canonical_json({"v": value})


def test_aware_datetime_is_also_rejected_by_canonical_json() -> None:
    """带时区的 datetime 同样要走 encode_datetime，不在这层隐式转换。

    隐式转换意味着调用方无从知道用的是哪种格式；显式一层可以被 grep、被 review。
    """
    with pytest.raises(CanonicalizationError) as exc:
        canonical_json({"t": datetime(2026, 8, 13, 12, 0, tzinfo=UTC)})
    assert "encode_datetime" in str(exc.value)


# --------------------------------------------------------------------------- 显式值编码（DATA_MODEL §8）


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0"), (-1, "-1"), (10**30, "1" + "0" * 30), (-(2**64), "-18446744073709551616")],
)
def test_encode_integer_is_exact_at_arbitrary_precision(value: int, expected: str) -> None:
    assert encode_integer(value) == expected


def test_encode_integer_rejects_bool() -> None:
    """Python 里 bool 是 int 的子类；把 True 编码成 "1" 会让布尔与整数在 digest 上无法区分。"""
    with pytest.raises(CanonicalizationError):
        encode_integer(True)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1.50", "1.50"), ("1.5", "1.5"), ("0", "0"), ("-0.000", "-0.000"), ("1E+3", "1000")],
)
def test_encode_decimal_preserves_scale_and_expands_exponent(raw: str, expected: str) -> None:
    """scale 是有效数字信息（1.50 与 1.5 精度不同），必须保留；但指数记法要展开成唯一形式。"""
    assert encode_decimal(Decimal(raw)) == expected


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_encode_decimal_rejects_non_finite(raw: str) -> None:
    with pytest.raises(CanonicalizationError):
        encode_decimal(Decimal(raw))


def test_encode_float_uses_ieee754_bits() -> None:
    """DATA_MODEL §8：float 编码为 binary64 bits，而不是十进制串。

    十进制串会把 0.1 编码成一个并不等于它实际位模式的值，也无法区分 0.0 与 -0.0。
    """
    assert encode_float(1.0) == "3ff0000000000000"
    assert encode_float(0.0) == "0000000000000000"
    assert encode_float(-0.0) == "8000000000000000"


def test_encode_float_distinguishes_positive_and_negative_zero() -> None:
    assert encode_float(0.0) != encode_float(-0.0)


def test_encode_float_does_not_collide_with_integer_encoding() -> None:
    """canonical_json 里 1.0 和 1 都序列化成 `1`；显式编码层必须能区分。"""
    assert encode_float(1.0) != encode_integer(1)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_encode_float_rejects_non_finite(value: float) -> None:
    with pytest.raises(CanonicalizationError):
        encode_float(value)


def test_encode_bytes_is_unpadded_base64url() -> None:
    assert encode_bytes(b"") == ""
    assert encode_bytes(b"\xff\xfe") == "__4"
    assert "=" not in encode_bytes(b"abcd")
    assert "+" not in encode_bytes(b"\xfb\xff") and "/" not in encode_bytes(b"\xfb\xff")


def test_encode_datetime_normalizes_to_utc() -> None:
    from datetime import timedelta, timezone

    shanghai = timezone(timedelta(hours=8))
    assert encode_datetime(datetime(2026, 8, 13, 20, 0, 0, tzinfo=shanghai)) == encode_datetime(
        datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
    )


def test_encode_datetime_rejects_naive() -> None:
    """naive datetime 被当作 UTC 是最典型的静默数据损坏。"""
    with pytest.raises(CanonicalizationError):
        encode_datetime(datetime(2026, 8, 13, 12, 0, 0))


# --------------------------------------------------------------------------- property


_json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-SAFE_INT_MAX, max_value=SAFE_INT_MAX)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text()
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: st.lists(children, max_size=4)
    | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=12,
)


@given(_json_values)
@settings(max_examples=200, deadline=None)
def test_canonical_json_is_deterministic(value: Any) -> None:
    assert canonical_json(value) == canonical_json(value)


@given(st.dictionaries(st.text(max_size=8), _json_scalars, max_size=6))
@settings(max_examples=200, deadline=None)
def test_shuffled_construction_yields_identical_bytes(mapping: dict[str, Any]) -> None:
    reversed_mapping = dict(reversed(list(mapping.items())))
    assert canonical_json(mapping) == canonical_json(reversed_mapping)


@given(st.text())
@settings(max_examples=200, deadline=None)
def test_encode_text_is_idempotent(value: str) -> None:
    once = encode_text(value)
    assert encode_text(once) == once


@given(st.binary(max_size=64))
@settings(max_examples=200, deadline=None)
def test_encode_bytes_round_trips(value: bytes) -> None:
    import base64

    encoded = encode_bytes(value)
    padding = "=" * (-len(encoded) % 4)
    assert base64.urlsafe_b64decode(encoded + padding) == value


@given(st.integers())
@settings(max_examples=200, deadline=None)
def test_encode_integer_round_trips(value: int) -> None:
    assert int(encode_integer(value)) == value
