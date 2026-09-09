"""S9-T3 冻结契约：诊断 scope 隔离与不可用确定性（external/blind/metamorphic）。

事实源：specs/s9 §3（external BIRD/LongMemEval/LoCoMo/Promptfoo/Inspect 分开报告、
层级 suite：internal frozen + blind holdout + external diagnostic + metamorphic）、
S9 plan Task 3。

两条不可协商的边界：

1. scope 隔离——external/blind/metamorphic 产物一律携带诊断 scope 标签
   （external_diagnostic:* / blind_holdout:* / metamorphic:*）；内部 suite 的名字
   不是诊断 scope，诊断机制也不得产出内部 scope。两类报告永远不混写。
2. 不可用确定性——缺数据/许可时的 unavailable 状态与机器可读理由必须确定性可
   复现（同一清单两次探测逐字节一致）；blind holdout 无显式密钥 → unavailable +
   holdout_key_missing，错误密钥 → fail closed 异常。缺数据/密钥都不得生成空
   成功报告（specs/s9 §7 Gate）。
"""

from __future__ import annotations

import hashlib
import importlib
import inspect as stdlib_inspect
import re
from pathlib import Path

import pytest

from zhiwei.contracts.canonical import canonical_json
from zhiwei.evals.ask_contracts import ASK_V1_SUITE
from zhiwei.evals.external import (
    EXTERNAL_ADAPTER_NAMES,
    ExternalAdapterSpec,
    diagnostic_scope,
    ensure_diagnostic_scope,
    load_adapter_manifest,
    probe_adapter,
    resolve_external_adapter,
    run_available_adapter,
)
from zhiwei.evals.external.holdout import (
    HoldoutKey,
    HoldoutKeyInvalid,
    HoldoutSuiteSpec,
    register_holdout_suite,
    unlock_holdout_suite,
)
from zhiwei.evals.external.metamorphic import (
    NUMERIC_RISK_METAMORPHIC_V1,
    resolve_metamorphic_suite,
)
from zhiwei.evals.factqa_suites import FACTQA_V1
from zhiwei.evals.knowledge_suites import KNOWLEDGE_SUITE_NAMES
from zhiwei.evals.memory_suites import ENTERPRISE_MEMORY_V1
from zhiwei.evals.risk_suites import RISK_SUITE_NAMES

# 全部已收口阶段的内部 suite 名：诊断 scope 与这些名字不得有任何交集。
INTERNAL_SUITE_NAMES = (
    frozenset({ASK_V1_SUITE, FACTQA_V1, ENTERPRISE_MEMORY_V1}) | RISK_SUITE_NAMES | KNOWLEDGE_SUITE_NAMES
)

_DIAGNOSTIC_MODULES = (
    "longmemeval",
    "locomo",
    "bird",
    "promptfoo",
    "inspect",
    "holdout",
    "metamorphic",
)


def _fixture_adapter() -> ExternalAdapterSpec:
    return ExternalAdapterSpec(
        name="scope-contract-adapter",
        benchmark="scope-contract-bench",
        data_dir="evals/external/scope-contract/data",
        data_glob="*.jsonl",
        license_file="evals/external/scope-contract/LICENSE",
        version_file="evals/external/scope-contract/VERSION",
        required_fields=("case_id",),
        claim_id="scope-contract-bench",
    )


class TestScopeSeparation:
    def test_diagnostic_scope_only_emits_declared_labels(self) -> None:
        assert (
            diagnostic_scope("external_diagnostic", "bird-adapter")
            == "external_diagnostic:bird-adapter"
        )
        assert diagnostic_scope("blind_holdout", "holdout-x") == "blind_holdout:holdout-x"
        assert diagnostic_scope("metamorphic", "m-1") == "metamorphic:m-1"
        with pytest.raises(ValueError, match="未知诊断 scope"):
            diagnostic_scope("internal", "whatever")

    def test_internal_suite_names_are_not_diagnostic_scopes(self) -> None:
        for name in sorted(INTERNAL_SUITE_NAMES):
            with pytest.raises(ValueError, match="不是诊断标签"):
                ensure_diagnostic_scope(name)

    def test_external_artifact_scope_never_collides_with_internal_suites(self, tmp_path: Path) -> None:
        spec = _fixture_adapter()
        data_dir = tmp_path / "evals/external/scope-contract/data"
        data_dir.mkdir(parents=True)
        (data_dir / "cases.jsonl").write_text('{"case_id": "c-1"}\n', encoding="utf-8")
        (tmp_path / "evals/external/scope-contract/LICENSE").write_text("L", encoding="utf-8")
        (tmp_path / "evals/external/scope-contract/VERSION").write_text("v1", encoding="utf-8")

        probe = probe_adapter(spec, root=tmp_path)
        result = run_available_adapter(spec, probe, root=tmp_path)
        # 外部完整性执行的 scope 必须通过诊断校验，且与任何内部 suite 名无交集。
        ensure_diagnostic_scope(result["scope"])
        assert result["scope"] == "external_diagnostic:scope-contract-adapter"
        assert result["scope"] not in INTERNAL_SUITE_NAMES

    def test_unavailable_probe_scope_also_passes_diagnostic_check(self) -> None:
        spec = _fixture_adapter()
        probe = probe_adapter(spec, root=Path("/nonexistent-root-for-scope-contract"))
        assert probe.status == "unavailable"
        ensure_diagnostic_scope(probe.scope)
        assert probe.scope not in INTERNAL_SUITE_NAMES

    def test_metamorphic_scope_is_diagnostic_and_separate(self) -> None:
        suite = resolve_metamorphic_suite(NUMERIC_RISK_METAMORPHIC_V1)
        ensure_diagnostic_scope(suite.scope)
        assert suite.scope.startswith("metamorphic:")
        assert suite.scope not in INTERNAL_SUITE_NAMES

    def test_diagnostic_modules_have_no_network_import_surface(self) -> None:
        # 外部/blind/metamorphic 诊断绝不静默下载：模块导入面不允许出现网络栈。
        network_import = re.compile(
            r"^\s*(?:import|from)\s+(urllib|requests|httpx|socket|http\.client|aiohttp)\b",
            re.MULTILINE,
        )
        for module_name in _DIAGNOSTIC_MODULES:
            module = importlib.import_module(f"zhiwei.evals.external.{module_name}")
            assert network_import.search(stdlib_inspect.getsource(module)) is None


