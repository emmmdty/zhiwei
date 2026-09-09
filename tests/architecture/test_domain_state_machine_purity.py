"""F-R1-04（P5/T-P5.2）：域状态机文件纯净性 AST 扫描。

规则（docs/ARCHITECTURE.md §2/§3 2026-09-08 修订后的双层约定）：
- **域状态机文件**（本测试显式登记）禁止导入 FastAPI/Temporal/SQLAlchemy/
  OpenSearch/provider SDK——「靠约定不靠机制」的漂移温床由此钉死；
- **模块内嵌 adapter**（persistence 落点）以显式 allowlist 登记于
  docs/ARCHITECTURE.md §3，不在本扫描禁令内。

新增域状态机文件须加入 `DOMAIN_STATE_MACHINE_FILES`（登记式，不静默豁免）。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "zhiwei"

# 域状态机/域逻辑文件（禁 infra import 的纯净面）。登记式清单——
# 与各 spec 的域层文件一一对应；新文件须显式登记。
DOMAIN_STATE_MACHINE_FILES: tuple[str, ...] = (
    # memory（S7：状态机唯一实现点 + 遗忘/检索/策略）
    "memory/domain.py",
    "memory/candidates.py",
    "memory/forget.py",
    "memory/policy.py",
    "memory/retrieval.py",
    "memory/confirmation.py",
    "memory/conflicts.py",
    # runtime（S2：纯 reducer + 命令/委托域逻辑）
    "runtime/reducer.py",
    "runtime/commands.py",
    "runtime/delegation.py",
    "runtime/context_slice.py",
    "runtime/attempts.py",
    "runtime/planner.py",
    # agents（S2/S9：任务图/版本状态机/发布域逻辑）
    "agents/task_graph.py",
    "agents/rollout.py",
    "agents/versions.py",
    # capabilities（S4：生命周期状态机）
    "capabilities/versions.py",
    # cases（S6）
    "cases/domain.py",
    # knowledge（S5：不可变契约/ACL 语义）
    "knowledge/contracts.py",
    "knowledge/acl.py",
)

BANNED_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "fastapi",
        "temporalio",
        "sqlalchemy",
        "opensearchpy",
        "openai",
        "anthropic",
        "redis",
        "httpx",
    }
)


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_domain_state_machine_files_exist() -> None:
    """登记清单不得空转：每个登记文件必须存在（防止文件移动后扫描失效）。"""
    missing = [rel for rel in DOMAIN_STATE_MACHINE_FILES if not (SRC / rel).exists()]
    assert not missing, f"登记的域状态机文件不存在: {missing}"
    assert len(DOMAIN_STATE_MACHINE_FILES) >= 15


def test_domain_state_machine_files_import_no_infra() -> None:
    violations: list[str] = []
    for rel in DOMAIN_STATE_MACHINE_FILES:
        path = SRC / rel
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        banned = _import_roots(tree) & BANNED_IMPORT_ROOTS
        if banned:
            violations.append(f"{rel}: {sorted(banned)}")
    assert not violations, (
        "域状态机文件禁止导入 infra/provider SDK（ARCHITECTURE §2 双层约定）："
        + "; ".join(violations)
    )
