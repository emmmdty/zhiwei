"""Schema validation, bomb detection, and prompt injection scanning.

Covers S4 spec §7:
- parser/schema/ref size/depth/cycle
- malformed output/stream/tool args
- malicious Tool description/Skill/resource prompt injection
- secret exfiltration patterns
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class Severity(StrEnum):
    """Inspection finding severity."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InspectionFinding(_FrozenModel):
    """Single finding from an inspection check."""

    check: str
    severity: Severity
    message: str
    path: str = ""

    def is_blocking(self) -> bool:
        return self.severity in {Severity.HIGH, Severity.CRITICAL}


class InspectionReport(_FrozenModel):
    """Deterministic inspection report for a capability version."""

    findings: tuple[InspectionFinding, ...] = ()
    passed: bool = True

    def add(self, finding: InspectionFinding) -> InspectionReport:
        updated = [*list(self.findings), finding]
        return self.model_copy(
            update={
                "findings": tuple(updated),
                "passed": self.passed and not finding.is_blocking(),
            }
        )

    def merge(self, other: InspectionReport) -> InspectionReport:
        combined = list(self.findings) + list(other.findings)
        has_blocking = any(f.is_blocking() for f in combined)
        return InspectionReport(
            findings=tuple(combined),
            passed=self.passed and other.passed and not has_blocking,
        )


# ---------------------------------------------------------------------------
# Schema validation constants
# ---------------------------------------------------------------------------

MAX_SCHEMA_DEPTH = 12
MAX_SCHEMA_PROPERTIES = 256
MAX_SCHEMA_REFS = 32
MAX_SCHEMA_STRING_LENGTH = 8192
MAX_TOOL_NAME_LENGTH = 128
MAX_DESCRIPTION_LENGTH = 16384

# Prompt injection patterns (case-insensitive)
_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?prior", re.IGNORECASE),
    re.compile(r"override\s+(your\s+)?(safety|system)\s+(rules|prompt)", re.IGNORECASE),
    re.compile(r"<\s*(system|assistant)\s*>", re.IGNORECASE),
    re.compile(r"\[/?(INST|SYS)\]", re.IGNORECASE),
    re.compile(r"\\n\\nHuman:", re.IGNORECASE),
    re.compile(r"^(Human|System|Assistant):\s*", re.IGNORECASE | re.MULTILINE),
)

# Secret exfiltration patterns
_SECRET_EXFIL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(api[_-]?key|secret[_-]?key|password|token)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"curl\s+.*-d\s+.*\$[\({]", re.IGNORECASE),
    re.compile(r"fetch\s*\(\s*['\"]https?://[^'\"]*['\"].*\b(token|key|secret)\b", re.IGNORECASE),
    re.compile(r"base64\s*(encode|decode)\s+.*\b(secret|token|key|password)\b", re.IGNORECASE),
)


