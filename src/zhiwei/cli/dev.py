"""`zhiwei dev` 命令组：面向开发者的环境诊断。

`dev doctor` 回答"这套环境到底能不能用"，且**在回答过程中不发起任何网络连接**——DB 检查只做
"配没配、DSN 能不能解析"（`sqlalchemy.engine.url.make_url` 是纯解析，不出网），模型 provider
配置了也不 probe。"会顺手连一下 provider 的 doctor"会让"不调用 live 模型"在最小的地方破掉。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

import click
import typer
from sqlalchemy.engine.url import make_url

from zhiwei.config.settings import Settings, load_settings

app = typer.Typer(help="开发诊断与调试命令", no_args_is_help=True, pretty_exceptions_enable=False)

OUTPUT_FORMAT = Annotated[Literal["text", "json"], typer.Option("--format", help="输出格式")]


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
    # 查 revision 需要连库，而 T1 的 doctor 不做任何连接；migration 接入后（S0-T3）才有答案。
    return {
        "status": "unknown",
        "detail": "schema revision 未知：migration 尚未接入（S0-T3）",
    }


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
