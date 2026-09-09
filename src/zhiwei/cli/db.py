"""`zhiwei db` 命令组：Alembic 迁移与 schema 检查的应用层封装。

约定：没有数据库时必须以一行可读信息 + 非零退出码失败——悄悄成功会让 CI 在数据库根本没起来的
情况下显示绿灯。错误信息绝不回显 DSN 密码（报错是凭据泄漏的高发路径）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal, NoReturn

import asyncpg
import click
import typer
from sqlalchemy.engine.url import make_url

from zhiwei.config.settings import Settings, load_settings

app = typer.Typer(help="数据库迁移与 schema 检查", no_args_is_help=True, pretty_exceptions_enable=False)

OUTPUT_FORMAT = Annotated[Literal["text", "json"], typer.Option("--format", help="输出格式")]
TIMEOUT = Annotated[int, typer.Option(min=1, help="连接超时秒数")]

# alembic.ini 与 migrations/ 位于仓库根目录（开发期即仓库内运行）。
_ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def _load_settings() -> Settings:
    """加载配置；配置错误转成一行可读信息，不抛栈。"""
    try:
        return load_settings()
    except ValueError as exc:
        click.echo(f"配置错误: {exc}", err=True)
        raise typer.Exit(1) from None


def _fail(message: str) -> NoReturn:
    click.echo(message, err=True)
    raise typer.Exit(1)


def _dsn_password(raw_dsn: str) -> str | None:
    """取出 DSN 密码用于错误信息脱敏；解析失败时保守地返回 None。"""
    try:
        return make_url(raw_dsn).password
    except ValueError:
        return None


def _sanitize(message: str, raw_dsn: str) -> str:
    """从错误信息中抹掉 DSN 原文与其密码——报错信息是凭据泄漏的高发路径。"""
    password = _dsn_password(raw_dsn)
    if password:
        message = message.replace(password, "***")
    return message.replace(raw_dsn, "<dsn>")


def _describe_error(exc: Exception, raw_dsn: str) -> str:
    """把连接异常转成一行可读描述；超时例外单独命名，其余信息脱敏后原样呈现。"""
    if isinstance(exc, TimeoutError):
        return "连接超时"
    return _sanitize(str(exc), raw_dsn) or type(exc).__name__


def _dsn_for_asyncpg(raw_dsn: str) -> str:
    """asyncpg 只认 postgresql:// scheme，去掉 +asyncpg 驱动后缀。"""
    return raw_dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _connect(raw_dsn: str, timeout: int) -> asyncpg.Connection:
    """带超时建连。仅 db 命令组使用；`dev doctor` 的 DB 检查不出网（见 dev.py）。"""
    return await asyncio.wait_for(
        asyncpg.connect(_dsn_for_asyncpg(raw_dsn), timeout=timeout),
        timeout=timeout,
    )


async def _current_revision(raw_dsn: str, timeout: int) -> str | None:
    """读取当前 schema revision；从未迁移过时返回 None。"""
    conn = await _connect(raw_dsn, timeout)
    try:
        row = await conn.fetchrow("SELECT version_num FROM alembic_version")
        return str(row["version_num"]) if row is not None else None
    except asyncpg.UndefinedTableError:
        # 缺 alembic_version 表 = 从未迁移过，语义上不是连接错误。
        return None
    finally:
        await conn.close()


def _run_migrations(raw_dsn: str) -> None:
    """把 DSN 交给 alembic 执行 upgrade head；env.py 优先读 config attributes。"""
    from alembic import command as alembic_command
    from alembic.config import Config

    cfg = Config(str(_ALEMBIC_INI))
    cfg.attributes["database_url"] = raw_dsn
    alembic_command.upgrade(cfg, "head")


def _emit_report(payload: dict[str, str], output_format: Literal["text", "json"]) -> None:
    if output_format == "json":
        # stdout 必须是纯 JSON（供 Gate 脚本消费）；诊断信息走 stderr。
        click.echo(json.dumps(payload, ensure_ascii=False))
    else:
        click.echo(f"status: {payload['status']} — {payload['detail']}")


@app.command("migrate")
def migrate(
    timeout: TIMEOUT = 5,
) -> None:
    """把 schema 迁移到最新 revision（只迁移，不建库）。"""
    settings = _load_settings()
    if settings.database_url is None:
        _fail("ZHIWEI_DATABASE_URL 未配置，无法执行迁移")
    raw_dsn = settings.database_url.get_secret_value()

    try:
        asyncio.run(_connect(raw_dsn, timeout))
    except Exception as exc:
        _fail(f"无法连接数据库: {_describe_error(exc, raw_dsn)}")

    try:
        _run_migrations(raw_dsn)
    except Exception as exc:
        _fail(f"迁移失败: {_sanitize(str(exc), raw_dsn)}")
    click.echo("migration 完成")


@app.command("check")
def check(
    output_format: OUTPUT_FORMAT = "text",
    timeout: TIMEOUT = 5,
) -> None:
    """检查数据库可达性与当前 schema revision。"""
    settings = _load_settings()
    if settings.database_url is None:
        _emit_report(
            {"status": "not_configured", "detail": "ZHIWEI_DATABASE_URL 未配置"},
            output_format,
        )
        raise typer.Exit(1)
    raw_dsn = settings.database_url.get_secret_value()

    try:
        revision = asyncio.run(_current_revision(raw_dsn, timeout))
    except Exception as exc:
        _emit_report(
            {"status": "error", "detail": f"无法连接数据库: {_describe_error(exc, raw_dsn)}"},
            output_format,
        )
        raise typer.Exit(1) from None

    if revision is None:
        _emit_report(
            {"status": "error", "detail": "schema 未初始化：数据库中不存在 alembic_version 表"},
            output_format,
        )
        raise typer.Exit(1)
    _emit_report({"status": "ok", "detail": f"schema revision: {revision}"}, output_format)
