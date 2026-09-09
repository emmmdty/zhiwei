"""S0-T2 RED：不透明标识符。

DATA_MODEL §1："外部 API 使用不透明 UUID/ULID，不使用自增 ID 暴露租户数量。"

这里把"不透明"落成一条可执行断言：**必须是 UUIDv4**。ULID 与 UUIDv7 都在前缀里编码毫秒
时间戳——对多租户系统这意味着任何拿到两个 id 的人都能推断出资源创建时间，进而推断出某个
组织的活动节奏和数据量。它们解决的是数据库索引局部性问题，不是"不透明"问题。
"""

from __future__ import annotations

import re
from uuid import UUID

import pytest

from zhiwei.contracts.identifiers import (
    IdentifierError,
    format_id,
    new_id,
    parse_id,
)

# --------------------------------------------------------------------------- 不透明性


def test_new_id_is_uuid4() -> None:
    assert new_id().version == 4


def test_ids_are_unique() -> None:
    assert len({new_id() for _ in range(1000)}) == 1000


def test_ids_are_not_monotonic() -> None:
    """连续生成的 id 不得单调——单调即泄漏创建顺序。

    64 个随机 UUID 恰好有序的概率是 1/64!，实际为零。
    """
    ids = [new_id() for _ in range(64)]
    assert ids != sorted(ids)


# --------------------------------------------------------------------------- 外部表示


def test_format_id_uses_prefix_and_hex() -> None:
    value = UUID("4f9a1c2b-3d4e-4f60-8a1b-2c3d4e5f6071")
    assert format_id("org", value) == "org_4f9a1c2b3d4e4f608a1b2c3d4e5f6071"


def test_format_id_output_matches_the_documented_shape() -> None:
    assert re.fullmatch(r"run_[0-9a-f]{32}", format_id("run", new_id()))


def test_parse_id_round_trips() -> None:
    value = new_id()
    assert parse_id("org", format_id("org", value)) == value


def test_parse_id_rejects_a_mismatched_prefix() -> None:
    """前缀是类型信息。把 workspace id 传进要 organization id 的地方必须炸，
    而不是解析成功后在几层之外变成一个越权查询。
    """
    text = format_id("workspace", new_id())
    with pytest.raises(IdentifierError):
        parse_id("org", text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "org_",
        "org_notahexstring",
        "org_4f9a1c2b3d4e4f608a1b2c3d4e5f60",  # 少两位
        "org_4f9a1c2b3d4e4f608a1b2c3d4e5f607100",  # 多两位
        "org4f9a1c2b3d4e4f608a1b2c3d4e5f6071",  # 缺分隔符
        "ORG_4f9a1c2b3d4e4f608a1b2c3d4e5f6071",
        "org_4F9A1C2B3D4E4F608A1B2C3D4E5F6071",  # 大写十六进制是第二种写法
    ],
)
def test_parse_id_rejects_malformed_input(text: str) -> None:
    with pytest.raises(IdentifierError):
        parse_id("org", text)


def test_parse_id_rejects_a_non_v4_uuid() -> None:
    """外部传入的 v1 UUID 含 MAC 地址与时间戳，不能被当作本系统的 id 接受。"""
    v1_like = "org_" + "0" * 12 + "1" + "0" * 19
    with pytest.raises(IdentifierError):
        parse_id("org", v1_like)


@pytest.mark.parametrize("prefix", ["", "Org", "or g", "org_", "1org", "org-x"])
def test_format_id_rejects_a_malformed_prefix(prefix: str) -> None:
    """前缀本身也要受约束，否则 `format_id("org_x", ...)` 会产生歧义的分隔。"""
    with pytest.raises(IdentifierError):
        format_id(prefix, new_id())


# --------------------------------------------------------------------------- 无全局状态


def test_new_id_does_not_depend_on_module_state() -> None:
    """没有可被重置的序列号/计数器——那正是自增 ID 的问题。"""
    import zhiwei.contracts.identifiers as module

    counters = [
        name
        for name, value in vars(module).items()
        if not name.startswith("__") and isinstance(value, (int, list, dict, set))
    ]
    assert counters == [], f"模块级可变状态: {counters}"
