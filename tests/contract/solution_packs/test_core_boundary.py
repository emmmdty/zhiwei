"""S2 修复轮批次 C RED（架构边界，spec §6）：Core 不导入具体 Solution Pack 名称。

原测试（tests/contract/solution_packs/test_schema.py::test_*）断言的
`zhiwei.apps` 前缀在代码树中不存在——恒真；且 `except ImportError: pass`
是 fail-open。本文件重写为：遍历真实 core 模块（runtime/workflows/workers/
evals），断言全部可导入（ImportError = FAIL），且不引用保留的 concrete-pack
命名空间（zhiwei.packs）。
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

CORE_PACKAGES = ("zhiwei.runtime", "zhiwei.workflows", "zhiwei.workers", "zhiwei.evals")
# concrete Solution Pack 的保留命名空间（S3+ 落地；Core 只经 registry/port 引用）
_RESERVED_PACK_NAMESPACE = "zhiwei.packs"


def _core_modules() -> list[str]:
    modules: list[str] = []
    for package_name in CORE_PACKAGES:
        package = importlib.import_module(package_name)
        modules.append(package_name)
        for info in pkgutil.walk_packages(package.__path__, prefix=f"{package_name}."):
            modules.append(info.name)
    assert modules, "core 模块清单不得为空（空清单使本测试恒真）"
    return sorted(set(modules))


class TestCoreBoundary:
    @pytest.mark.parametrize("module_name", _core_modules())
    def test_core_module_imports_cleanly(self, module_name: str) -> None:
        """ImportError 是失败不是通过（fail closed）。"""
        importlib.import_module(module_name)

    @pytest.mark.parametrize("module_name", _core_modules())
    def test_core_module_does_not_reference_concrete_packs(self, module_name: str) -> None:
        """Core 不得 import 保留的 concrete-pack 命名空间（spec §6 架构边界）。"""
        import inspect

        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        assert f"import {_RESERVED_PACK_NAMESPACE}" not in source
        assert f"from {_RESERVED_PACK_NAMESPACE}" not in source
