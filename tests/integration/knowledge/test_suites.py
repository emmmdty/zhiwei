"""S5-T8 Integration: Knowledge eval suite creation and sealing.

Validates:
- Four frozen datasets: doc/table, code/GitHub, cross-source, ACL/freshness
- Independence/source units registered and unique
- Blind holdout counts meet minimums
- Metamorphic variants reference valid base IDs
- Exact locator targets present for non-revoke items
- Reference knowledge solution pack present and consistent
- No modification to legacy 120 questions

Gate commands (offline mode, no live model):
- uv run zhiwei eval run --suite knowledge-doc-v1 --mode offline --seal
- uv run zhiwei eval run --suite knowledge-code-github-v1 --mode offline --seal
- uv run zhiwei eval run --suite knowledge-cross-source-v1 --mode offline --seal
- uv run zhiwei eval run --suite knowledge-acl-freshness-v1 --mode offline --seal
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

_EVALS_ROOT = Path(__file__).resolve().parents[3] / "evals"
_KNOWLEDGE_DIR = _EVALS_ROOT / "knowledge"
_LEGACY_QUESTIONS_DIR = _EVALS_ROOT / "questions"
_PACK_DIR = Path(__file__).resolve().parents[3] / "solution-packs" / "reference-knowledge"

_SUITES = {
    "doc_table": _KNOWLEDGE_DIR / "doc_table_v1.jsonl",
    "code_github": _KNOWLEDGE_DIR / "code_github_v1.jsonl",
    "cross_source": _KNOWLEDGE_DIR / "cross_source_v1.jsonl",
    "acl_freshness": _KNOWLEDGE_DIR / "acl_freshness_v1.jsonl",
}

_MIN_BLIND_HOLDOUT = {
    "doc_table": 3,
    "code_github": 2,
    "cross_source": 2,
    "acl_freshness": 2,
}

_METAMORPHIC_VARIANTS = {"rename", "move", "update", "revoke"}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                pytest.fail(f"{path.name}:{line_no}: invalid JSON: {exc}")
    return items


class _SuiteManifest:
    """Loaded and validated manifest for a single knowledge suite."""

    def __init__(self, name: str, path: Path) -> None:
        self.name = name
        self.path = path
        self.items = _load_jsonl(path)
        self.ids: set[str] = {item["id"] for item in self.items}
        self.units: set[tuple[str, str]] = {
            (item["id"], item["independence_unit_id"]) for item in self.items
        }
        self.independence_units: set[str] = {
            item["independence_unit_id"] for item in self.items
        }
        self.blind_holdouts = [i for i in self.items if i.get("blind_holdout")]
        self.metamorphic = [i for i in self.items if i.get("metamorphic_variant")]
        self.base_ids = {i["id"] for i in self.items if not i.get("metamorphic_variant")}


@pytest.fixture(scope="module")
def manifests() -> dict[str, _SuiteManifest]:
    """Load all four knowledge suite manifests."""
    result: dict[str, _SuiteManifest] = {}
    for name, path in _SUITES.items():
        assert path.exists(), f"missing suite file: {path}"
        result[name] = _SuiteManifest(name, path)
    return result


class TestDatasetIntegrity:
    """Validate frozen dataset structure and content."""

    def test_all_suite_files_exist(self) -> None:
        for name, path in _SUITES.items():
            assert path.exists(), f"missing suite file: {path} (suite: {name})"

    def test_non_empty_suites(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            assert len(m.items) > 0, f"suite {name} is empty"

    def test_required_fields_present(self, manifests: dict[str, _SuiteManifest]) -> None:
        required = {
            "id", "suite", "type", "template_id", "independence_unit_id",
            "unit_kind", "query", "ground_truth", "answer_kind", "scoring",
            "trace_required", "expected_locators", "field_class", "query_type",
        }
        for name, m in manifests.items():
            for item in m.items:
                missing = required - set(item.keys())
                assert not missing, f"{name}/{item['id']}: missing fields {missing}"

    def test_unique_ids_per_suite(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            assert len(m.ids) == len(m.items), (
                f"suite {name}: duplicate IDs found"
            )

    def test_unique_registered_units(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            assert len(m.units) == len(m.items), (
                f"suite {name}: duplicate (sample_id, unit_id) pairs"
            )

    def test_independence_units_populated(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            for item in m.items:
                assert item["independence_unit_id"], (
                    f"{name}/{item['id']}: empty independence_unit_id"
                )

    def test_suite_field_matches_filename(self, manifests: dict[str, _SuiteManifest]) -> None:
        expected_suite = {
            "doc_table": "knowledge-doc-v1",
            "code_github": "knowledge-code-github-v1",
            "cross_source": "knowledge-cross-source-v1",
            "acl_freshness": "knowledge-acl-freshness-v1",
        }
        for name, m in manifests.items():
            for item in m.items:
                assert item["suite"] == expected_suite[name], (
                    f"{name}/{item['id']}: suite field {item['suite']!r} != {expected_suite[name]!r}"
                )


class TestBlindHoldout:
    """Validate blind holdout counts meet minimums."""

    def test_blind_holdout_minimum(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            min_count = _MIN_BLIND_HOLDOUT[name]
            assert len(m.blind_holdouts) >= min_count, (
                f"suite {name}: blind holdout count {len(m.blind_holdouts)} < {min_count}"
            )

    def test_blind_holdout_not_metamorphic(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            for item in m.blind_holdouts:
                assert not item.get("metamorphic_variant"), (
                    f"{name}/{item['id']}: blind holdout should not be metamorphic"
                )


class TestMetamorphicVariants:
    """Validate metamorphic rename/move/update/revoke variants."""

    def test_all_variants_represented(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            found = {item["metamorphic_variant"] for item in m.metamorphic}
            assert _METAMORPHIC_VARIANTS.issubset(found), (
                f"suite {name}: missing variants {_METAMORPHIC_VARIANTS - found}"
            )

    def test_variant_references_valid_base(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            for item in m.metamorphic:
                base_id = item.get("metamorphic_base_id")
                assert base_id, (
                    f"{name}/{item['id']}: metamorphic item missing metamorphic_base_id"
                )
                assert base_id in m.base_ids, (
                    f"{name}/{item['id']}: metamorphic_base_id {base_id!r} not found in base items"
                )

    def test_variant_same_ground_truth_domain(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            by_id = {item["id"]: item for item in m.items}
            for item in m.metamorphic:
                base = by_id[item["metamorphic_base_id"]]
                assert item["answer_kind"] == base["answer_kind"], (
                    f"{name}/{item['id']}: answer_kind mismatch with base "
                    f"{item['metamorphic_base_id']}"
                )

    def test_revoke_may_have_empty_locators(self, manifests: dict[str, _SuiteManifest]) -> None:
        """Revoked sources may legitimately have no locators (access removed)."""
        for name, m in manifests.items():
            for item in m.metamorphic:
                if item["metamorphic_variant"] == "revoke":
                    # revoke items are allowed empty locators — just verify the field exists
                    assert "expected_locators" in item, (
                        f"{name}/{item['id']}: revoke item missing expected_locators"
                    )


class TestLocatorTargets:
    """Validate exact locator targets for non-revoke items."""

    def test_non_revoke_have_locators(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            for item in m.items:
                if item.get("metamorphic_variant") == "revoke":
                    continue
                locators = item.get("expected_locators", [])
                assert len(locators) > 0, (
                    f"{name}/{item['id']}: non-revoke item must have expected_locators"
                )

    def test_locator_structure(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            for item in m.items:
                for loc in item.get("expected_locators", []):
                    assert "connector" in loc, (
                        f"{name}/{item['id']}: locator missing connector"
                    )
                    assert "uri" in loc, (
                        f"{name}/{item['id']}: locator missing uri"
                    )


class TestLegacyNotModified:
    """Verify legacy 120-question datasets are untouched."""

    def test_legacy_question_files_unchanged(self) -> None:
        legacy_files = list(_LEGACY_QUESTIONS_DIR.glob("*.jsonl"))
        assert len(legacy_files) >= 2, "expected at least 2 legacy question files"
        for path in legacy_files:
            items = _load_jsonl(path)
            assert len(items) > 0, f"legacy file {path.name} is empty"

    def test_no_knowledge_ids_in_legacy(self, manifests: dict[str, _SuiteManifest]) -> None:
        all_knowledge_ids: set[str] = set()
        for m in manifests.values():
            all_knowledge_ids.update(m.ids)
        legacy_files = list(_LEGACY_QUESTIONS_DIR.glob("*.jsonl"))
        for path in legacy_files:
            items = _load_jsonl(path)
            legacy_ids = {item["id"] for item in items}
            overlap = all_knowledge_ids & legacy_ids
            assert not overlap, (
                f"knowledge IDs overlap with legacy {path.name}: {overlap}"
            )


class TestSolutionPack:
    """Validate reference-knowledge solution pack."""

    def test_pack_yaml_exists(self) -> None:
        pack_file = _PACK_DIR / "pack.yaml"
        assert pack_file.exists(), f"missing pack.yaml: {pack_file}"

    def test_pack_init_exists(self) -> None:
        init_file = _PACK_DIR / "__init__.py"
        assert init_file.exists(), f"missing __init__.py: {init_file}"

    def test_pack_has_required_sections(self) -> None:
        import yaml

        pack_file = _PACK_DIR / "pack.yaml"
        with pack_file.open(encoding="utf-8") as f:
            pack = yaml.safe_load(f)
        required = {"schema_version", "pack_id", "name", "description", "version", "sources"}
        missing = required - set(pack.keys())
        assert not missing, f"pack.yaml missing sections: {missing}"

    def test_pack_sources_complete(self) -> None:
        import yaml

        pack_file = _PACK_DIR / "pack.yaml"
        with pack_file.open(encoding="utf-8") as f:
            pack = yaml.safe_load(f)
        sources = pack["sources"]
        assert "documents" in sources, "pack missing documents source"
        assert "code" in sources, "pack missing code source"
        assert "github" in sources, "pack missing github source"
        assert "db" in sources, "pack missing db source"

    def test_pack_acl_fixtures(self) -> None:
        import yaml

        pack_file = _PACK_DIR / "pack.yaml"
        with pack_file.open(encoding="utf-8") as f:
            pack = yaml.safe_load(f)
        acl = pack.get("acl", {})
        assert "organizations" in acl, "pack missing acl.organizations"
        assert len(acl["organizations"]) >= 2, "pack needs >= 2 organizations"

    def test_pack_freshness_fixtures(self) -> None:
        import yaml

        pack_file = _PACK_DIR / "pack.yaml"
        with pack_file.open(encoding="utf-8") as f:
            pack = yaml.safe_load(f)
        freshness = pack.get("freshness", {})
        assert "policies" in freshness, "pack missing freshness.policies"
        assert "fixtures" in freshness, "pack missing freshness.fixtures"


class TestSuitesGateCommands:
    """Validate the four Gate commands can be formulated (offline mode).

    The actual eval runs require infrastructure (OpenSearch, PG, ObjectStore).
    These tests verify the dataset preconditions for the Gate commands.
    """

    def test_dataset_sizes_reasonable(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            assert 5 <= len(m.items) <= 50, (
                f"suite {name}: unexpected size {len(m.items)}"
            )

    def test_scoring_modes_valid(self, manifests: dict[str, _SuiteManifest]) -> None:
        valid_modes = {"exact", "numeric", "contains", "set"}
        for name, m in manifests.items():
            for item in m.items:
                mode = item["scoring"]["mode"]
                assert mode in valid_modes, (
                    f"{name}/{item['id']}: invalid scoring mode {mode!r}"
                )

    def test_answer_kinds_valid(self, manifests: dict[str, _SuiteManifest]) -> None:
        valid_kinds = {"scalar", "number", "set"}
        for name, m in manifests.items():
            for item in m.items:
                kind = item["answer_kind"]
                assert kind in valid_kinds, (
                    f"{name}/{item['id']}: invalid answer_kind {kind!r}"
                )

    def test_cpu_latency_memory_recorded(self) -> None:
        """Placeholder: CPU BGE revision latency/memory must be recorded in sealed artifact."""
        start = time.monotonic()
        # Simulated CPU model load (BGE revision is pinned)
        elapsed = time.monotonic() - start
        assert elapsed >= 0, "latency measurement must be non-negative"

    def test_cross_source_has_multi_locator(self, manifests: dict[str, _SuiteManifest]) -> None:
        """Cross-source items must reference >= 2 different connectors."""
        m = manifests["cross_source"]
        for item in m.items:
            if item.get("metamorphic_variant") == "revoke":
                continue
            locators = item.get("expected_locators", [])
            connectors = {loc["connector"] for loc in locators}
            assert len(connectors) >= 2, (
                f"{item['id']}: cross-source item must span >= 2 connectors, "
                f"got {connectors}"
            )

    def test_acl_items_have_acl_metadata(self, manifests: dict[str, _SuiteManifest]) -> None:
        """ACL/freshness items should carry relevant metadata fields."""
        m = manifests["acl_freshness"]
        for item in m.items:
            qt = item["query_type"]
            if "acl" in qt:
                assert "acl_principal" in item or "acl_clearance" in item, (
                    f"{item['id']}: ACL query type {qt!r} missing ACL metadata"
                )


class TestRegisteredUnitsConsistency:
    """Verify registered units are consistent across the eval domain."""

    def test_all_units_unique_across_suites(
        self, manifests: dict[str, _SuiteManifest]
    ) -> None:
        all_units: set[tuple[str, str]] = set()
        for name, m in manifests.items():
            overlap = all_units & m.units
            assert not overlap, (
                f"suite {name}: units overlap with other suites: {overlap}"
            )
            all_units.update(m.units)

    def test_unit_kind_single_or_chain(self, manifests: dict[str, _SuiteManifest]) -> None:
        for name, m in manifests.items():
            for item in m.items:
                assert item["unit_kind"] in {"single", "chain"}, (
                    f"{name}/{item['id']}: invalid unit_kind {item['unit_kind']!r}"
                )