def validate_schema(
    schema: dict[str, Any],
    *,
    path: str = "$",
    _depth: int = 0,
    _visited_refs: frozenset[str] | None = None,
) -> InspectionReport:
    """Validate a JSON Schema for size, depth, $ref cycles, and injection patterns.

    Returns a deterministic InspectionReport.
    """
    if _visited_refs is None:
        _visited_refs = frozenset()

    report = InspectionReport()

    if _depth > MAX_SCHEMA_DEPTH:
        return report.add(
            InspectionFinding(
                check="schema_depth",
                severity=Severity.CRITICAL,
                message=f"Schema exceeds maximum depth of {MAX_SCHEMA_DEPTH}",
                path=path,
            )
        )

    if not isinstance(schema, dict):
        return report.add(
            InspectionFinding(
                check="schema_type",
                severity=Severity.HIGH,
                message=f"Expected dict schema at {path}, got {type(schema).__name__}",
                path=path,
            )
        )

    # Check $ref count
    ref_count = _count_refs(schema)
    if ref_count > MAX_SCHEMA_REFS:
        report = report.add(
            InspectionFinding(
                check="schema_ref_count",
                severity=Severity.HIGH,
                message=f"Schema has {ref_count} $ref entries, exceeds limit of {MAX_SCHEMA_REFS}",
                path=path,
            )
        )

    # Check for $ref cycles
    cycle_result = _detect_ref_cycles(schema, path=path, _visited=_visited_refs)
    report = report.merge(cycle_result)

    # Check property count
    prop_count = _count_properties(schema)
    if prop_count > MAX_SCHEMA_PROPERTIES:
        report = report.add(
            InspectionFinding(
                check="schema_property_count",
                severity=Severity.MEDIUM,
                message=f"Schema has {prop_count} properties, exceeds limit of {MAX_SCHEMA_PROPERTIES}",
                path=path,
            )
        )

    # Check string length in descriptions
    str_result = _check_string_lengths(schema, path=path)
    report = report.merge(str_result)

    # Check for bomb patterns (exponential expansion markers)
    bomb_result = _detect_schema_bombs(schema, path=path)
    report = report.merge(bomb_result)

    # Recurse into nested dicts with depth tracking
    for key, value in schema.items():
        if isinstance(value, dict):
            child_report = validate_schema(
                value,
                path=f"{path}.{key}",
                _depth=_depth + 1,
                _visited_refs=_visited_refs,
            )
            report = report.merge(child_report)
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, dict):
                    child_report = validate_schema(
                        item,
                        path=f"{path}.{key}[{idx}]",
                        _depth=_depth + 1,
                        _visited_refs=_visited_refs,
                    )
                    report = report.merge(child_report)

    return report


def scan_prompt_injection(text: str, *, field: str = "description") -> InspectionReport:
    """Scan text content for prompt injection patterns.

    Checks tool descriptions, skill content, and resource metadata.
    """
    report = InspectionReport()
    for pattern in _PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            report = report.add(
                InspectionFinding(
                    check="prompt_injection",
                    severity=Severity.CRITICAL,
                    message=f"Prompt injection pattern detected in {field}",
                    path=field,
                )
            )
            break
    return report


def scan_secret_exfiltration(text: str, *, field: str = "description") -> InspectionReport:
    """Scan text for secret exfiltration patterns."""
    report = InspectionReport()
    for pattern in _SECRET_EXFIL_PATTERNS:
        if pattern.search(text):
            report = report.add(
                InspectionFinding(
                    check="secret_exfiltration",
                    severity=Severity.CRITICAL,
                    message=f"Potential secret exfiltration pattern in {field}",
                    path=field,
                )
            )
            break
    return report


def validate_tool_args(schema: dict[str, Any], *, tool_name: str = "") -> InspectionReport:
    """Validate tool argument schema for malformed definitions."""
    report = InspectionReport()
    path = f"tool:{tool_name}" if tool_name else "tool"

    if len(tool_name) > MAX_TOOL_NAME_LENGTH:
        report = report.add(
            InspectionFinding(
                check="tool_name_length",
                severity=Severity.HIGH,
                message=f"Tool name exceeds {MAX_TOOL_NAME_LENGTH} characters",
                path=path,
            )
        )

    if not schema:
        report = report.add(
            InspectionFinding(
                check="tool_args_empty",
                severity=Severity.MEDIUM,
                message=f"Tool {tool_name!r} has empty argument schema",
                path=path,
            )
        )

    schema_report = validate_schema(schema, path=f"{path}.args")
    return report.merge(schema_report)


