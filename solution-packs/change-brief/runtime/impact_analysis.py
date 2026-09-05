"""ChangeBrief pack impact-analysis skill entry.

skills/impact-analysis.yaml 声明的 entry 模块（S10-T6 起为真实实现）：对检索到的
代码知识快照做确定性影响分析——零模型调用、零 Core 导入、纯函数。

推导规则（specs/s10 §4；expected 断言数据的可复算事实源）：
- 触达面 = 改动文件 ∪ 改动符号；affected 起点是快照命中（定义文件被触达或符号名
  在改动列表内），沿「调用方」边闭包传播（被影响符号的调用方受影响）；
- 快照之外的改动符号绝不进入 affected——它们只能以 unknowns 的形式如实披露
  （fail closed：不编造影响面）；
- 依赖按消费者命中关联；测试按受影响符号的引用关联，定义文件被触达的预期 fail；
- PR 按 touches 文件、issue 按 mentions 符号、check 按 ref 关联；
- risks 由确定性规则派生（符号移除 → high、影响面 ≥4 → high、依赖契约面 → medium、
  分析不完整 → medium）。

依赖纪律：本模块是 pack runtime（tests/architecture/test_app_boundaries.py 的
PACK_RUNTIME_BANNED_IMPORTS 约束）——不导入 DB / model provider / 基础设施工具。
"""

from __future__ import annotations

from typing import Any

_TRIGGER_KINDS = frozenset({"commit", "pull_request"})

_BROAD_IMPACT_THRESHOLD = 4


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"impact analysis: {label} 必须是非空字符串")
    return value


def analyze_impact(
    repository: str,
    commit_or_pr: dict[str, Any],
    candidates: dict[str, Any],
) -> dict[str, Any]:
    """Compute the impact report consumed by the Verify/Synthesize tasks.

    输入是检索候选集（快照子集）；输出是 task_graph.yaml 的 analyze_impact.outputs
    声明的 impact 形态：affected symbols/dependencies/tests、related PRs/issues/
    checks、risks、unknowns、code_refs/github_refs（Evidence ref 词汇镜像）。
    """
    repo = _require_str(repository, "repository")
    kind = commit_or_pr.get("kind")
    if kind not in _TRIGGER_KINDS:
        raise ValueError(f"impact analysis: 未知 trigger kind {kind!r}")
    ref = _require_str(commit_or_pr.get("ref"), "commit_or_pr.ref")

    files_changed = _files_changed(candidates)
    touched_files = sorted({fc["path"] for fc in files_changed})
    changed_symbols = sorted({s for fc in files_changed for s in fc["symbols_after"]})
    removed_symbols = sorted(
        {
            s
            for fc in files_changed
            for s in fc["symbols_before"]
            if s not in fc["symbols_after"]
        }
    )

    symbols = candidates.get("symbols") or []
    by_name: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        name = _require_str(symbol.get("name"), "snapshot symbol name")
        if name in by_name:
            raise ValueError(f"impact analysis: 快照符号名重复: {name}")
        by_name[name] = symbol

    touched_set = set(touched_files)
    changed_set = set(changed_symbols)
    direct = {
        name
        for name, symbol in by_name.items()
        if symbol.get("file_path") in touched_set or name in changed_set
    }
    affected = set(direct)
    changed = True
    while changed:
        changed = False
        for name in by_name:
            # 调用方闭包：s.callers 列出「谁调用了 s」——被影响符号的调用方受影响。
            if name not in affected and any(
                name in by_name[a].get("callers", ()) for a in affected
            ):
                affected.add(name)
                changed = True

    # 快照之外的改动符号：只能成为 unknowns，绝不编造进影响面。
    unknown_symbols = [name for name in changed_symbols if name not in by_name]
    snapshot_file_paths = {symbol.get("file_path") for symbol in symbols}

    affected_dependencies = sorted(
        (
            {
                "name": dep["name"],
                "version_constraint": dep.get("version_constraint", ""),
                "impact": "direct",
            }
            for dep in candidates.get("dependencies") or []
            if set(dep.get("consumers") or ()) & affected
        ),
        key=lambda dep: dep["name"],
    )

    test_status: dict[str, str] = {}
    for name in sorted(affected):
        symbol = by_name[name]
        definition_touched = symbol.get("file_path") in touched_set
        for test_id in symbol.get("tests") or ():
            if definition_touched:
                test_status[test_id] = "fail"
            else:
                test_status.setdefault(test_id, "pass")
    affected_tests = sorted(test_status)

    related_prs = sorted(
        {
            pr["pr_number"]
            for pr in candidates.get("prs") or []
            if set(pr.get("touches") or ()) & touched_set
        }
    )
    related_issues = sorted(
        {
            issue["issue_number"]
            for issue in candidates.get("issues") or []
            if set(issue.get("mentions") or ()) & affected
        }
    )
    related_check_names = {
        check["name"]
        for check in candidates.get("checks") or []
        if ref in set(check.get("on_refs") or ())
    }
    related_checks = [
        {"name": check["name"], "status": check["status"]}
        for check in sorted(
            candidates.get("checks") or [], key=lambda c: c.get("name", "")
        )
        if check["name"] in related_check_names
    ]

    code_refs = [
        {
            "file_path": symbol["file_path"],
            "line_start": symbol["line_start"],
            "line_end": symbol["line_end"],
            "code_digest": symbol["code_digest"],
        }
        for symbol in sorted(
            (by_name[name] for name in affected),
            key=lambda s: (s.get("file_path", ""), s.get("line_start", 0), s["name"]),
        )
    ]
    identity: dict[str, Any] = (
        {"commit_sha": ref} if kind == "commit" else {"pr_number": _pr_number(ref)}
    )
    github_refs = [
        {"repository": repo, **identity, "path": path} for path in touched_files
    ]

    risks = _derive_risks(
        removed_symbols=removed_symbols,
        removed_caller_counts={
            name: len(by_name[name].get("callers") or ())
            for name in removed_symbols
            if name in by_name
        },
        affected=affected,
        affected_dependencies=affected_dependencies,
        unknown_symbols=unknown_symbols,
        missing_files=sorted(touched_set - snapshot_file_paths),
    )
    unknowns = sorted(
        [f"symbol '{name}' not present in knowledge snapshot" for name in unknown_symbols]
        + [
            f"changed file '{path}' not present in knowledge snapshot"
            for path in sorted(touched_set - snapshot_file_paths)
        ]
    )

    return {
        "repository": repo,
        "commit_or_pr": {"kind": kind, "ref": ref},
        "affected_symbols": [
            {
                "name": name,
                "kind": by_name[name]["kind"],
                "file_path": by_name[name]["file_path"],
                "line_start": by_name[name]["line_start"],
                "line_end": by_name[name]["line_end"],
            }
            for name in sorted(affected)
        ],
        "affected_dependencies": affected_dependencies,
        "affected_tests": [
            {"test_id": test_id, "expected_status": test_status[test_id]}
            for test_id in affected_tests
        ],
        "related_prs": [{"repository": repo, "pr_number": n} for n in related_prs],
        "related_issues": [
            {"repository": repo, "issue_number": n} for n in related_issues
        ],
        "related_checks": related_checks,
        "risks": risks,
        "unknowns": unknowns,
        "code_refs": code_refs,
        "github_refs": github_refs,
    }


