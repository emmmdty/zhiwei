"""Typed, fail-closed deployment settings.

配置来源只有环境变量：显式传入的 mapping 或 `os.environ`，二者**互不合并**，也从不读取 `.env`
文件（AGENTS.md「不读 .env」）。未知的 profile / release mode 在加载期抛 `ValueError` 并指名
变量——不允许静默回落到某个"常见默认"，否则测试进程可能在没有显式声明的情况下获得 live 能力。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

# 环境变量不是部署期 override 清单，而是唯一配置来源。`OPENAI_*` 三个键沿用标准名（ADR-011）：
# 企业自部署的内部 LLM 配 `OPENAI_BASE_URL` 即可接入，无需在本仓库配置里登记。
_PROFILE_ENV = "ZHIWEI_PROFILE"
_RELEASE_MODE_ENV = "ZHIWEI_RELEASE_MODE"
_DATABASE_URL_ENV = "ZHIWEI_DATABASE_URL"
_OBJECT_STORE_ROOT_ENV = "ZHIWEI_OBJECT_STORE_ROOT"
_MODEL_API_KEY_ENV = "OPENAI_API_KEY"
_MODEL_BASE_URL_ENV = "OPENAI_BASE_URL"
_MODEL_NAME_ENV = "OPENAI_MODEL"


class DeploymentProfile(StrEnum):
    """部署档位，从最受限到最宽松。

    取值必须与 CLI / 测试断言里出现的拼写严格一致：`local-product`、`prod`、`TEST` 等变体
    一律拒绝，接受它们会让配置文件里出现两种写法并存，成为后续 drift 的起点。
    """

    TEST = "test"
    LOCAL_PRODUCT = "local_product"
    PRODUCTION_REFERENCE = "production_reference"


class ReleaseMode(StrEnum):
    """模型调用开关：`fixture_only` 永不发出真实请求，`live` 只由 operator 显式触发。"""

    FIXTURE_ONLY = "fixture_only"
    LIVE = "live"


class Settings(BaseModel):
    """冻结的部署配置。进程内不可改写，避免"这次请求用的是哪份配置"无法回答。

    所有 secret 字段（DSN、API key）用 `SecretStr`：repr / str / model_dump 一律脱敏，只有
    显式调用 `.get_secret_value()` 才能取出原文——这是可被测试证伪的结构，不是约定。
    """

    model_config = ConfigDict(frozen=True)

    profile: DeploymentProfile = DeploymentProfile.TEST
    release_mode: ReleaseMode = ReleaseMode.FIXTURE_ONLY
    database_url: SecretStr | None = None
    object_store_root: Path | None = None
    model_api_key: SecretStr | None = None
    model_base_url: str | None = None
    model_name: str | None = None

    @model_validator(mode="after")
    def _deny_live_below_production(self) -> Self:
        """test / local_product 档不允许 live 调用。

        门禁放在加载期而不是"发出请求前"：否则「测试进程不得在 live 下运行」只能靠事后发现，
        而那时请求可能已经发出去了。
        """
        if (
            self.release_mode is ReleaseMode.LIVE
            and self.profile is not DeploymentProfile.PRODUCTION_REFERENCE
        ):
            raise ValueError(
                "profile 'test'/'local_product' 不允许 release mode 'live'："
                "只有 production_reference 可直连真实 provider"
            )
        return self

    @property
    def live_model_calls_allowed(self) -> bool:
        """当前配置是否允许发出真实模型请求。校验保证该属性仅在生产档 + live 下为真。"""
        return self.release_mode is ReleaseMode.LIVE


def _parse_profile(raw: str | None) -> DeploymentProfile:
    if raw is None:
        return DeploymentProfile.TEST
    try:
        return DeploymentProfile(raw)
    except ValueError:
        raise ValueError(
            f"{_PROFILE_ENV}: 未知取值 {raw!r}，应为 "
            "test / local_product / production_reference"
        ) from None


def _parse_release_mode(raw: str | None) -> ReleaseMode:
    if raw is None:
        return ReleaseMode.FIXTURE_ONLY
    try:
        return ReleaseMode(raw)
    except ValueError:
        raise ValueError(f"{_RELEASE_MODE_ENV}: 未知取值 {raw!r}，应为 fixture_only / live") from None


def _optional_secret(raw: str | None) -> SecretStr | None:
    """空字符串视为"未提供"：把空值塞进 SecretStr 只会误导"到底配没配"这个问题。"""
    if raw is None or raw == "":
        return None
    return SecretStr(raw)


def _optional_path(raw: str | None) -> Path | None:
    if raw is None or raw == "":
        return None
    return Path(raw)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """从显式传入的 mapping（默认 `os.environ`）构建配置。

    传空 mapping（`{}`）时结果是全空配置，即使 CWD 下有 `.env` 也不会读取——测试与工具链的
    行为不得取决于某台机器上的本地文件。显式传入的 mapping 是唯一来源，不与 `os.environ` 合并，
    否则"这份配置到底从哪来"无法回答。
    """
    source = os.environ if env is None else env
    return Settings(
        profile=_parse_profile(source.get(_PROFILE_ENV)),
        release_mode=_parse_release_mode(source.get(_RELEASE_MODE_ENV)),
        database_url=_optional_secret(source.get(_DATABASE_URL_ENV)),
        object_store_root=_optional_path(source.get(_OBJECT_STORE_ROOT_ENV)),
        model_api_key=_optional_secret(source.get(_MODEL_API_KEY_ENV)),
        model_base_url=source.get(_MODEL_BASE_URL_ENV),
        model_name=source.get(_MODEL_NAME_ENV),
    )
