"""S0-T1 RED：`zhiwei db` 命令组的契约。

`db migrate` / `db check` 是 Alembic 与 schema 检查的应用层薄封装。本 Task 只固定命令行契约与
**没有数据库时的表现**——真实 migration 行为属于 S0-T3，不在这里断言。

关键点是"没有 DB 时怎么失败"：必须是一条可读的错误 + 非零退出码，不是 traceback，也不是
悄悄成功。后者会让 CI 在数据库根本没起来的情况下显示绿灯。
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner
from zhiwei.cli.main import app

runner = CliRunner()

TRACEBACK_MARKER = "Traceback (most recent call last)"

# 保留地址段 TEST-NET-1（RFC 5737），保证连不通且不会误伤真实主机。
UNREACHABLE_DSN = "postgresql+asyncpg://zhiwei:pw@192.0.2.1:5432/zhiwei"

# 同 test_dev_cli：CliRunner 的 env 叠加在 os.environ 上，必须显式清空受管变量。
_MANAGED_VARS = (
    "ZHIWEI_PROFILE",
    "ZHIWEI_RELEASE_MODE",
    "ZHIWEI_DATABASE_URL",
    "ZHIWEI_OBJECT_STORE_ROOT",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
)


def _env(**overrides: str) -> dict[str, str | None]:
    env: dict[str, str | None] = dict.fromkeys(_MANAGED_VARS)
    env.update(overrides)
    return env


# --------------------------------------------------------------------------- --help


def test_root_help_lists_db_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "db" in result.output


@pytest.mark.parametrize("command", ["migrate", "check"])
def test_db_subcommand_help_exits_zero(command: str) -> None:
    result = runner.invoke(app, ["db", command, "--help"])
    assert result.exit_code == 0
    assert TRACEBACK_MARKER not in result.output


def test_db_help_lists_both_subcommands() -> None:
    result = runner.invoke(app, ["db", "--help"])
    assert result.exit_code == 0
    assert "migrate" in result.output
    assert "check" in result.output


# --------------------------------------------------------------------------- 未配置 DB


@pytest.mark.parametrize("command", ["migrate", "check"])
def test_db_command_fails_clearly_when_no_database_configured(command: str) -> None:
    """没有 ZHIWEI_DATABASE_URL 时必须明确失败并指出缺什么。"""
    result = runner.invoke(app, ["db", command], env=_env(ZHIWEI_PROFILE="test"))
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "ZHIWEI_DATABASE_URL" in result.output


@pytest.mark.parametrize("command", ["migrate", "check"])
def test_db_command_fails_when_database_is_unreachable(command: str) -> None:
    """配了 DSN 但连不上，同样是非零退出 + 可读信息，不是 traceback。"""
    result = runner.invoke(
        app,
        ["db", command, "--timeout", "2"],
        env=_env(ZHIWEI_PROFILE="test", ZHIWEI_DATABASE_URL=UNREACHABLE_DSN),
    )
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


@pytest.mark.parametrize("command", ["migrate", "check"])
def test_db_command_never_echoes_the_dsn_password(command: str) -> None:
    """错误信息里不得带出 DSN 中的密码——报错是凭据泄漏的高发路径。"""
    secret = "pw-db-cli-leak-check-4410"
    result = runner.invoke(
        app,
        ["db", command, "--timeout", "2"],
        env=_env(
            ZHIWEI_PROFILE="test",
            ZHIWEI_DATABASE_URL=f"postgresql+asyncpg://zhiwei:{secret}@192.0.2.1:5432/zhiwei",
        ),
    )
    assert secret not in result.output


# --------------------------------------------------------------------------- check 的输出契约


def test_db_check_supports_json_format() -> None:
    """`db check --format json` 要能被 Gate 脚本消费。"""
    result = runner.invoke(app, ["db", "check", "--format", "json"], env=_env(ZHIWEI_PROFILE="test"))
    assert TRACEBACK_MARKER not in result.output
    import json

    payload: dict[str, Any] = json.loads(result.stdout)
    assert "status" in payload


def test_db_check_rejects_unknown_format() -> None:
    result = runner.invoke(app, ["db", "check", "--format", "toml"], env=_env(ZHIWEI_PROFILE="test"))
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


# --------------------------------------------------------------------------- migrate 不得在 test profile 下静默建库


def test_db_migrate_does_not_create_a_database_implicitly() -> None:
    """migrate 只负责迁移 schema，不负责把库建出来。

    隐式建库会让"连错了库"表现为"跑通了"，而这类错误在生产上是不可逆的。
    """
    result = runner.invoke(app, ["db", "migrate", "--help"])
    assert result.exit_code == 0
    assert "--create-database" not in result.output
