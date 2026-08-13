"""`zhiwei dev` 命令组：面向开发者的环境诊断。

`dev doctor` 回答"这套环境到底能不能用"。DB 检查只连接**配置的数据库**（短超时、失败即
error），绝不连接模型 provider——provider 配置了也不 probe，否则"不调用 live 模型"会在最
不起眼的地方破掉。所有失败信息脱敏：不出现 DSN 原文或密码。
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any, Literal

import click
import typer
from sqlalchemy.engine.url import make_url

from zhiwei.cli.db import _current_revision, _describe_error
from zhiwei.config.settings import Settings, load_settings

app = typer.Typer(help="开发诊断与调试命令", no_args_is_help=True, pretty_exceptions_enable=False)

OUTPUT_FORMAT = Annotated[Literal["text", "json"], typer.Option("--format", help="输出格式")]

# revision 查询只连配置的数据库；连接超时给短值，避免 doctor 在坏 DSN 上挂起。
REVISION_TIMEOUT = 3


def _load_settings() -> Settings:
    """加载配置；配置错误转成一行可读信息，不抛栈。"""
    try:
        return load_settings()
    except ValueError as exc:
        click.echo(f"配置错误: {exc}", err=True)
        raise typer.Exit(1) from None


def _database_check(settings: Settings) -> dict[str, str]:
    if settings.database_url is None:
        return {"status": "not_configured", "detail": "ZHIWEI_DATABASE_URL 未配置"}
    # 只做语法级解析，不建立任何连接——doctor 不得出网。
    try:
        make_url(settings.database_url.get_secret_value())
    except ValueError:
        return {"status": "error", "detail": "ZHIWEI_DATABASE_URL 无法解析为数据库 URL"}
    return {"status": "ok", "detail": "数据库 DSN 已配置且可解析"}


def _object_store_check(settings: Settings) -> dict[str, str]:
    if settings.object_store_root is None:
        return {"status": "not_configured", "detail": "ZHIWEI_OBJECT_STORE_ROOT 未配置"}
    if not settings.object_store_root.is_dir():
        return {
            "status": "error",
            "detail": f"对象存储目录不存在: {settings.object_store_root}",
        }
    return {"status": "ok", "detail": str(settings.object_store_root)}


def _schema_revision_check(settings: Settings) -> dict[str, str]:
    """查询数据库当前 migration revision；未配置、连不上、未迁移都显式报错。

    S0 Gate（specs/s0-foundation.md §6）要求配置健康 DB 后 doctor 返回真实 revision
    并退出 0——占位的 unknown 会让 Gate 永远失败，因此这里做真实查询。模型 provider
    绝不接触；错误信息经脱敏（不出现 DSN/密码）。
    """
    if settings.database_url is None:
        return {
            "status": "not_configured",
            "detail": "ZHIWEI_DATABASE_URL 未配置，无法查询 schema revision",
        }
    raw_dsn = settings.database_url.get_secret_value()
    try:
        revision = asyncio.run(_current_revision(raw_dsn, REVISION_TIMEOUT))
    except Exception as exc:
        return {"status": "error", "detail": f"无法查询 schema revision: {_describe_error(exc, raw_dsn)}"}
    if revision is None:
        return {
            "status": "error",
            "detail": "schema 未初始化：数据库中不存在 alembic_version 表",
        }
    return {"status": "ok", "detail": f"schema revision: {revision}"}


def _doctor_payload(settings: Settings) -> dict[str, Any]:
    checks: dict[str, dict[str, str]] = {
        "database": _database_check(settings),
        "object_store": _object_store_check(settings),
        "schema_revision": _schema_revision_check(settings),
    }
    return {
        "profile": settings.profile.value,
        "release_mode": settings.release_mode.value,
        "live_model_calls_allowed": settings.live_model_calls_allowed,
        "checks": checks,
    }


@app.command("doctor")
def doctor(
    output_format: OUTPUT_FORMAT = "text",
) -> None:
    """显示 DB、object store、schema revision 与 release mode 的就绪状态。"""
    settings = _load_settings()
    payload = _doctor_payload(settings)

    if output_format == "json":
        # stdout 必须是可直接管进 jq 的纯 JSON；诊断信息走 stderr。
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        lines = [
            f"profile: {payload['profile']}",
            f"release_mode: {payload['release_mode']}",
            f"live_model_calls_allowed: {payload['live_model_calls_allowed']}",
        ]
        for name, check in payload["checks"].items():
            lines.append(f"check {name}: {check['status']} — {check['detail']}")
        click.echo("\n".join(lines))

    # 任一 check 未就绪，Gate 就必须可见地失败，否则"报了问题却退出 0"形同虚设。
    if any(check["status"] != "ok" for check in payload["checks"].values()):
        raise typer.Exit(1)
