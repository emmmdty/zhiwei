"""S7-T7 RED: external adapter registry 与确定性可用性探测。

事实源：specs/s7-memory.md §8（external-status 二选一 sealed artifact 契约）、
ADR-013 决策 2（`eval external-status` 是真实产品能力）、S7 plan Task 7。

探测必须是确定性的本地检查：config/evals/ 登记的 adapter 清单 + 本地数据目录/
许可文件存在性；available 时附许可/version/checksum 并实际运行 adapter 的
完整性执行（离线确定性，不产生任何质量分数——那需要 live 模型）。

S9-T3 扩展（specs/s9 §3）：per-adapter 模块（longmemeval/locomo/bird/promptfoo/
inspect）、scope 标签 external_diagnostic:*、harness 纯转换层。新模块的导入放在
测试函数内：这些模块在 GREEN 之前不存在，函数级导入保证既有断言在 RED 阶段
仍可运行、失败点精确落在未实现的契约上。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zhiwei.contracts.canonical import canonical_json
from zhiwei.evals.external import (
    EXTERNAL_ADAPTER_NAMES,
    ExternalAdapterSpec,
    load_adapter_manifest,
    probe_adapter,
    resolve_external_adapter,
    run_available_adapter,
)


@pytest.fixture
def fixture_adapter(tmp_path: Path) -> tuple[ExternalAdapterSpec, Path]:
    """一个指向 tmp_path 的最小 adapter 声明（fixture 数据驱动的 available 分支）。"""
    spec = ExternalAdapterSpec(
        name="fixture-adapter",
        benchmark="fixture-bench",
        data_dir="evals/external/fixture/data",
        data_glob="*.jsonl",
        license_file="evals/external/fixture/LICENSE",
        version_file="evals/external/fixture/VERSION",
        required_fields=("question_id", "question", "answer"),
        claim_id="fixture-bench",
    )
    return spec, tmp_path


def _materialize_available(root: Path) -> None:
    data_dir = root / "evals/external/fixture/data"
    data_dir.mkdir(parents=True)
    (data_dir / "episodes-1.jsonl").write_text(
        '{"question_id": "q-1", "question": "q", "answer": "a"}\n'
        '{"question_id": "q-2", "question": "q", "answer": "a"}\n',
        encoding="utf-8",
    )
    (data_dir / "episodes-2.jsonl").write_text(
        '{"question_id": "q-3", "question": "q", "answer": "a"}\n',
        encoding="utf-8",
    )
    (root / "evals/external/fixture/LICENSE").write_text("CC-BY-4.0\n", encoding="utf-8")
    (root / "evals/external/fixture/VERSION").write_text("v1.0-fixture\n", encoding="utf-8")


class TestManifest:
    def test_longmemeval_adapter_is_registered(self) -> None:
        assert "longmemeval-adapter" in EXTERNAL_ADAPTER_NAMES

    def test_resolve_unknown_adapter_fails_closed(self) -> None:
        with pytest.raises(LookupError, match="未知 external adapter"):
            resolve_external_adapter("longmemeval-v2")

    def test_manifest_rejects_unknown_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        path.write_text(
            "adapters:\n"
            "  - name: x-adapter\n"
            "    benchmark: x\n"
            "    data_dir: d\n"
            "    data_glob: '*.jsonl'\n"
            "    license_file: L\n"
            "    version_file: V\n"
            "    required_fields: [q]\n"
            "    claim_id: x\n"
            "    surprise_field: nope\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="surprise_field"):
            load_adapter_manifest(path)

    def test_manifest_rejects_duplicate_names(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        entry = (
            "  - name: dup-adapter\n"
            "    benchmark: x\n"
            "    data_dir: d\n"
            "    data_glob: '*.jsonl'\n"
            "    license_file: L\n"
            "    version_file: V\n"
            "    required_fields: [q]\n"
            "    claim_id: x\n"
        )
        path.write_text("adapters:\n" + entry + entry, encoding="utf-8")
        with pytest.raises(ValueError, match="dup-adapter"):
            load_adapter_manifest(path)

    def test_manifest_rejects_path_escape(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        path.write_text(
            "adapters:\n"
            "  - name: escape-adapter\n"
            "    benchmark: x\n"
            "    data_dir: ../outside\n"
            "    data_glob: '*.jsonl'\n"
            "    license_file: L\n"
            "    version_file: V\n"
            "    required_fields: [q]\n"
            "    claim_id: x\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="相对路径"):
            load_adapter_manifest(path)


class TestProbe:
    def test_missing_everything_is_unavailable_with_machine_readable_reasons(
        self, fixture_adapter: tuple[ExternalAdapterSpec, Path]
    ) -> None:
        spec, root = fixture_adapter
        probe = probe_adapter(spec, root=root)
        assert probe.status == "unavailable"
        codes = {reason["code"] for reason in probe.reasons}
        assert codes == {"missing_data_dir", "missing_file"}
        # 机器可读：每条 reason 携带期望路径
        paths = {reason["path"] for reason in probe.reasons}
        assert "evals/external/fixture/LICENSE" in paths
        assert "evals/external/fixture/VERSION" in paths
        assert "evals/external/fixture/data" in paths

    def test_empty_data_dir_is_unavailable(self, fixture_adapter: tuple[ExternalAdapterSpec, Path]) -> None:
        spec, root = fixture_adapter
        (root / "evals/external/fixture/data").mkdir(parents=True)
        (root / "evals/external/fixture/LICENSE").write_text("L", encoding="utf-8")
        (root / "evals/external/fixture/VERSION").write_text("v1", encoding="utf-8")
        probe = probe_adapter(spec, root=root)
        assert probe.status == "unavailable"
        assert {reason["code"] for reason in probe.reasons} == {"no_data_files"}

    def test_complete_local_files_are_available(
        self, fixture_adapter: tuple[ExternalAdapterSpec, Path]
    ) -> None:
        spec, root = fixture_adapter
        _materialize_available(root)
        probe = probe_adapter(spec, root=root)
        assert probe.status == "available"
        assert probe.reasons == ()
        assert probe.version == "v1.0-fixture"
        assert [path.name for path in probe.data_files] == [
            "episodes-1.jsonl",
            "episodes-2.jsonl",
        ]

    def test_probe_is_deterministic(self, fixture_adapter: tuple[ExternalAdapterSpec, Path]) -> None:
        spec, root = fixture_adapter
        _materialize_available(root)
        first = probe_adapter(spec, root=root)
        second = probe_adapter(spec, root=root)
        assert first == second


class TestRunAvailable:
    def test_run_refuses_unavailable_probe(
        self, fixture_adapter: tuple[ExternalAdapterSpec, Path]
    ) -> None:
        spec, root = fixture_adapter
        probe = probe_adapter(spec, root=root)
        with pytest.raises(RuntimeError, match="unavailable"):
            run_available_adapter(spec, probe, root=root)

    def test_run_reports_license_version_and_checksums(
        self, fixture_adapter: tuple[ExternalAdapterSpec, Path]
    ) -> None:
        spec, root = fixture_adapter
        probe = probe_adapter(spec, root=root)
        _materialize_available(root)
        probe = probe_adapter(spec, root=root)
        result = run_available_adapter(spec, probe, root=root)
        assert result["run_kind"] == "corpus-integrity"
        assert result["license"]["sha256"]
        assert result["version"]["content"] == "v1.0-fixture"
        assert result["data"]["record_count"] == 3
        assert len(result["data"]["files"]) == 2
        assert result["data"]["total_checksum"]

    def test_run_is_deterministic(
        self, fixture_adapter: tuple[ExternalAdapterSpec, Path]
    ) -> None:
        spec, root = fixture_adapter
        _materialize_available(root)
        probe = probe_adapter(spec, root=root)
        first = run_available_adapter(spec, probe, root=root)
        second = run_available_adapter(spec, probe, root=root)
        assert first == second

    def test_run_fails_closed_on_schema_violation(
        self, fixture_adapter: tuple[ExternalAdapterSpec, Path]
    ) -> None:
        spec, root = fixture_adapter
        _materialize_available(root)
        (root / "evals/external/fixture/data/episodes-2.jsonl").write_text(
            '{"question_id": "q-3"}\n',  # 缺 question/answer 必需字段
            encoding="utf-8",
        )
        probe = probe_adapter(spec, root=root)
        with pytest.raises(RuntimeError, match=r"episodes-2\.jsonl"):
            run_available_adapter(spec, probe, root=root)


# ---------------------------------------------------------------- S9-T3 扩展

S9_ADAPTER_NAMES = frozenset(
    {"locomo-adapter", "bird-adapter", "promptfoo-adapter", "inspect-adapter"}
)


class TestS9AdapterArchive:
    """specs/s9 §3：BIRD/LongMemEval/LoCoMo/Promptfoo/Inspect 分开登记、分开报告。

    清单仍是「已审查档案库」：登记只喂给确定性本地探测，未登记路径在别处不被阻断。
    """

    def test_s9_adapters_are_registered(self) -> None:
        assert S9_ADAPTER_NAMES <= EXTERNAL_ADAPTER_NAMES

    def test_every_registered_adapter_resolves_from_manifest(self) -> None:
        for spec in load_adapter_manifest():
            assert resolve_external_adapter(spec.name) == spec

    def test_s9_adapters_probe_unavailable_at_repo_root(self) -> None:
        # 仓库不含任何外部基准数据（许可约束）→ 五个 adapter 在仓库根全部 unavailable，
        # 且理由机器可读：code + 期望的仓库相对路径，绝不静默下载。
        for name in sorted(S9_ADAPTER_NAMES | {"longmemeval-adapter"}):
            spec = resolve_external_adapter(name)
            probe = probe_adapter(spec)
            assert probe.status == "unavailable"
            codes = {reason["code"] for reason in probe.reasons}
            assert codes and codes <= {"missing_file", "missing_data_dir", "no_data_files"}
            for reason in probe.reasons:
                assert reason["path"].startswith("evals/external/")

    def test_s9_probe_is_deterministic(self) -> None:
        for name in sorted(S9_ADAPTER_NAMES):
            first = probe_adapter(resolve_external_adapter(name))
            second = probe_adapter(resolve_external_adapter(name))
            assert first == second


class TestPerAdapterModules:
    """specs/s9 §2：adapter 模块化。模块只封装清单 spec + 确定性探测/完整性执行。"""

    @pytest.mark.parametrize(
        ("module_name", "constant", "adapter", "probe_func"),
        [
            ("longmemeval", "LONGMEMEVAL_ADAPTER", "longmemeval-adapter", "probe_longmemeval"),
            ("locomo", "LOCOMO_ADAPTER", "locomo-adapter", "probe_locomo"),
            ("bird", "BIRD_ADAPTER", "bird-adapter", "probe_bird"),
            ("promptfoo", "PROMPTFOO_ADAPTER", "promptfoo-adapter", "probe_promptfoo"),
            ("inspect", "INSPECT_ADAPTER", "inspect-adapter", "probe_inspect"),
        ],
    )
    def test_module_wraps_manifest_spec(
        self, module_name: str, constant: str, adapter: str, probe_func: str
    ) -> None:
        import importlib

        module = importlib.import_module(f"zhiwei.evals.external.{module_name}")
        assert getattr(module, constant) == adapter
        # 模块探测与清单机制探测等价：同一 spec、同一确定性结果。
        assert getattr(module, probe_func)() == probe_adapter(resolve_external_adapter(adapter))

    def test_longmemeval_module_exposes_probe_and_run(self) -> None:
        from zhiwei.evals.external import longmemeval

        with pytest.raises(RuntimeError, match="unavailable"):
            longmemeval.run_longmemeval_integrity()

    def test_locomo_module_exposes_probe(self) -> None:
        from zhiwei.evals.external import locomo

        assert locomo.probe_locomo() == probe_adapter(resolve_external_adapter("locomo-adapter"))

    def test_bird_module_exposes_probe(self) -> None:
        from zhiwei.evals.external import bird

        assert bird.probe_bird() == probe_adapter(resolve_external_adapter("bird-adapter"))


class TestAdapterModulesNoNetwork:
    """适配器探测绝不静默下载：诊断模块不得引入任何网络导入面。"""

    @pytest.mark.parametrize(
        "module_name",
        ["longmemeval", "locomo", "bird", "promptfoo", "inspect", "holdout", "metamorphic"],
    )
    def test_no_network_imports(self, module_name: str) -> None:
        import importlib
        import inspect as stdlib_inspect
        import re

        module = importlib.import_module(f"zhiwei.evals.external.{module_name}")
        source = stdlib_inspect.getsource(module)
        network_import = re.compile(
            r"^\s*(?:import|from)\s+(urllib|requests|httpx|socket|http\.client|aiohttp)\b",
            re.MULTILINE,
        )
        assert network_import.search(source) is None


NORMALIZED_ROWS = (
    {"case_id": "c-1", "input": "q-1", "expected": "a-1"},
    {"case_id": "c-2", "input": "q-2", "expected": "a-2"},
)


class TestPromptfooTransform:
    """Promptfoo 适配器可本地测试的部分：normalized case → 原生 tests 结构（纯函数）。"""

    def test_emits_promptfoo_native_cases(self) -> None:
        from zhiwei.evals.external.promptfoo import promptfoo_cases_from_normalized

        cases = promptfoo_cases_from_normalized(NORMALIZED_ROWS)
        assert [case["description"] for case in cases] == ["c-1", "c-2"]
        assert cases[0]["vars"] == {"input": "q-1"}
        assert cases[0]["assert"] == [{"type": "equals", "value": "a-1"}]

    def test_transform_is_deterministic_and_order_preserving(self) -> None:
        from zhiwei.evals.external.promptfoo import promptfoo_cases_from_normalized

        first = promptfoo_cases_from_normalized(NORMALIZED_ROWS)
        second = promptfoo_cases_from_normalized(NORMALIZED_ROWS)
        assert canonical_json(list(first)) == canonical_json(list(second))

    def test_fails_closed_on_unknown_fields(self) -> None:
        from zhiwei.evals.external.promptfoo import promptfoo_cases_from_normalized

        with pytest.raises(ValueError, match="未声明字段"):
            promptfoo_cases_from_normalized(
                [{"case_id": "c", "input": "q", "expected": "a", "surprise": 1}]
            )

    def test_fails_closed_on_missing_fields(self) -> None:
        from zhiwei.evals.external.promptfoo import promptfoo_cases_from_normalized

        with pytest.raises(ValueError, match="缺少必需字段"):
            promptfoo_cases_from_normalized([{"case_id": "c", "input": "q"}])

    def test_fails_closed_on_non_object_row(self) -> None:
        from zhiwei.evals.external.promptfoo import promptfoo_cases_from_normalized

        with pytest.raises(ValueError, match="JSON object"):
            promptfoo_cases_from_normalized(["not-a-dict"])  # type: ignore[list-item]

    def test_reads_normalized_jsonl_fixture_file(self, tmp_path: Path) -> None:
        from zhiwei.evals.external.promptfoo import promptfoo_cases_from_file

        path = tmp_path / "normalized.jsonl"
        lines = [json.dumps(row, ensure_ascii=False) for row in NORMALIZED_ROWS]
        path.write_text("\n".join(lines) + "\n\n", encoding="utf-8")
        cases = promptfoo_cases_from_file(path)
        assert [case["description"] for case in cases] == ["c-1", "c-2"]

    def test_file_parse_errors_are_located(self, tmp_path: Path) -> None:
        from zhiwei.evals.external.promptfoo import promptfoo_cases_from_file

        path = tmp_path / "normalized.jsonl"
        path.write_text("{broken\n", encoding="utf-8")
        with pytest.raises(ValueError, match=r"normalized\.jsonl:1"):
            promptfoo_cases_from_file(path)


class TestInspectTransform:
    """Inspect 适配器可本地测试的部分：normalized case → 原生 sample 结构（纯函数）。"""

    def test_emits_inspect_native_samples(self) -> None:
        from zhiwei.evals.external.inspect import inspect_samples_from_normalized

        samples = inspect_samples_from_normalized(NORMALIZED_ROWS)
        assert samples[0] == {"id": "c-1", "input": "q-1", "target": "a-1"}
        assert [sample["id"] for sample in samples] == ["c-1", "c-2"]

    def test_transform_is_deterministic(self) -> None:
        from zhiwei.evals.external.inspect import inspect_samples_from_normalized

        first = inspect_samples_from_normalized(NORMALIZED_ROWS)
        second = inspect_samples_from_normalized(NORMALIZED_ROWS)
        assert canonical_json(list(first)) == canonical_json(list(second))

    def test_fails_closed_on_unknown_fields(self) -> None:
        from zhiwei.evals.external.inspect import inspect_samples_from_normalized

        with pytest.raises(ValueError, match="未声明字段"):
            inspect_samples_from_normalized(
                [{"case_id": "c", "input": "q", "expected": "a", "extra": None}]
            )

    def test_reads_normalized_jsonl_fixture_file(self, tmp_path: Path) -> None:
        from zhiwei.evals.external.inspect import inspect_samples_from_file

        path = tmp_path / "normalized.jsonl"
        lines = [json.dumps(row, ensure_ascii=False) for row in NORMALIZED_ROWS]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        samples = inspect_samples_from_file(path)
        assert samples == (
            {"id": "c-1", "input": "q-1", "target": "a-1"},
            {"id": "c-2", "input": "q-2", "target": "a-2"},
        )


class TestScopeLabeling:
    """specs/s9 §3：external 产物一律带 external_diagnostic scope 标签。"""

    def test_probe_carries_external_scope(self, fixture_adapter: tuple[ExternalAdapterSpec, Path]) -> None:
        spec, root = fixture_adapter
        probe = probe_adapter(spec, root=root)
        assert probe.scope == "external_diagnostic:fixture-adapter"

    def test_integrity_run_carries_external_scope(
        self, fixture_adapter: tuple[ExternalAdapterSpec, Path]
    ) -> None:
        spec, root = fixture_adapter
        _materialize_available(root)
        probe = probe_adapter(spec, root=root)
        result = run_available_adapter(spec, probe, root=root)
        assert result["scope"] == "external_diagnostic:fixture-adapter"
