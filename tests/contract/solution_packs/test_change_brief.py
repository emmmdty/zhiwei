"""S10-T5 CONTRACT：ChangeBrief SolutionPack 声明 + 通用 pack 文件 conformance。

契约（specs/s10 §4/§6、plan Task 5）：
- load_pack_dir 解析 pack.yaml/agent.yaml/task_graph.yaml + 可选 skills/schemas/
  views/evals 声明目录，frozen 模型、未知键拒绝；
- validate_pack_bundle 产出 PackConformanceIssue：unknown_primitive /
  path_escape / skill_entry_missing / unresolved_ref / id_mismatch；
- 篡改面（未知 top-level 键、重复 id、版本不匹配、digest 不匹配）一律 LOAD 期
  PackFileError，不静默跳过（fail closed）；
- 机制必须 pack 无关：同一套 load+validate 对既有 pack 同样零 issue。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from zhiwei.agents.pack_files import (
    PackFileBundle,
    PackFileError,
    load_pack_dir,
    validate_pack_bundle,
)

from zhiwei.agents.task_graph import TaskPrimitive
from zhiwei.contracts.canonical import canonical_json, digest_bytes

REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = REPO_ROOT / "solution-packs" / "change-brief"
ASK_DIR = REPO_ROOT / "solution-packs" / "ask"


def _copy_pack(tmp_path: Path, source: Path = PACK_DIR) -> Path:
    target = tmp_path / source.name
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        if "__pycache__" in item.parts:
            continue
        relative = item.relative_to(source)
        if item.is_dir():
            (target / relative).mkdir(parents=True, exist_ok=True)
        else:
            (target / relative).write_bytes(item.read_bytes())
    return target


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"fixture expected mapping root: {path}"
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def _rewrite_yaml(
    path: Path,
    mutate: Callable[[dict[str, Any]], None],
    *,
    recompute_digest: bool = False,
) -> None:
    """按声明值语义改写 yaml 文件；pack.yaml 可选同步复算 digest。"""
    data = _load_yaml(path)
    mutate(data)
    if recompute_digest:
        data["content_digest"] = _repack_digest(data)
    _write_yaml(path, data)


def _digest_normalized(value: Any) -> Any:
    """digest 归一化契约：YAML date/datetime → ISO 字符串，其余值原样。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _digest_normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_digest_normalized(v) for v in value]
    return value


def _repack_digest(data: dict[str, Any]) -> str:
    """pack.yaml digest 契约：弹出 content_digest 后对剩余内容做 canonical JSON 摘要。"""
    content = {k: v for k, v in data.items() if k != "content_digest"}
    return digest_bytes(canonical_json(_digest_normalized(content)))


def _issue_codes(issues: tuple[Any, ...]) -> list[str]:
    return [issue.code for issue in issues]