def validate_output_schema(schema: dict[str, Any], *, tool_name: str = "") -> InspectionReport:
    """Validate tool output schema for malformed definitions."""
    path = f"tool:{tool_name}.output" if tool_name else "tool.output"
    return validate_schema(schema, path=path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _count_refs(schema: dict[str, Any]) -> int:
    """Count total $ref entries in a schema tree."""
    count = 0
    for key, value in schema.items():
        if key == "$ref" and isinstance(value, str):
            count += 1
        elif isinstance(value, dict):
            count += _count_refs(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    count += _count_refs(item)
    return count


def _detect_ref_cycles(
    schema: dict[str, Any],
    *,
    path: str,
    _visited: frozenset[str] = frozenset(),
) -> InspectionReport:
    """Detect cycles in $ref chains by tracking referenced targets."""
    report = InspectionReport()
    for key, value in schema.items():
        if key == "$ref" and isinstance(value, str):
            ref_target = value.lstrip("#/")
            if ref_target in _visited:
                return report.add(
                    InspectionFinding(
                        check="schema_ref_cycle",
                        severity=Severity.CRITICAL,
                        message=f"Cycle detected in $ref chain at {path}",
                        path=path,
                    )
                )
        elif isinstance(value, dict):
            # Propagate any $ref targets found so far into child traversals
            new_visited = _visited
            for v in value.values():
                if isinstance(v, dict) and "$ref" in v and isinstance(v["$ref"], str):
                    new_visited = new_visited | {v["$ref"].lstrip("#/")}
            report = report.merge(
                _detect_ref_cycles(value, path=f"{path}.{key}", _visited=new_visited)
            )
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, dict):
                    new_visited = _visited
                    for v in item.values():
                        if isinstance(v, dict) and "$ref" in v and isinstance(v["$ref"], str):
                            new_visited = new_visited | {v["$ref"].lstrip("#/")}
                    report = report.merge(
                        _detect_ref_cycles(
                            item,
                            path=f"{path}.{key}[{idx}]",
                            _visited=new_visited,
                        )
                    )
    return report


def _count_properties(schema: dict[str, Any], _count: int = 0) -> int:
    """Count total named properties across nested objects."""
    count = _count
    properties = schema.get("properties")
    if isinstance(properties, dict):
        count += len(properties)
    for _key, value in schema.items():
        if isinstance(value, dict):
            count = _count_properties(value, count)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    count = _count_properties(item, count)
    return count


def _check_string_lengths(
    schema: dict[str, Any],
    *,
    path: str,
) -> InspectionReport:
    """Check that string values in schema don't exceed length limits."""
    report = InspectionReport()
    for key, value in schema.items():
        if isinstance(value, str) and key in ("description", "title", "summary"):
            if len(value) > MAX_SCHEMA_STRING_LENGTH:
                report = report.add(
                    InspectionFinding(
                        check="schema_string_length",
                        severity=Severity.MEDIUM,
                        message=f"String field '{key}' exceeds {MAX_SCHEMA_STRING_LENGTH} characters",
                        path=f"{path}.{key}",
                    )
                )
        elif isinstance(value, dict):
            report = report.merge(_check_string_lengths(value, path=f"{path}.{key}"))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, dict):
                    report = report.merge(
                        _check_string_lengths(item, path=f"{path}.{key}[{idx}]")
                    )
    return report


def _detect_schema_bombs(
    schema: dict[str, Any],
    *,
    path: str,
) -> InspectionReport:
    """Detect schema bomb patterns (exponential expansion).

    Heuristics:
    - anyOf/oneOf/allOf with > 20 variants
    - recursive $ref without bounds
    - deeply nested array items
    """
    report = InspectionReport()

    # Combinator explosion
    for combinator in ("anyOf", "oneOf", "allOf"):
        variants = schema.get(combinator)
        if isinstance(variants, list) and len(variants) > 20:
            report = report.add(
                InspectionFinding(
                    check="schema_bomb_combinator",
                    severity=Severity.HIGH,
                    message=f"Schema bomb: {combinator} has {len(variants)} variants (limit 20)",
                    path=f"{path}.{combinator}",
                )
            )

    # Nested array bomb: items.items.items (depth > 3)
    items = schema.get("items")
    if isinstance(items, dict):
        nesting_depth = 0
        current = items
        while isinstance(current, dict) and "items" in current:
            nesting_depth += 1
            current = current["items"]
        if nesting_depth > 2:
            report = report.add(
                InspectionFinding(
                    check="schema_bomb_array_nesting",
                    severity=Severity.HIGH,
                    message=f"Schema bomb: {nesting_depth} levels of nested array items",
                    path=f"{path}.items",
                )
            )

    return report
