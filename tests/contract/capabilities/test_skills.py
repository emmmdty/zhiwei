"""S4-T5 contract: Agent Skills and SDK provider tests."""

from __future__ import annotations

from typing import Any

import pytest

from zhiwei.capabilities.sdk import (
    InMemorySDKProvider,
    SDKAuthMethod,
    SDKCapability,
    SDKHealthResult,
    SDKHealthStatus,
    SDKInvokeResult,
    SDKProviderPort,
)
from zhiwei.capabilities.skills.package import (
    SkillPackage,
    SkillPackageError,
    SkillPackageLoader,
)
from zhiwei.capabilities.skills.projection import (
    SkillProjectionError,
    SkillProjector,
)
from zhiwei.capabilities.skills.script_tool import (
    OCIImage,
    ScriptToolBuilder,
    ScriptToolError,
)
from zhiwei.capabilities.skills.validator import (
    SkillValidationResult,
    SkillValidator,
)

# ---------------------------------------------------------------------------
# Skill Package tests
# ---------------------------------------------------------------------------


class TestSkillPackage:
    def test_load_from_metadata(self) -> None:
        loader = SkillPackageLoader()
        pkg = loader.load_from_metadata(
            "test-skill",
            {
                "version": "1.0.0",
                "description": "A test skill",
                "publisher": "test-org",
                "allowedTools": ["search", "write"],
                "assets": [
                    {"name": "readme.md", "type": "document", "size_bytes": 1024}
                ],
            },
        )
        assert pkg.name == "test-skill"
        assert pkg.version == "1.0.0"
        assert pkg.allowed_tools == ("search", "write")
        assert len(pkg.assets) == 1

    def test_load_empty_name_raises(self) -> None:
        loader = SkillPackageLoader()
        with pytest.raises(SkillPackageError, match="name"):
            loader.load_from_metadata("", {})

    def test_load_from_dict(self) -> None:
        loader = SkillPackageLoader()
        pkg = loader.load_from_dict(
            {"name": "skill-a", "version": "2.0.0", "source_url": "https://example.com"}
        )
        assert pkg.name == "skill-a"
        assert pkg.source_url == "https://example.com"

    def test_content_digest_computed(self) -> None:
        loader = SkillPackageLoader()
        pkg = loader.load_from_metadata("s", {"content": {"key": "value"}})
        assert pkg.content_digest.startswith("sha256:")

    def test_content_digest_deterministic(self) -> None:
        loader = SkillPackageLoader()
        p1 = loader.load_from_metadata("s", {"content": {"a": 1}})
        p2 = loader.load_from_metadata("s", {"content": {"a": 1}})
        assert p1.content_digest == p2.content_digest

    def test_frozen(self) -> None:
        loader = SkillPackageLoader()
        pkg = loader.load_from_metadata("s", {})
        with pytest.raises(Exception, match="frozen"):
            pkg.name = "other"  # type: ignore[misc]

    def test_assets_parsed(self) -> None:
        loader = SkillPackageLoader()
        pkg = loader.load_from_metadata(
            "s",
            {
                "assets": [
                    {"name": "a.py", "type": "script", "content_digest": "sha256:abc", "url": "https://x.com/a.py"},
                    {"name": "b.json", "type": "config"},
                ]
            },
        )
        assert len(pkg.assets) == 2
        assert pkg.assets[0].name == "a.py"
        assert pkg.assets[0].asset_type == "script"

    def test_invalid_assets_skipped(self) -> None:
        loader = SkillPackageLoader()
        pkg = loader.load_from_metadata(
            "s",
            {"assets": [{"no_name": True}, "not-a-dict", {"name": "x", "type": "y"}]},
        )
        assert len(pkg.assets) == 1

    def test_metadata_preserved(self) -> None:
        loader = SkillPackageLoader()
        pkg = loader.load_from_metadata(
            "s",
            {"custom_key": "custom_val", "version": "1.0.0"},
        )
        assert pkg.metadata.get("custom_key") == "custom_val"
        assert "version" not in pkg.metadata


# ---------------------------------------------------------------------------
# Skill Validator tests
# ---------------------------------------------------------------------------


