"""Release 表面的 claim marker 渲染（specs/s9 §5）。

与 checker 共享同一 marker 语法，但渲染是纯文本替换、无 I/O：拒绝时抛
RenderRefused，调用方在异常路径上不得写回文件（fail closed：宁可保留 marker
也不落半成品）。渲染只接受 artifact-verified 且已绑定 bound_value 的 claim。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date

from zhiwei.agents.claims import ClaimRecord, ClaimStatus
from zhiwei.release.checker import CLAIMS_END, CLAIMS_START

__all__ = ["RenderRefused", "render_claim_surface", "render_release_surface"]

_CLAIM_MARKER = re.compile(r"\{\{claim:([^}]+)\}\}")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

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


def render_claim_surface(text: str, registry: Mapping[str, ClaimRecord]) -> str:
    """渲染声明块并施加口径护栏（S11 followup-2 任务一）。

    在 render_release_surface 之上增加两条 fail-closed 规则：

    - 块内引用的全部 claim 的 scope（mode/model/environment/date）必须一致——
      块级口径标注只能描述单一口径，混写即拒绝；
    - 块内出现的每个 ISO-8601 日期都必须等于 scope date——README 口径注释与
      registry 漂移（照抄语义失效）时拒绝渲染，宁可保留 marker 也不发布过期口径。

    无声明块的文件原样返回（checker 只扫块内，渲染对块外零触碰）。
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_block = False
    block: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_block and stripped == _CLAIMS_START:
            in_block = True
            block = [line]
            continue
        if in_block:
            block.append(line)
            if stripped == _CLAIMS_END:
                in_block = False
                out.extend(_render_block(block, registry))
            continue
        out.append(line)
    if in_block:
        # 未闭合块：宁可拒绝也不猜块边界（checker 按延伸到文件末尾扫描，渲染
        # 侧无法等价重建，直接 fail closed）
        raise RenderRefused("claims block is not closed by a claims:end marker")
    return "".join(out)


_CLAIMS_START = CLAIMS_START
_CLAIMS_END = CLAIMS_END


def _render_block(block: list[str], registry: Mapping[str, ClaimRecord]) -> list[str]:
    block_text = "".join(block)
    claim_ids = list(dict.fromkeys(_CLAIM_MARKER.findall(block_text)))
    if not claim_ids:
        # 无 marker 的块没有渲染对象；数字支撑问题由 checker 警察，这里不越权
        return block
    records = []
    for claim_id in claim_ids:
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
        records.append(record)

    scope_keys = {
        (record.scope.mode, record.scope.model, record.scope.environment, record.scope.date)
        for record in records
    }
    if len(scope_keys) != 1:
        raise RenderRefused(
            "claims in one block must share a single scope "
            f"(mode/model/environment/date), got {sorted(scope_keys)}"
        )
    scope_date = records[0].scope.date
    try:
        date.fromisoformat(scope_date)
    except ValueError as exc:
        raise RenderRefused(f"claim scope date is not an ISO-8601 date: {scope_date!r}") from exc
    for match in _ISO_DATE.finditer(block_text):
        if match.group(0) != scope_date:
            raise RenderRefused(
                "claims block carries an ISO date that differs from the registry "
                f"scope date {scope_date}: {match.group(0)}"
            )
    return render_release_surface(block_text, registry).splitlines(keepends=True)
