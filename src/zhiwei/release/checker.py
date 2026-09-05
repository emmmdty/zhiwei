"""S9 strict release checker（specs/s9 §5，冻结契约见 test_claim_scan_frozen.py）。

release 表面（README/docs/demo）声明块内的每个数字都必须经由 `{{claim:ID}}`
marker 绑定 Claim Registry 中 artifact-verified 的 claim。fail closed 语义：

- 只有 `<!-- claims:start -->` / `<!-- claims:end -->` 围成的声明块受扫描；
  未闭合的块按「延伸到文件末尾」处理——宁可多扫不漏扫；
- 声明块内的数字只有紧邻（仅空白间隔）verified claim marker 且数值与该 claim
  的 bound_value 逐字符一致（whitespace 归一后）才算被支撑；紧邻 verified
  marker 但数值不符（或 claim 未绑定值）是 UNSUPPORTED_NUMBER——伪造数字不能
  借「贴着已验证 claim」逃逸；
- marker 本身无论解析成败都被独立校验（未知 id / 非 verified 状态都会产生
  finding），紧邻这类 marker 的数字不重复计 finding，因此数字无法借道无效
  marker 逃逸；
- marker span 内的数字属于 claim id（如 factqa-v1 里的 "1"），不是表面数字；
- ISO-8601 日期（口径标注）从数字扫描中剔除：scope date 是声明身份的一部分
  而非质量数字，否则 Task 8 生成的口径表会被自己的 checker 拦截。

Finding 不携带被扫描文本原文（detail 只含固定描述与 registry 侧状态），避免把
表面内容复制进报告；同输入同输出，排序按 (path, line, code) 逐字节确定。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from zhiwei.agents.claims import ClaimRecord, ClaimStatus

__all__ = ["CLAIMS_END", "CLAIMS_START", "Finding", "FindingCode", "scan_release_surface"]

CLAIMS_START = "<!-- claims:start -->"
CLAIMS_END = "<!-- claims:end -->"

_CLAIM_MARKER = re.compile(r"\{\{claim:([^}]+)\}\}")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

_VERIFIED_STATUSES = frozenset({ClaimStatus.OFFLINE_VERIFIED, ClaimStatus.LIVE_VERIFIED})
_LIVE_ENVIRONMENT = "live-production"


class FindingCode(StrEnum):
    UNSUPPORTED_NUMBER = "unsupported_number"
    FIXTURE_LIVE_MIX = "fixture_live_mix"
    STALE_ARTIFACT = "stale_artifact"
    UNKNOWN_CLAIM = "unknown_claim"


class Finding(BaseModel):
    """单条扫描发现；claim_id 是 registry 键，detail 不含被扫描的表面文本。"""

    model_config = ConfigDict(frozen=True)

    code: FindingCode
    path: str
    line: int = Field(ge=1)
    detail: str
    claim_id: str | None = None


def scan_release_surface(
    files: Mapping[str, str],
    registry: Mapping[str, ClaimRecord],
    now: str,
    stale_after_days: int,
) -> tuple[Finding, ...]:
    """对 release 表面声明块做确定性扫描，返回按 (path, line, code) 排序的 findings。"""
    if stale_after_days < 0:
        raise ValueError("stale_after_days must be non-negative")
    today = _parse_iso_date(now)
    findings: list[Finding] = []
    for path in sorted(files):
        in_block = False
        for line_no, line in enumerate(files[path].splitlines(), start=1):
            stripped = line.strip()
            if stripped == CLAIMS_START:
                in_block = True
                continue
            if stripped == CLAIMS_END:
                in_block = False
                continue
            if not in_block:
                continue
            findings.extend(
                _scan_line(path, line_no, line, registry, today, stale_after_days)
            )
    findings.sort(key=lambda f: (f.path, f.line, f.code.value, f.detail, f.claim_id or ""))
    return tuple(findings)


def _scan_line(
    path: str,
    line_no: int,
    line: str,
    registry: Mapping[str, ClaimRecord],
    today: date,
    stale_after_days: int,
) -> list[Finding]:
    # ISO 日期遮蔽成等长空白：行内 span 对齐不变，marker 邻接判断不受影响。
    masked = _ISO_DATE.sub(lambda match: " " * len(match.group(0)), line)
    markers = list(_CLAIM_MARKER.finditer(masked))
    marker_spans = [marker.span() for marker in markers]
    findings: list[Finding] = []
    for marker in markers:
        findings.extend(
            _check_marker(
                marker.group(1), path, line_no, masked, registry, today, stale_after_days
            )
        )
    for number in _NUMBER.finditer(masked):
        span = number.span()
        if _inside_any(span, marker_spans):
            continue
        adjacent_claim = _adjacent_verified_claim(span, markers, registry, masked)
        if isinstance(adjacent_claim, _AllowedNumber):
            # 紧邻 verified marker 且数值与该 claim 的 bound_value 一致：唯一放行路径
            continue
        if isinstance(adjacent_claim, str):
            findings.append(
                Finding(
                    code=FindingCode.UNSUPPORTED_NUMBER,
                    path=path,
                    line=line_no,
                    detail=(
                        "number adjacent to a verified claim marker does not match "
                        "the claim's bound value"
                    ),
                    claim_id=adjacent_claim,
                )
            )
            continue
        if _adjacent_to_any(span, marker_spans, masked):
            # 紧邻无效/非 verified marker 的数字由 _check_marker 的独立 finding
            # 覆盖（未知 id / 无 artifact 支撑），数字不重复计 finding。
            continue
        findings.append(
            Finding(
                code=FindingCode.UNSUPPORTED_NUMBER,
                path=path,
                line=line_no,
                detail="number inside a claims block has no adjacent claim marker",
            )
        )
    return findings


def _check_marker(
    claim_id: str,
    path: str,
    line_no: int,
    line: str,
    registry: Mapping[str, ClaimRecord],
    today: date,
    stale_after_days: int,
) -> list[Finding]:
    record = registry.get(claim_id)
    if record is None:
        return [
            Finding(
                code=FindingCode.UNKNOWN_CLAIM,
                path=path,
                line=line_no,
                detail="claim marker references an id absent from the claim registry",
                claim_id=claim_id,
            )
        ]
    findings: list[Finding] = []
    if record.status not in _VERIFIED_STATUSES:
        findings.append(
            Finding(
                code=FindingCode.UNSUPPORTED_NUMBER,
                path=path,
                line=line_no,
                detail=(
                    "referenced claim status "
                    f"{record.status.value!r} carries no artifact-backed evidence"
                ),
                claim_id=claim_id,
            )
        )
    if "(live)" in line.lower() and record.scope.environment != _LIVE_ENVIRONMENT:
        findings.append(
            Finding(
                code=FindingCode.FIXTURE_LIVE_MIX,
                path=path,
                line=line_no,
                detail="claim outside live-production scope is presented as live",
                claim_id=claim_id,
            )
        )
    scope_date = _parse_scope_date(record.scope.date)
    if scope_date is None:
        findings.append(
            Finding(
                code=FindingCode.STALE_ARTIFACT,
                path=path,
                line=line_no,
                detail="claim scope date is not an ISO-8601 date",
                claim_id=claim_id,
            )
        )
    elif (today - scope_date).days > stale_after_days:
        findings.append(
            Finding(
                code=FindingCode.STALE_ARTIFACT,
                path=path,
                line=line_no,
                detail=f"claim scope date is older than {stale_after_days} days",
                claim_id=claim_id,
            )
        )
    return findings


def _inside_any(span: tuple[int, int], marker_spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < marker_end and marker_start < end for marker_start, marker_end in marker_spans)


class _AllowedNumber:
    """哨兵类型：紧邻 verified marker 且数值与 bound_value 一致——唯一放行路径。"""


_ALLOWED_NUMBER = _AllowedNumber()


def _normalize_ws(value: str) -> str:
    return " ".join(value.split())


def _adjacent_verified_claim(
    span: tuple[int, int],
    markers: list[re.Match[str]],
    registry: Mapping[str, ClaimRecord],
    line: str,
) -> str | _AllowedNumber | None:
    """裁决紧邻 verified marker 的数字（缺口仅空白）。

    返回 _ALLOWED_NUMBER：数值与某个 verified claim 的 bound_value 逐字符一致
    （whitespace 归一后）——放行；返回 str：紧邻 verified claim 但数值不符或
    bound_value 缺失——拒绝，携带该 claim_id；返回 None：与 verified marker 无关
    （未紧邻，或紧邻的是无效/非 verified marker，后者由 _check_marker 独立拦截）。
    """
    start, end = span
    number_text = _normalize_ws(line[start:end])
    rejected_claim_id: str | None = None
    for marker in markers:
        marker_start, marker_end = marker.span()
        if marker_end <= start:
            gap = line[marker_end:start]
        elif end <= marker_start:
            gap = line[end:marker_start]
        else:
            continue
        if gap.strip():
            continue
        record = registry.get(marker.group(1))
        if record is None or record.status not in _VERIFIED_STATUSES:
            continue
        if (
            record.bound_value is not None
            and number_text == _normalize_ws(record.bound_value)
        ):
            return _ALLOWED_NUMBER
        if rejected_claim_id is None:
            rejected_claim_id = marker.group(1)
    return rejected_claim_id


def _adjacent_to_any(
    span: tuple[int, int], marker_spans: list[tuple[int, int]], line: str
) -> bool:
    start, end = span
    for marker_start, marker_end in marker_spans:
        if marker_end <= start and not line[marker_end:start].strip():
            return True
        if end <= marker_start and not line[end:marker_start].strip():
            return True
    return False


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"scan reference date is not an ISO-8601 date: {value!r}") from exc


def _parse_scope_date(value: str) -> date | None:
    # scope date 不可解析时返回 None 交由调用方出 finding，而不是让整个扫描崩溃。
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
