"""S7-T7 RED: external adapter registry 与确定性可用性探测。

事实源：specs/s7-memory.md §8（external-status 二选一 sealed artifact 契约）、
ADR-013 决策 2（`eval external-status` 是真实产品能力）、S7 plan Task 7。

探测必须是确定性的本地检查：config/evals/ 登记的 adapter 清单 + 本地数据目录/
许可文件存在性；available 时附许可/version/checksum 并实际运行 adapter 的
完整性执行（离线确定性，不产生任何质量分数——那需要 live 模型）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