def _files_changed(candidates: dict[str, Any]) -> list[dict[str, Any]]:
    """candidates 携带的触发上下文：plan_retrieval 物化时绑定的改动清单。

    检索候选集与触发面同源（同一场景绑定），analyze_impact 只接受它已校验的
    形态——缺失即 fail closed，不猜空改动。
    """
    files = candidates.get("files_changed")
    if not isinstance(files, list) or not files:
        raise ValueError("impact analysis: 候选集缺少 files_changed 触发上下文")
    return files


def _pr_number(ref: str) -> int:
    try:
        value = int(ref)
    except ValueError as exc:
        raise ValueError(f"impact analysis: PR ref 必须是数字字符串: {ref!r}") from exc
    if value < 1:
        raise ValueError(f"impact analysis: PR ref 必须 >= 1: {ref!r}")
    return value


def _derive_risks(
    *,
    removed_symbols: list[str],
    removed_caller_counts: dict[str, int],
    affected: set[str],
    affected_dependencies: list[dict[str, Any]],
    unknown_symbols: list[str],
    missing_files: list[str],
) -> list[dict[str, str]]:
    """确定性风险规则（pack.yaml escalation_rules 的 offline 可复算形态）。"""
    risks: list[dict[str, str]] = []
    for name in removed_symbols:
        if name in removed_caller_counts:
            risks.append(
                {
                    "description": (
                        f"symbol '{name}' removed; "
                        f"{removed_caller_counts[name]} caller(s) may break"
                    ),
                    "severity": "high",
                }
            )
    if len(affected) >= _BROAD_IMPACT_THRESHOLD:
        risks.append(
            {
                "description": f"broad impact: {len(affected)} symbols affected",
                "severity": "high",
            }
        )
    if affected_dependencies:
        names = ", ".join(dep["name"] for dep in affected_dependencies)
        risks.append(
            {
                "description": f"dependency contract surface touched: {names}",
                "severity": "medium",
            }
        )
    if unknown_symbols:
        risks.append(
            {
                "description": (
                    f"analysis incomplete: {len(unknown_symbols)} changed symbol(s) "
                    "absent from knowledge snapshot"
                ),
                "severity": "medium",
            }
        )
    if missing_files:
        risks.append(
            {
                "description": (
                    "changed file(s) absent from knowledge snapshot: "
                    f"{', '.join(missing_files)}"
                ),
                "severity": "medium",
            }
        )
    return risks
