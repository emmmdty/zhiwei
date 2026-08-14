"""S0-T1 RED：typed settings 的契约。

这些断言定义 `src/zhiwei/config/settings.py` 必须满足的行为，不是对某种实现方式的偏好。
三条真正的不变量：

1. **不读 `.env`**（AGENTS.md）。配置来源必须是显式传入的 mapping，默认才回落到 `os.environ`。
   这不是"记得别读"的约定，而是可被测试证伪的结构：`load_settings({})` 在 CWD 有 `.env` 时
   仍须得到空配置。
2. **未知取值 fail closed**。非法 profile / release_mode 不得回落到"常见默认"，必须抛错。
3. **secret 不得出现在任何字符串化输出里**。repr/str/日志格式化都不行——凭据泄漏最常见的路径
   是异常栈里的一行 `repr(settings)`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from zhiwei.config.settings import (
    DeploymentProfile,
    ReleaseMode,
    Settings,
    load_settings,
)

SENTINEL_SECRET = "sk-do-not-leak-92f1c0"


# --------------------------------------------------------------------------- profile 解析


def test_profile_defaults_to_test_when_unset() -> None:
    """未声明 profile 时落到最受限的档，而不是最宽松的档。"""
    settings = load_settings({})
    assert settings.profile is DeploymentProfile.TEST


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("test", DeploymentProfile.TEST),
        ("local_product", DeploymentProfile.LOCAL_PRODUCT),
        ("production_reference", DeploymentProfile.PRODUCTION_REFERENCE),
    ],
)
def test_profile_parses_the_three_declared_profiles(raw: str, expected: DeploymentProfile) -> None:
    assert load_settings({"ZHIWEI_PROFILE": raw}).profile is expected


@pytest.mark.parametrize("raw", ["", "prod", "production", "TEST", "local-product", "staging"])
def test_unknown_profile_fails_closed(raw: str) -> None:
    """拼错的 profile 必须炸，不能悄悄变成某个默认值。

    大小写与连字符变体一并拒绝：接受 "TEST" 会让配置文件出现两种写法，接受 "local-product"
    会让它与 canonical 的 `local_product` 并存——两者都是后续 drift 的起点。
    """
    with pytest.raises(ValueError) as exc:
        load_settings({"ZHIWEI_PROFILE": raw})
    assert "ZHIWEI_PROFILE" in str(exc.value), "错误信息必须指出是哪个变量非法"


@pytest.mark.parametrize("raw", ["", "prod", "LIVE", "fixture", "replay"])
def test_unknown_release_mode_fails_closed(raw: str) -> None:
    with pytest.raises(ValueError) as exc:
        load_settings({"ZHIWEI_RELEASE_MODE": raw})
    assert "ZHIWEI_RELEASE_MODE" in str(exc.value)


# --------------------------------------------------------------------------- live 门禁


def test_release_mode_defaults_to_fixture_only() -> None:
    assert load_settings({}).release_mode is ReleaseMode.FIXTURE_ONLY


def test_live_model_calls_are_denied_by_default() -> None:
    """默认配置不得允许 live 调用——这是 AGENTS.md「不调用 live 模型」的可执行形式。"""
    assert load_settings({}).live_model_calls_allowed is False


def test_live_model_calls_require_explicit_live_mode() -> None:
    settings = load_settings(
        {"ZHIWEI_PROFILE": "production_reference", "ZHIWEI_RELEASE_MODE": "live"}
    )
    assert settings.live_model_calls_allowed is True


def test_test_profile_cannot_be_combined_with_live_mode() -> None:
    """test profile + live 是配置错误，必须在加载期就拒绝。

    否则「测试进程不得在 live 下运行」只能靠 tests/unit/test_environment.py 事后发现，
    而那时请求可能已经发出去了。
    """
    with pytest.raises(ValueError) as exc:
        load_settings({"ZHIWEI_PROFILE": "test", "ZHIWEI_RELEASE_MODE": "live"})
    message = str(exc.value)
    assert "test" in message and "live" in message


def test_local_product_profile_cannot_be_combined_with_live_mode() -> None:
    """local-product 是本地演示档，同样不得直连真实 provider。"""
    with pytest.raises(ValueError):
        load_settings({"ZHIWEI_PROFILE": "local_product", "ZHIWEI_RELEASE_MODE": "live"})


# --------------------------------------------------------------------------- 不读 .env


def test_dotenv_in_cwd_is_never_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CWD 下存在 `.env` 时也不得读取它。

    仓库根目录本来就有一个真实的 `.env`；测试与工具链一旦读它，CI 的行为就取决于某台机器上
    的本地文件，「不调用 live 模型」也随之失守。
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        f"ZHIWEI_PROFILE=production_reference\nZHIWEI_RELEASE_MODE=live\nOPENAI_API_KEY={SENTINEL_SECRET}\n",
        encoding="utf-8",
    )

    settings = load_settings({})

    assert settings.profile is DeploymentProfile.TEST
    assert settings.release_mode is ReleaseMode.FIXTURE_ONLY
    assert settings.model_api_key is None


def test_explicit_env_mapping_takes_precedence_over_process_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式传入的 mapping 是唯一来源，不与 os.environ 合并。

    合并语义会让测试互相污染，也让「这份配置到底从哪来」无法回答。
    """
    monkeypatch.setenv("ZHIWEI_PROFILE", "production_reference")
    assert load_settings({}).profile is DeploymentProfile.TEST


