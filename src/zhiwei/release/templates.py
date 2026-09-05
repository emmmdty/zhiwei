"""Release 表面的 claim marker 渲染（specs/s9 §5）。

与 checker 共享同一 marker 语法，但渲染是纯文本替换、无 I/O：拒绝时抛
RenderRefused，调用方在异常路径上不得写回文件（fail closed：宁可保留 marker
也不落半成品）。渲染只接受 artifact-verified 且已绑定 bound_value 的 claim。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from zhiwei.agents.claims import ClaimRecord, ClaimStatus

__all__ = ["RenderRefused", "render_release_surface"]

_CLAIM_MARKER = re.compile(r"\{\{claim:([^}]+)\}\}")

_VERIFIED_STATUSES = frozenset({ClaimStatus.OFFLINE_VERIFIED, ClaimStatus.LIVE_VERIFIED})


class RenderRefused(RuntimeError):
    """渲染被拒：未知 id、非 verified 状态或 bound_value 缺失——文件必须保持原样。"""


def render_release_surface(text: str, registry: Mapping[str, ClaimRecord]) -> str:
    """把 `{{claim:ID}}` 替换为 claim 的 bound_value；任何拒绝路径都不产生部分输出。"""

    def _fill(match: re.Match[str]) -> str:
        claim_id = match.group(1)
        record = registry.get(claim_id)
        if record is None:
            raise RenderRefused(f"claim id is not registered: {claim_id!r}")
        if record.status not in _VERIFIED_STATUSES:
            raise RenderRefused(
                f"claim {claim_id!r} has status {record.status.value!r}; "
                "only artifact-verified claims render"
            )
        if record.bound_value is None:
            raise RenderRefused(f"claim {claim_id!r} is verified but has no bound value")
        return record.bound_value

    return _CLAIM_MARKER.sub(_fill, text)