class TestSkillValidator:
    def test_valid_package(self) -> None:
        loader = SkillPackageLoader()
        pkg = loader.load_from_metadata("test", {"version": "1.0.0"})
        validator = SkillValidator()
        result = validator.validate(pkg)
        assert result.valid is True

    def test_empty_name_invalid(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="at least 1 character"):
            SkillPackage(name="", version="1.0.0")

    def test_html_sanitization(self) -> None:
        loader = SkillPackageLoader()
        pkg = loader.load_from_metadata(
            "s",
            {"description": "<script>alert('xss')</script>Hello <b>world</b>"},
        )
        validator = SkillValidator()
        result = validator.validate(pkg)
        assert "<script>" not in result.sanitized_description
        assert "<b>" not in result.sanitized_description
        assert "Hello" in result.sanitized_description

    def test_metadata_html_sanitization(self) -> None:
        validator = SkillValidator()
        result = validator.validate(
            SkillPackage(
                name="s",
                metadata={"note": "<img src=x onerror=alert(1)>safe</img>"},
            )
        )
        assert "<img" not in result.sanitized_metadata["note"]
        assert "safe" in result.sanitized_metadata["note"]

    def test_allowed_tools_narrowing_valid(self) -> None:
        validator = SkillValidator()
        result = validator.validate(
            SkillPackage(name="s", allowed_tools=("search",)),
            parent_allowed_tools=("search", "write", "read"),
        )
        assert result.valid is True

    def test_allowed_tools_widening_rejected(self) -> None:
        validator = SkillValidator()
        result = validator.validate(
            SkillPackage(name="s", allowed_tools=("search", "delete")),
            parent_allowed_tools=("search", "write"),
        )
        assert result.valid is False
        assert any("narrow" in e.lower() for e in result.errors)

    def test_allowed_tools_empty_child_ok(self) -> None:
        validator = SkillValidator()
        result = validator.validate(
            SkillPackage(name="s", allowed_tools=()),
            parent_allowed_tools=("search", "write"),
        )
        assert result.valid is True

    def test_script_safety_clean(self) -> None:
        validator = SkillValidator()
        result = validator.validate_script_safety("print('hello world')")
        assert result.valid is True

    def test_script_safety_dangerous(self) -> None:
        validator = SkillValidator()
        result = validator.validate_script_safety("import subprocess; subprocess.call('rm -rf /')")
        assert result.valid is False
        assert any("subprocess" in e for e in result.errors)

    def test_script_safety_eval(self) -> None:
        validator = SkillValidator()
        result = validator.validate_script_safety("eval(user_input)")
        assert result.valid is False
        assert any("eval(" in e for e in result.errors)

    def test_frozen_result(self) -> None:
        result = SkillValidationResult(valid=True)
        with pytest.raises(Exception, match="frozen"):
            result.valid = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Skill Projection tests
# ---------------------------------------------------------------------------


class TestSkillProjector:
    def test_project_creates_tool(self) -> None:
        projector = SkillProjector()
        proj = projector.project("my-skill", "1.0.0", description="A skill")
        assert proj.skill_name == "my-skill"
        assert proj.skill_version == "1.0.0"
        assert proj.tool_definition.tool_name == "skill_my-skill"
        assert proj.tool_definition.tool_type == "skill"

    def test_project_empty_name_raises(self) -> None:
        projector = SkillProjector()
        with pytest.raises(SkillProjectionError, match="name"):
            projector.project("", "1.0.0")

    def test_project_from_content(self) -> None:
        projector = SkillProjector()
        content = {
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
            "outputSchema": {"type": "object"},
            "description": "Search skill",
        }
        proj = projector.project_from_content("search", "2.0.0", content)
        assert proj.tool_definition.input_schema == content["inputSchema"]
        assert proj.tool_definition.description == "Search skill"

    def test_allowed_tools_forwarded(self) -> None:
        projector = SkillProjector()
        proj = projector.project("s", "1.0.0", allowed_tools=("a", "b"))
        assert proj.allowed_tools == ("a", "b")

    def test_frozen(self) -> None:
        projector = SkillProjector()
        proj = projector.project("s", "1.0.0")
        with pytest.raises(Exception, match="frozen"):
            proj.skill_name = "other"  # type: ignore[misc]

    def test_metadata_forwarded(self) -> None:
        projector = SkillProjector()
        proj = projector.project("s", "1.0.0", metadata={"key": "val"})
        assert proj.projection_metadata["key"] == "val"


# ---------------------------------------------------------------------------
# Script Tool tests
# ---------------------------------------------------------------------------