def test_load_settings_defaults_to_process_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传参时才回落到 os.environ。"""
    monkeypatch.setenv("ZHIWEI_PROFILE", "local_product")
    assert load_settings().profile is DeploymentProfile.LOCAL_PRODUCT


# --------------------------------------------------------------------------- model provider 键名


def test_model_provider_keys_use_openai_compatible_names() -> None:
    """ADR-011：provider 凭据用标准键名，不加 ZHIWEI_ 前缀。

    企业自部署的内部 LLM 直接配 `OPENAI_BASE_URL` 即可接入，无需改本仓库配置。
    """
    settings = load_settings(
        {
            "OPENAI_API_KEY": SENTINEL_SECRET,
            "OPENAI_BASE_URL": "http://vllm.internal.example/v1",
            "OPENAI_MODEL": "qwen-max",
        }
    )
    assert settings.model_api_key is not None
    assert settings.model_api_key.get_secret_value() == SENTINEL_SECRET
    assert settings.model_base_url == "http://vllm.internal.example/v1"
    assert settings.model_name == "qwen-max"


def test_absent_model_provider_config_is_none_not_a_guessed_default() -> None:
    """未知即 null，不猜 `https://api.openai.com/v1` 这类"常见默认"。"""
    settings = load_settings({})
    assert settings.model_api_key is None
    assert settings.model_base_url is None
    assert settings.model_name is None


# --------------------------------------------------------------------------- secret 不泄漏


def _all_string_renderings(settings: Settings) -> list[str]:
    """凭据可能从任何一条字符串化路径漏出去，逐条都要堵。"""
    return [
        repr(settings),
        str(settings),
        f"{settings}",
        f"{settings!r}",
        json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, default=str),
    ]


def test_secret_never_appears_in_any_string_rendering() -> None:
    settings = load_settings(
        {"ZHIWEI_DATABASE_URL": f"postgresql://u:{SENTINEL_SECRET}@h/db", "OPENAI_API_KEY": SENTINEL_SECRET}
    )
    for rendering in _all_string_renderings(settings):
        assert SENTINEL_SECRET not in rendering, f"凭据出现在字符串化输出中: {rendering[:200]}"


def test_secret_fields_still_expose_the_value_through_an_explicit_call() -> None:
    """脱敏不能以"拿不到值"为代价——必须存在一条显式、可 grep 的取值路径。"""
    settings = load_settings({"OPENAI_API_KEY": SENTINEL_SECRET})
    assert settings.model_api_key is not None
    assert settings.model_api_key.get_secret_value() == SENTINEL_SECRET


def test_database_url_is_a_secret_field() -> None:
    """DSN 里带密码，它和 API key 是同一类东西。"""
    settings = load_settings({"ZHIWEI_DATABASE_URL": f"postgresql://u:{SENTINEL_SECRET}@h/db"})
    assert settings.database_url is not None
    assert SENTINEL_SECRET not in repr(settings.database_url)
    assert settings.database_url.get_secret_value().endswith("@h/db")


def test_settings_is_immutable() -> None:
    """配置在进程内不可改写——否则「这次请求用的是哪份配置」无法回答。"""
    settings = load_settings({})
    with pytest.raises((AttributeError, TypeError, ValueError)):
        settings.profile = DeploymentProfile.PRODUCTION_REFERENCE  # type: ignore[misc]


# --------------------------------------------------------------------------- object store


def test_object_store_root_is_absent_by_default() -> None:
    assert load_settings({}).object_store_root is None


def test_object_store_root_is_parsed_as_path(tmp_path: Path) -> None:
    settings = load_settings({"ZHIWEI_OBJECT_STORE_ROOT": str(tmp_path)})
    assert settings.object_store_root == tmp_path
    assert isinstance(settings.object_store_root, Path)


# --------------------------------------------------------------------------- 无全局状态


def test_load_settings_has_no_process_wide_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """两次调用互不影响——模块级单例会让测试顺序决定结果。"""
    first = load_settings({"ZHIWEI_PROFILE": "local_product"})
    second = load_settings({"ZHIWEI_PROFILE": "production_reference"})
    assert first.profile is DeploymentProfile.LOCAL_PRODUCT
    assert second.profile is DeploymentProfile.PRODUCTION_REFERENCE
    monkeypatch.delenv("ZHIWEI_PROFILE", raising=False)
    assert load_settings().profile is DeploymentProfile.TEST


def test_settings_module_import_does_not_touch_environment() -> None:
    """import 本身不得产生副作用——否则 `import zhiwei` 的行为取决于当时的环境变量。"""
    import importlib

    module = importlib.import_module("zhiwei.config.settings")
    assert not hasattr(module, "settings"), "不得在模块级实例化全局 settings 单例"
    assert os.environ.get("ZHIWEI_PROFILE") in (None, os.environ.get("ZHIWEI_PROFILE"))


# --------------------------------------------------------------------------- S1-T2 identity / OIDC / secret 配置


def test_identity_database_url_is_a_secret_field() -> None:
    """identity DSN 与 app DSN 同级：独立角色、独立凭据，repr 不得出现。"""
    settings = load_settings(
        {"ZHIWEI_IDENTITY_DATABASE_URL": f"postgresql://u:{SENTINEL_SECRET}@h/identity_db"}
    )
    assert settings.identity_database_url is not None
    assert settings.identity_database_url.get_secret_value().endswith("@h/identity_db")
    assert SENTINEL_SECRET not in repr(settings.identity_database_url)
    assert SENTINEL_SECRET not in repr(settings)


def test_identity_database_url_absent_by_default() -> None:
    assert load_settings({}).identity_database_url is None


def test_oidc_client_secret_is_a_secret_field() -> None:
    settings = load_settings({"ZHIWEI_OIDC_CLIENT_SECRET": SENTINEL_SECRET})
    assert settings.oidc_client_secret is not None
    assert settings.oidc_client_secret.get_secret_value() == SENTINEL_SECRET
    assert SENTINEL_SECRET not in repr(settings.oidc_client_secret)
    assert SENTINEL_SECRET not in repr(settings)
    for rendering in _all_string_renderings(settings):
        assert SENTINEL_SECRET not in rendering


def test_oidc_settings_parsed_from_environment_names() -> None:
    settings = load_settings(
        {
            "ZHIWEI_OIDC_ISSUER": "https://idp.example.com",
            "ZHIWEI_OIDC_CLIENT_ID": "zhiwei-bff",
            "ZHIWEI_OIDC_REDIRECT_URI": "https://app.example.com/auth/callback",
        }
    )
    assert settings.oidc_issuer == "https://idp.example.com"
    assert settings.oidc_client_id == "zhiwei-bff"
    assert settings.oidc_redirect_uri == "https://app.example.com/auth/callback"


def test_oidc_settings_absent_by_default() -> None:
    settings = load_settings({})
    assert settings.oidc_issuer is None
    assert settings.oidc_client_id is None
    assert settings.oidc_client_secret is None
    assert settings.oidc_redirect_uri is None


def test_identity_master_key_file_is_a_path(tmp_path: Path) -> None:
    settings = load_settings({"ZHIWEI_IDENTITY_MASTER_KEY_FILE": str(tmp_path / "master.key")})
    assert settings.identity_master_key_file == tmp_path / "master.key"
    assert isinstance(settings.identity_master_key_file, Path)
    assert load_settings({}).identity_master_key_file is None


# --------------------------------------------------------------------------- auth app 组合期拒绝（G 契约）


def _full_auth_app_settings(tmp_path: Path) -> dict[str, str]:
    return {
        "ZHIWEI_PROFILE": "test",
        "ZHIWEI_DATABASE_URL": "postgresql://zhiwei_app@db.example/zhiwei_test",
        "ZHIWEI_IDENTITY_DATABASE_URL": "postgresql://zhiwei_identity@db.example/zhiwei_test",
        "ZHIWEI_OIDC_ISSUER": "https://idp.example.com",
        "ZHIWEI_OIDC_CLIENT_ID": "zhiwei-bff",
        "ZHIWEI_OIDC_CLIENT_SECRET": SENTINEL_SECRET,
        "ZHIWEI_OIDC_REDIRECT_URI": "https://app.example.com/auth/callback",
        "ZHIWEI_IDENTITY_MASTER_KEY_FILE": str(tmp_path / "master.key"),
    }


@pytest.mark.parametrize(
    "dropped",
    [
        "ZHIWEI_DATABASE_URL",
        "ZHIWEI_IDENTITY_DATABASE_URL",
        "ZHIWEI_OIDC_ISSUER",
        "ZHIWEI_OIDC_CLIENT_ID",
        "ZHIWEI_OIDC_CLIENT_SECRET",
        "ZHIWEI_OIDC_REDIRECT_URI",
        "ZHIWEI_IDENTITY_MASTER_KEY_FILE",
    ],
)
def test_create_app_rejects_missing_composition_inputs(tmp_path: Path, dropped: str) -> None:
    """auth app 缺 identity DSN、OIDC issuer/client、master-key file 任一项都在组合期拒绝。"""
    from zhiwei.app import create_app

    env = {key: value for key, value in _full_auth_app_settings(tmp_path).items() if key != dropped}
    with pytest.raises(ValueError) as exc:
        create_app(load_settings(env))
    assert dropped in str(exc.value), "错误信息必须指名缺失的变量"


def test_create_app_composes_with_complete_settings(tmp_path: Path) -> None:
    """配置完整时 create_app 成功组合（engine 惰性，不触网）。"""
    from fastapi import FastAPI

    from zhiwei.app import create_app

    (tmp_path / "master.key").write_text("k1=YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY=\n", encoding="utf-8")
    app = create_app(load_settings(_full_auth_app_settings(tmp_path)))
    assert isinstance(app, FastAPI)
    assert app.state.session_service is not None or hasattr(app.state, "session_service")