class TestUnavailableDeterminism:
    def test_registered_adapters_probe_identically_across_runs(self) -> None:
        for spec in load_adapter_manifest():
            first = probe_adapter(spec)
            second = probe_adapter(spec)
            assert first == second
            assert canonical_json(list(first.reasons)) == canonical_json(list(second.reasons))

    def test_unavailable_reasons_are_machine_readable(self) -> None:
        # 仓库不含外部基准数据 → 全部 adapter unavailable；每条理由必须
        # {code, path, detail} 三元组、path 是仓库相对 POSIX 路径。
        for name in sorted(EXTERNAL_ADAPTER_NAMES):
            probe = probe_adapter(resolve_external_adapter(name))
            assert probe.status == "unavailable"
            assert probe.reasons
            for reason in probe.reasons:
                assert set(reason) == {"code", "path", "detail"}
                assert reason["path"] == Path(reason["path"]).as_posix()

    def test_unavailable_run_refuses_deterministically(self) -> None:
        spec = _fixture_adapter()
        probe = probe_adapter(spec, root=Path("/nonexistent-root-for-scope-contract"))
        with pytest.raises(RuntimeError, match="fail closed") as first:
            run_available_adapter(spec, probe, root=Path("/nonexistent-root-for-scope-contract"))
        with pytest.raises(RuntimeError, match="fail closed") as second:
            run_available_adapter(spec, probe, root=Path("/nonexistent-root-for-scope-contract"))
        assert str(first.value) == str(second.value)


class TestBlindHoldoutKeyBoundary:
    """blind holdout 密钥只能从显式 typed 参数进入；env/文件发现路径一律无效。"""

    @staticmethod
    def _register(name: str, material: str) -> None:
        register_holdout_suite(
            HoldoutSuiteSpec(
                name=name,
                source_suite=FACTQA_V1,
                claim_id=FACTQA_V1,
                key_digest=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            )
        )

    def test_missing_key_reports_unavailable_with_holdout_key_missing(self) -> None:
        self._register("scope-contract-holdout-missing", "operator-key")
        access = unlock_holdout_suite("scope-contract-holdout-missing", None)
        assert access.status == "unavailable"
        assert [reason["code"] for reason in access.reasons] == ["holdout_key_missing"]
        ensure_diagnostic_scope(access.scope)

    def test_env_is_never_consulted_for_holdout_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._register("scope-contract-holdout-env", "operator-key")
        monkeypatch.setenv("ZHIWEI_HOLDOUT_KEY", "operator-key")
        monkeypatch.setenv("BLIND_HOLDOUT_KEY", "operator-key")
        # env 里哪怕放着正确密钥，不显式传 typed 参数也必须保持 unavailable。
        access = unlock_holdout_suite("scope-contract-holdout-env", None)
        assert access.status == "unavailable"
        assert [reason["code"] for reason in access.reasons] == ["holdout_key_missing"]

    def test_wrong_key_fails_closed(self) -> None:
        self._register("scope-contract-holdout-wrong", "operator-key")
        with pytest.raises(HoldoutKeyInvalid, match="fail closed"):
            unlock_holdout_suite("scope-contract-holdout-wrong", HoldoutKey(material="wrong-key"))

    def test_correct_key_unlocks_with_digest_recorded(self) -> None:
        material = "operator-key"
        self._register("scope-contract-holdout-ok", material)
        access = unlock_holdout_suite("scope-contract-holdout-ok", HoldoutKey(material=material))
        assert access.status == "available"
        assert access.reasons == ()
        assert access.key_digest == hashlib.sha256(material.encode("utf-8")).hexdigest()
        assert access.scope == "blind_holdout:scope-contract-holdout-ok"

    def test_holdout_module_has_no_env_or_file_discovery(self) -> None:
        # 静态边界：holdout 模块源码不得出现 env/自动发现入口。
        source = stdlib_inspect.getsource(importlib.import_module("zhiwei.evals.external.holdout"))
        assert re.search(r"os\.environ|getenv|load_dotenv|Path\(|open\(", source) is None

    def test_unknown_holdout_suite_fails_closed(self) -> None:
        with pytest.raises(LookupError, match="未知 holdout suite"):
            unlock_holdout_suite("scope-contract-holdout-unknown", None)
