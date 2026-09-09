"""F-R2-01：ADR-001 结构约束的 AST 架构测试（防回退核心锚点）。

钉死三条：
1. src/zhiwei/models/ 内 AsyncClient 构造点唯一且位于 client_factory.py，transport
   实参是 CaptureTransport(...) 且 gate 实参非 None（gate 从可选变必选）；
2. 工厂模块内的默认 inner 是真实 HTTP transport（AsyncHTTPTransport 构造点恰一个）；
3. CaptureTransport 构造点全 src 普查：定义模块、工厂、已登记 eval seam
   （evals/executors/）之外出现即违规——生产 egress 只能经工厂组装。

这是 ADR-001（docs/DECISIONS.md「对 S3-T5 的最小实现骨架建议」配套三条结构约束）
要求的 architecture test；评审 finding F-R2-01 指出其缺失。
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO_ROOT / "src/zhiwei/models"
FACTORY_MODULE = "client_factory.py"
PRESEND_MODULE = "presend.py"
# eval seam（evals/executors/security.py）是评审前已登记的评测组装点，豁免。
EVAL_SEAM_DIR = REPO_ROOT / "src/zhiwei/evals/executors"


def _python_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _collect_calls(tree: ast.AST, names: set[str]) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _call_name(n) in names]


def _resolve_name(node: ast.AST, tree: ast.AST) -> ast.AST:
    """把模块内单赋值变量解析到其右值（transport = CaptureTransport(...) 形态）。"""
    if isinstance(node, ast.Name):
        for stmt in ast.walk(tree):
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == node.id
            ):
                return stmt.value
    return node


class TestModelsClientFactoryArchitecture:
    def test_async_client_construction_is_unique_and_in_factory(self) -> None:
        constructions: list[tuple[Path, ast.Call]] = []
        scanned = 0
        for path in _python_files(MODELS_DIR):
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for call in _collect_calls(tree, {"AsyncClient"}):
                constructions.append((path, call))
        assert scanned > 5, "scan must cover the real models/ tree"
        assert len(constructions) == 1, (
            "models/ must have exactly one AsyncClient construction (the factory)"
        )
        path, call = constructions[0]
        assert path.name == FACTORY_MODULE, (
            f"the single AsyncClient construction must live in {FACTORY_MODULE}"
        )

    def test_factory_transport_is_capture_transport_with_gate(self) -> None:
        factory = ast.parse((MODELS_DIR / FACTORY_MODULE).read_text(encoding="utf-8"))
        capture_calls = _collect_calls(factory, {"CaptureTransport"})
        assert len(capture_calls) == 1, "factory must assemble exactly one CaptureTransport"
        # CaptureTransport 是被包装方：工厂把它作为 transport 实参传给 AsyncClient。
        client_calls = _collect_calls(factory, {"AsyncClient"})
        assert len(client_calls) == 1
        transport_kw = {kw.arg: kw.value for kw in client_calls[0].keywords if kw.arg}[
            "transport"
        ]
        transport_call = _resolve_name(transport_kw, factory)
        assert _call_name(transport_call) == "CaptureTransport", (
            "client transport must be CaptureTransport(...)"
        )
        capture_keywords = {
            kw.arg: kw.value for kw in transport_call.keywords if kw.arg  # type: ignore[union-attr]
        }
        assert "gate" in capture_keywords, "CaptureTransport must be constructed with gate="
        assert not isinstance(capture_keywords["gate"], ast.Constant) or (
            capture_keywords["gate"].value is not None
        ), "gate must not be None literal"
        assert "inner" in capture_keywords, "CaptureTransport must be constructed with inner="

    def test_factory_default_inner_is_real_http_transport(self) -> None:
        factory = ast.parse((MODELS_DIR / FACTORY_MODULE).read_text(encoding="utf-8"))
        transports = _collect_calls(factory, {"AsyncHTTPTransport"})
        assert len(transports) == 1, (
            "factory default inner must be exactly one real AsyncHTTPTransport"
        )

    def test_capture_transport_construction_only_in_sanctioned_modules(self) -> None:
        allowed = {
            REPO_ROOT / "src/zhiwei/models" / PRESEND_MODULE,
            REPO_ROOT / "src/zhiwei/models" / FACTORY_MODULE,
        }
        violations: list[str] = []
        scanned = 0
        for path in _python_files(REPO_ROOT / "src/zhiwei"):
            scanned += 1
            if path in allowed or EVAL_SEAM_DIR in path.parents:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if _collect_calls(tree, {"CaptureTransport"}):
                violations.append(str(path.relative_to(REPO_ROOT)))
        assert scanned > 100, "scan must cover the real src tree"
        assert violations == []