class TestChangeBriefDeclarations:
    def test_bundle_shape(self) -> None:
        bundle = load_pack_dir(PACK_DIR)
        assert isinstance(bundle, PackFileBundle)
        assert bundle.pack.pack_id == "change-brief"
        assert bundle.pack.version == 1
        assert bundle.pack.frozen_at == date(2026, 9, 6)
        assert set(bundle.pack.capabilities) == {"knowledge.retrieve@1", "github.read@1"}
        assert bundle.pack.core_deps == ("reference-knowledge",)
        assert bundle.pack.source_connectors == ("github",)
        assert bundle.agent is not None
        assert bundle.agent.agent_id == "change-brief-agent-v1"
        assert bundle.agent.input.required == ("repository", "commit_or_pr")
        assert bundle.task_graph is not None
        assert bundle.task_graph.graph_id == "change-brief-task-graph-v1"
        assert bundle.task_graph.triggers[0].source == "github"
        assert bundle.task_graph.triggers[0].events == ("commit", "pull_request")
        assert [task.id for task in bundle.task_graph.tasks] == [
            "retrieve_code_knowledge",
            "analyze_impact",
            "verify_brief",
            "synthesize_brief",
            "emit_brief",
            "finish",
        ]
        assert len(bundle.skills) == 1
        assert bundle.skills[0].skill_id == "impact-analysis"
        assert bundle.skills[0].entry == "runtime/impact_analysis.py"
        assert [schema.schema_id for schema in bundle.schemas] == ["verified-brief"]

    def test_conformance_zero_issues(self) -> None:
        bundle = load_pack_dir(PACK_DIR)
        assert validate_pack_bundle(bundle, PACK_DIR) == ()

    def test_skill_entry_missing_is_empty_from_t6_onward(self) -> None:
        """skill entry 存在性自 T6 起必须为空 issue（T5 以 stub 满足）。"""
        bundle = load_pack_dir(PACK_DIR)
        issues = validate_pack_bundle(bundle, PACK_DIR)
        assert [issue for issue in issues if issue.code == "skill_entry_missing"] == []

    def test_pack_digest_matches_recomputed_value(self) -> None:
        data = _load_yaml(PACK_DIR / "pack.yaml")
        declared = data["content_digest"]
        assert declared.startswith("sha256:")
        assert declared == _repack_digest(data)

    def test_task_graph_uses_only_core_primitives(self) -> None:
        bundle = load_pack_dir(PACK_DIR)
        assert bundle.task_graph is not None
        primitive_names = {primitive.value for primitive in TaskPrimitive}
        task_types = {task.type for task in bundle.task_graph.tasks}
        assert task_types <= primitive_names
        assert task_types == set(bundle.pack.task_primitives)

    def test_views_declare_web_registry_renderer_refs(self) -> None:
        bundle = load_pack_dir(PACK_DIR)
        views = {
            (view.view_id, view.kind, view.app_id, view.schema_ref, view.renderer_ref)
            for view in bundle.views
        }
        assert views == {
            (
                "change-brief-input",
                "input",
                "change-brief",
                "verified-brief",
                "changeBrief/input",
            ),
            (
                "change-brief-result",
                "result",
                "change-brief",
                "verified-brief",
                "changeBrief/result",
            ),
        }

    def test_eval_declaration_points_at_corpus(self) -> None:
        bundle = load_pack_dir(PACK_DIR)
        assert len(bundle.evals) == 1
        declaration = bundle.evals[0]
        assert declaration.suite_id == "change-brief-v1"
        assert declaration.corpus_ref == "evals/change-brief/"
        assert declaration.registered_unit_count_hint == 6

    def test_verified_brief_schema_mirrors_evidence_ref_vocabulary(self) -> None:
        bundle = load_pack_dir(PACK_DIR)
        schema = bundle.schemas[0]
        properties = schema.schema_["properties"]
        assert set(properties) == {
            "affected_symbols",
            "affected_dependencies",
            "affected_tests",
            "related_prs",
            "related_issues",
            "related_checks",
            "risks",
            "unknowns",
            "code_refs",
            "github_refs",
        }
        # CodeRef 词汇：file_path/line_start/line_end/code_digest
        assert set(properties["code_refs"]["items"]["required"]) == {
            "file_path",
            "line_start",
            "line_end",
            "code_digest",
        }
        # GitHubRef 词汇：repository/commit_sha/path/line_start/line_end/pr_number
        assert set(properties["github_refs"]["items"]["properties"]) == {
            "repository",
            "commit_sha",
            "path",
            "line_start",
            "line_end",
            "pr_number",
        }

    def test_bundle_is_frozen(self) -> None:
        bundle = load_pack_dir(PACK_DIR)
        with pytest.raises(ValidationError):
            bundle.pack.pack_id = "tampered"  # type: ignore[misc]


