"""S10 冻结契约：前端/核心架构边界（A 档，S10-T1/T7）。

总设计 §9/§10 + specs/s10 §2/§6：Core 不知道任何具体 App；通用面板不得写 App 名称
条件；App UI 只经 ViewManifest 注册的 renderer 出现；删除 ChangeBrief 不影响 Core。
本文件是架构层的冻结契约——实现（renderer registry / app 骨架 / ChangeBrief pack）
必须满足此处全部扫描断言；GREEN 阶段不得修改本文件。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Core 中禁止出现任何 ChangeBrief 标识的层（App 逻辑只允许存在于 solution-packs/ 与
# web renderers/）。src/zhiwei/evals 的 suite 资产层与 cli/evals.py、cli/assets.py 的
# suite-id/资产目录注册同属「评测资产=数据」模式（factqa/memory/security 同型，
# 注册表是数据不是 App 条件分支），明确豁免。
CORE_BANNED_LAYERS = (
    "src/zhiwei/agents",
    "src/zhiwei/api",
    "src/zhiwei/app.py",
    "src/zhiwei/capabilities",
    "src/zhiwei/cases",
    "src/zhiwei/config",
    "src/zhiwei/context",
    "src/zhiwei/contracts",
    "src/zhiwei/discover",
    "src/zhiwei/evidence",
    "src/zhiwei/identity",
    "src/zhiwei/knowledge",
    "src/zhiwei/memory",
    "src/zhiwei/models",
    "src/zhiwei/object_store",
    "src/zhiwei/persistence",
    "src/zhiwei/policy",
    "src/zhiwei/runtime",
    "src/zhiwei/secrets",
    "src/zhiwei/telemetry",
    "src/zhiwei/workers",
    "src/zhiwei/workflows",
)
CHANGE_BRIEF_PATTERN = re.compile(r"change[-_]?brief", re.IGNORECASE)

# 通用 web 层禁止 App 名称条件；App UI 只能住在 renderers/（ViewManifest 注册）。
WEB_GENERIC_LAYERS = (
    "apps/web/src/App.tsx",
    "apps/web/src/main.tsx",
    "apps/web/src/app",
    "apps/web/src/routes",
    "apps/web/src/state",
    "apps/web/src/components",
    "apps/web/src/lib",
    "apps/web/src/features",
)
WEB_BANNED_PATTERNS = (
    re.compile(r"\bask\b", re.IGNORECASE),
    re.compile(r"\bdiscover\b", re.IGNORECASE),
    re.compile(r"change[-_]?brief", re.IGNORECASE),
)

# App renderer 只能经 registry 出现在通用层：features/ 不得直接 import renderers/。
RENDERER_IMPORT_PATTERN = re.compile(
    r"(?:from\s+[\"'][^\"']*(?:^|/)renderers/|import\([^)]*renderers/)", re.IGNORECASE
)

# Pack 运行时模块的依赖纪律（plan Task 6）：不得直接触碰 DB/模型 provider/基础设施工具。
PACK_RUNTIME_BANNED_IMPORTS = (
    "zhiwei.persistence",
    "zhiwei.api",
    "zhiwei.cli",
    "zhiwei.config",
    "sqlalchemy",
    "asyncpg",
    "openai",
    "anthropic",
    "temporalio",
    "redis",
    "httpx",
)


def _python_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _web_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    return sorted(
        p
        for p in root.rglob("*")
        if p.suffix in {".ts", ".tsx"} and "node_modules" not in p.parts
    )


class TestCoreDoesNotKnowChangeBrief:
    def test_core_layers_are_change_brief_free(self) -> None:
        violations: list[str] = []
        scanned = 0
        for layer in CORE_BANNED_LAYERS:
            for path in _python_files(REPO_ROOT / layer):
                scanned += 1
                text = path.read_text(encoding="utf-8")
                if CHANGE_BRIEF_PATTERN.search(text):
                    violations.append(str(path.relative_to(REPO_ROOT)))
        assert scanned > 40, "architecture scan must cover the real core tree"
        assert violations == []

    def test_eval_asset_layer_is_the_only_sanctioned_registration(self) -> None:
        # evals 资产层与 cli/evals.py、cli/assets.py 的注册数据按既有模式豁免；这里
        # 断言豁免面没有扩大：除这些位置外，src/zhiwei 其余 python 文件必须全部干净。
        exempt_files = {
            REPO_ROOT / "src/zhiwei/cli/evals.py",
            REPO_ROOT / "src/zhiwei/cli/assets.py",
        }
        exempt_dirs = {REPO_ROOT / "src/zhiwei/evals"}
        violations: list[str] = []
        for path in _python_files(REPO_ROOT / "src/zhiwei"):
            if path in exempt_files or any(
                exempt_dir in path.parents for exempt_dir in exempt_dirs
            ):
                continue
            if CHANGE_BRIEF_PATTERN.search(path.read_text(encoding="utf-8")):
                violations.append(str(path.relative_to(REPO_ROOT)))
        assert violations == []


class TestWebGenericLayersHaveNoAppConditionals:
    def test_generic_layers_do_not_branch_on_app_names(self) -> None:
        violations: list[str] = []
        scanned = 0
        for layer in WEB_GENERIC_LAYERS:
            for path in _web_files(REPO_ROOT / layer):
                scanned += 1
                text = path.read_text(encoding="utf-8")
                for pattern in WEB_BANNED_PATTERNS:
                    if pattern.search(text):
                        violations.append(
                            f"{path.relative_to(REPO_ROOT)}:{pattern.pattern}"
                        )
        assert scanned > 5, "web scan must cover the real generic layers"
        assert violations == []

    def test_features_may_not_import_renderers_directly(self) -> None:
        violations: list[str] = []
        for path in _web_files(REPO_ROOT / "apps/web/src/features"):
            if RENDERER_IMPORT_PATTERN.search(path.read_text(encoding="utf-8")):
                violations.append(str(path.relative_to(REPO_ROOT)))
        assert violations == []

    def test_renderer_registry_exists_and_is_data_driven(self) -> None:
        registry = REPO_ROOT / "apps/web/src/renderers/registry.ts"
        assert registry.is_file(), "ViewManifest registry must exist"
        text = registry.read_text(encoding="utf-8")
        # registry 必须是注册表（register/resolve 接口），而不是硬编码分支。
        assert "registerRenderer" in text or "registerApp" in text
        assert "resolveRenderer" in text or "resolve" in text


class TestPackRuntimeDiscipline:
    def test_pack_runtime_imports_no_infra_or_provider(self) -> None:
        runtime_dirs = [
            REPO_ROOT / "solution-packs/change-brief/runtime",
        ]
        scanned: list[Path] = []
        violations: list[str] = []
        for runtime_dir in runtime_dirs:
            for path in _python_files(runtime_dir):
                scanned.append(path)
                text = path.read_text(encoding="utf-8")
                for banned in PACK_RUNTIME_BANNED_IMPORTS:
                    if re.search(
                        rf"^\s*(?:from|import)\s+{re.escape(banned)}\b",
                        text,
                        re.MULTILINE,
                    ):
                        violations.append(f"{path.name}:{banned}")
        if scanned:
            # ChangeBrief pack 在库时必须有运行时模块；目录不存在则该断言空转，
            # 由 test_change_brief pack conformance 侧兜底。
            assert violations == []