class TestScriptTool:
    def test_build_basic(self) -> None:
        builder = ScriptToolBuilder()
        tool = builder.build("my-script", "print('hello')")
        assert tool.name == "my-script"
        assert tool.script_digest.startswith("sha256:")
        assert tool.runtime == "python3.11"

    def test_build_empty_name_raises(self) -> None:
        builder = ScriptToolBuilder()
        with pytest.raises(ScriptToolError, match="name"):
            builder.build("", "code")

    def test_build_empty_script_raises(self) -> None:
        builder = ScriptToolBuilder()
        with pytest.raises(ScriptToolError, match="content"):
            builder.build("name", "")

    def test_dependencies_frozen(self) -> None:
        builder = ScriptToolBuilder()
        tool = builder.build(
            "s",
            "code",
            dependencies=[
                {"name": "requests", "version": "2.31.0", "digest": "sha256:abc"},
                {"name": "pydantic", "version": "2.7.0"},
            ],
        )
        assert len(tool.dependencies) == 2
        assert tool.dependencies[0].name == "requests"
        assert tool.dependencies[0].digest == "sha256:abc"
        assert tool.dependencies[1].name == "pydantic"

    def test_sbom_frozen(self) -> None:
        builder = ScriptToolBuilder()
        tool = builder.build(
            "s",
            "code",
            sbom=[
                {"name": "pkg-a", "version": "1.0", "license": "MIT"},
            ],
        )
        assert len(tool.sbom) == 1
        assert tool.sbom[0].license == "MIT"

    def test_oci_image_frozen(self) -> None:
        builder = ScriptToolBuilder()
        tool = builder.build(
            "s",
            "code",
            oci_image={
                "registry": "ghcr.io",
                "repository": "org/image",
                "digest": "sha256:" + "a" * 64,
                "tag": "v1.0",
            },
        )
        assert tool.oci_image is not None
        assert tool.oci_image.registry == "ghcr.io"
        assert tool.oci_image.digest.startswith("sha256:")

    def test_oci_digest_validation(self) -> None:
        with pytest.raises(ValueError, match="sha256"):
            OCIImage(
                registry="ghcr.io",
                repository="org/image",
                digest="md5:abc123",
            )

    def test_script_digest_computed(self) -> None:
        builder = ScriptToolBuilder()
        t1 = builder.build("s", "code")
        t2 = builder.build("s", "code")
        assert t1.script_digest == t2.script_digest

    def test_script_digest_changes_with_content(self) -> None:
        builder = ScriptToolBuilder()
        t1 = builder.build("s", "code1")
        t2 = builder.build("s", "code2")
        assert t1.script_digest != t2.script_digest

    def test_to_tool_definition(self) -> None:
        builder = ScriptToolBuilder()
        tool = builder.build("my-script", "code", description="My script")
        td = builder.to_tool_definition(tool)
        assert td.tool_name == "my-script"
        assert td.tool_type == "script"
        assert td.description == "My script"
        assert "script_digest" in td.metadata
        assert "runtime" in td.metadata

    def test_frozen(self) -> None:
        builder = ScriptToolBuilder()
        tool = builder.build("s", "code")
        with pytest.raises(Exception, match="frozen"):
            tool.name = "other"  # type: ignore[misc]

    def test_network_access_flag(self) -> None:
        builder = ScriptToolBuilder()
        tool = builder.build("s", "code", network_access=True)
        assert tool.network_access is True


# ---------------------------------------------------------------------------
# SDK Provider tests
# ---------------------------------------------------------------------------


class TestSDKProvider:
    def test_discover(self) -> None:
        provider = InMemorySDKProvider(
            capabilities=[
                SDKCapability(name="search", description="Search tool"),
            ]
        )
        result = provider.discover()
        assert result.provider_name == "in-memory-provider"
        assert len(result.capabilities) == 1
        assert result.capabilities[0].name == "search"

    def test_invoke_default(self) -> None:
        provider = InMemorySDKProvider()
        result = provider.invoke("search", {"query": "test"})
        assert result.success is True
        assert result.output == {"echo": {"query": "test"}}

    def test_invoke_custom_handler(self) -> None:
        provider = InMemorySDKProvider()

        def handler(name: str, data: dict[str, Any], **kw: Any) -> SDKInvokeResult:
            return SDKInvokeResult(success=True, output={"handled": name})

        provider.set_invoke_handler(handler)
        result = provider.invoke("test", {})
        assert result.output == {"handled": "test"}

    def test_health_check(self) -> None:
        provider = InMemorySDKProvider()
        result = provider.health_check()
        assert result.status == SDKHealthStatus.HEALTHY

    def test_get_auth_methods(self) -> None:
        provider = InMemorySDKProvider(
            auth_methods=(SDKAuthMethod.API_KEY, SDKAuthMethod.BEARER_TOKEN)
        )
        methods = provider.get_auth_methods()
        assert SDKAuthMethod.API_KEY in methods
        assert SDKAuthMethod.BEARER_TOKEN in methods

    def test_is_sdk_provider_port(self) -> None:
        assert issubclass(InMemorySDKProvider, SDKProviderPort)

    def test_discover_frozen(self) -> None:
        provider = InMemorySDKProvider()
        result = provider.discover()
        with pytest.raises(Exception, match="frozen"):
            result.provider_name = "other"  # type: ignore[misc]

    def test_invoke_frozen(self) -> None:
        provider = InMemorySDKProvider()
        result = provider.invoke("x", {})
        with pytest.raises(Exception, match="frozen"):
            result.success = False  # type: ignore[misc]

    def test_health_frozen(self) -> None:
        result = SDKHealthResult(status=SDKHealthStatus.HEALTHY)
        with pytest.raises(Exception, match="frozen"):
            result.status = SDKHealthStatus.UNHEALTHY  # type: ignore[misc]

    def test_invoke_with_auth_context(self) -> None:
        provider = InMemorySDKProvider()

        def handler(name: str, data: dict[str, Any], **kw: Any) -> SDKInvokeResult:
            ctx = kw.get("auth_context")
            return SDKInvokeResult(
                success=True,
                output={"auth": ctx},
            )

        provider.set_invoke_handler(handler)
        result = provider.invoke("x", {}, auth_context={"token": "abc"})
        assert result.output["auth"]["token"] == "abc"