class TestConformanceIssues:
    def test_unknown_primitive_in_task_graph(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "task_graph.yaml",
            lambda data: data["tasks"][0].__setitem__("type", "Teleport"),
        )
        bundle = load_pack_dir(pack)
        issues = validate_pack_bundle(bundle, pack)
        assert _issue_codes(issues) == ["unknown_primitive"]
        assert issues[0].location == "task_graph.yaml"

    def test_unknown_primitive_in_pack_declaration(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "pack.yaml",
            lambda data: data["task_primitives"].append("Teleport"),
            recompute_digest=True,
        )
        bundle = load_pack_dir(pack)
        issues = validate_pack_bundle(bundle, pack)
        assert _issue_codes(issues) == ["unknown_primitive"]
        assert issues[0].location == "pack.yaml"

    @pytest.mark.parametrize("entry", ["../evil.py", "/etc/passwd"])
    def test_skill_entry_escape(self, tmp_path: Path, entry: str) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "skills" / "impact-analysis.yaml",
            lambda data: data.__setitem__("entry", entry),
        )
        bundle = load_pack_dir(pack)
        issues = validate_pack_bundle(bundle, pack)
        assert _issue_codes(issues) == ["path_escape"]
        assert issues[0].location == "skills/impact-analysis"

    def test_skill_entry_missing(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "skills" / "impact-analysis.yaml",
            lambda data: data.__setitem__("entry", "runtime/missing.py"),
        )
        bundle = load_pack_dir(pack)
        issues = validate_pack_bundle(bundle, pack)
        assert _issue_codes(issues) == ["skill_entry_missing"]

    def test_view_unresolved_schema_ref(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "views" / "input.yaml",
            lambda data: data.__setitem__("schema_ref", "no-such-schema"),
        )
        bundle = load_pack_dir(pack)
        issues = validate_pack_bundle(bundle, pack)
        assert _issue_codes(issues) == ["unresolved_ref"]
        assert issues[0].location == "views/change-brief-input"

    def test_dangling_task_dependency(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "task_graph.yaml",
            lambda data: data["tasks"][1].__setitem__("depends_on", ["ghost_task"]),
        )
        bundle = load_pack_dir(pack)
        issues = validate_pack_bundle(bundle, pack)
        assert _issue_codes(issues) == ["unresolved_ref"]


class TestLoadFaultsClosed:
    def test_bad_content_digest_fails_at_load(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "pack.yaml",
            lambda data: data.__setitem__("content_digest", "sha256:" + "0" * 64),
        )
        with pytest.raises(PackFileError):
            load_pack_dir(pack)

    def test_unknown_top_level_key_fails_at_load(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "pack.yaml",
            lambda data: data.__setitem__("experimental_flag", True),
        )
        with pytest.raises(PackFileError):
            load_pack_dir(pack)

    def test_duplicate_skill_id_fails_at_load(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        duplicate = pack / "skills" / "impact-analysis-duplicate.yaml"
        duplicate.write_text(
            (pack / "skills" / "impact-analysis.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        with pytest.raises(PackFileError):
            load_pack_dir(pack)

    def test_duplicate_task_id_fails_at_load(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "task_graph.yaml",
            lambda data: data["tasks"][1].__setitem__("id", data["tasks"][0]["id"]),
        )
        with pytest.raises(PackFileError):
            load_pack_dir(pack)

    def test_unsupported_schema_version_fails_at_load(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "task_graph.yaml",
            lambda data: data.__setitem__("schema_version", 2),
        )
        with pytest.raises(PackFileError):
            load_pack_dir(pack)

    def test_version_mismatch_across_files_fails_at_load(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        _rewrite_yaml(
            pack / "agent.yaml",
            lambda data: data.__setitem__("version", 2),
        )
        with pytest.raises(PackFileError):
            load_pack_dir(pack)

    def test_missing_pack_yaml_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(PackFileError):
            load_pack_dir(tmp_path / "not-a-pack")

    def test_non_mapping_pack_root_fails_closed(self, tmp_path: Path) -> None:
        pack = _copy_pack(tmp_path)
        (pack / "pack.yaml").write_text("- one\n- two\n", encoding="utf-8")
        with pytest.raises(PackFileError):
            load_pack_dir(pack)


class TestGenericMachinery:
    def test_ask_pack_loads_and_conforms_with_same_machinery(self) -> None:
        bundle = load_pack_dir(ASK_DIR)
        assert bundle.pack.pack_id == "ask-v1"
        assert bundle.agent is not None
        assert bundle.agent.agent_id == "ask-agent-v1"
        assert bundle.task_graph is not None
        assert bundle.task_graph.graph_id == "ask-task-graph-v1"
        # ask 无可选声明目录：空 section 必须是空 tuple 而非错误
        assert bundle.skills == ()
        assert bundle.schemas == ()
        assert bundle.views == ()
        assert bundle.evals == ()
        assert validate_pack_bundle(bundle, ASK_DIR) == ()
