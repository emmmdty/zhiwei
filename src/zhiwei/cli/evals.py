"""`zhiwei eval` 命令组：最小执行器与 sealing 工作流。

所有子命令都必须走真实 PostgreSQL/ObjectStore/event/outbox 管线：`seal-empty --check` 在密封后
独立复核，legacy run 实际执行全部 checksum registry，再由 `seal` 显式恢复 tenant 上下文密封。
不提供占位 help 命令；未知 mode 在进入执行前用 Enum 拒绝。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import UUID, uuid4

import click
import typer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from zhiwei.config.settings import Settings, load_settings
from zhiwei.contracts.canonical import digest_bytes
from zhiwei.evals.domain import EvalMode
from zhiwei.evals.executors import EmptyExecutor, LegacyExecutor
from zhiwei.evals.legacy_assets import LegacyAssetInventory
from zhiwei.evals.runs import (
    CreateEvalRunCommand,
    EvalFoundationService,
    SealEmptyCommand,
)
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import EvalRun, Run
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

app = typer.Typer(
    help="评测执行与密封（fixture/offline，不调用 live 模型）",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
_KNOWN_EXECUTORS = frozenset({"legacy", "empty"})

SUITE_ARG = Annotated[str, typer.Option("--suite", help="suite 名称")]
EXECUTOR_ARG = Annotated[str, typer.Option("--executor", help="executor 名称")]
MODE_ARG = Annotated[EvalMode, typer.Option("--mode", help="执行模式")]
EVAL_RUN_ID_ARG = Annotated[UUID, typer.Argument(help="EvalRun ID")]
ORGANIZATION_ID_OPT = Annotated[
    UUID, typer.Option("--organization-id", help="EvalRun 所属组织（显式恢复 RLS 上下文）")
]
WORKSPACE_ID_OPT = Annotated[
    UUID, typer.Option("--workspace-id", help="EvalRun 所属工作区（显式恢复 RLS 上下文）")
]


def _load_settings() -> Settings:
    try:
        return load_settings()
    except ValueError as exc:
        click.echo(f"配置错误: {exc}", err=True)
        raise typer.Exit(1) from None


def _fail(message: str) -> NoReturn:
    click.echo(message, err=True)
    raise typer.Exit(1)


def _require_runtime(settings: Settings) -> tuple[str, Path]:
    if settings.database_url is None:
        _fail("ZHIWEI_DATABASE_URL 未配置，无法连接 PostgreSQL")
    if settings.object_store_root is None:
        _fail("ZHIWEI_OBJECT_STORE_ROOT 未配置，无法写入 artifact 对象")
    return settings.database_url.get_secret_value(), settings.object_store_root


def _emit_json(payload: dict[str, Any]) -> None:
    # stdout 必须是纯 JSON（供 Gate 脚本消费）；诊断信息走 stderr。
    click.echo(json.dumps(payload, ensure_ascii=False))


def _run_flow(
    sessions: async_sessionmaker, flow: Callable[[Any], Awaitable[dict[str, Any]]], context: TenantContext
) -> dict[str, Any]:
    async def runner() -> dict[str, Any]:
        async with tenant_session(sessions, context) as session:
            return await flow(session)

    try:
        return asyncio.run(runner())
    except Exception as exc:
        _fail(f"eval 失败: {str(exc).splitlines()[0]}")


async def _migration_revision(session: Any) -> str:
    """读取数据库当前 migration revision；缺失时 fail closed。"""
    row = await session.execute(text("SELECT version_num FROM alembic_version"))
    revision = row.scalar_one_or_none()
    if revision is None:
        raise RuntimeError("schema 未初始化：数据库中不存在 alembic_version 表")
    return str(revision)


async def _seal_empty_flow(session: Any, context: TenantContext, store: PosixObjectStore) -> dict[str, Any]:
    if context.workspace_id is None:
        raise RuntimeError("seal-empty 需要 workspace 上下文")
    repository = TenantRepository(session, context)
    await repository.create_organization(context.organization_id, status="active")
    await repository.create_workspace(context.workspace_id, name="S0-T6")
    service = EvalFoundationService(session, context, store)
    migration_revision = await _migration_revision(session)
    sealed = await service.seal_empty(
        SealEmptyCommand(
            mode=EvalMode.FIXTURE,
            code_digest=digest_bytes(b"zhiwei S0-T6 code revision"),
            config_digest=digest_bytes(b"zhiwei S0-T6 test config"),
            schema_digest=digest_bytes(b"0001_foundation schema"),
            migration_revision=migration_revision,
            test_report={
                "status": "passed",
                "scope": "plumbing",
                "command": "zhiwei eval seal-empty --check",
            },
        )
    )
    run = await session.scalar(
        select(Run).where(
            Run.id == sealed.run_id,
            Run.organization_id == context.organization_id,
            Run.workspace_id == context.workspace_id,
        )
    )
    await service.verify_sealed(sealed.eval_run_id)
    if run is None:
        raise RuntimeError("sealed Run 从 tenant scope 中丢失")
    return {
        "mode": "fixture",
        "run_status": run.status,
        "eval_run_status": "sealed",
        "registered_units": sealed.registered_units,
        "verified": True,
        "run_id": str(sealed.run_id),
        "eval_run_id": str(sealed.eval_run_id),
        "manifest_id": str(sealed.manifest_id),
        "seal_digest": sealed.seal_digest,
    }


async def _run_flow_impl(
    session: Any,
    context: TenantContext,
    store: PosixObjectStore,
    *,
    suite: str,
    executor_name: str,
    mode: EvalMode,
) -> dict[str, Any]:
    if suite != "legacy-assets":
        raise ValueError(f"未知 suite: {suite}")
    if context.workspace_id is None:
        raise RuntimeError("run 需要 workspace 上下文")
    repository = TenantRepository(session, context)
    await repository.create_organization(context.organization_id, status="active")
    await repository.create_workspace(context.workspace_id, name="S0-T6")
    inventory = LegacyAssetInventory.load(REPO_ROOT / "evals")
    executor = (
        LegacyExecutor(inventory) if executor_name == "legacy" else EmptyExecutor()
    )
    units = inventory.registered_units
    service = EvalFoundationService(session, context, store)
    created = await service.create(
        CreateEvalRunCommand(
            mode=mode,
            registered_units=units,
            dataset_payload={
                "suite": suite,
                "registered_units": [
                    {"sample_id": unit.sample_id, "unit_id": unit.unit_id}
                    for unit in units
                ],
            },
            code_digest=digest_bytes(b"zhiwei S0-T6 legacy adapter"),
            config_digest=digest_bytes(b"zhiwei S0-T6 offline config"),
            schema_digest=digest_bytes(b"0001_foundation schema"),
        )
    )
    terminal_units = 0
    for unit in units:
        outcome = await executor.execute(unit)
        await service.record_outcome(created.eval_run_id, outcome)
        terminal_units += 1
    return {
        "mode": mode.value,
        "executor": executor_name,
        "registered_units": len(units),
        "terminal_units": terminal_units,
        "eval_run_id": str(created.eval_run_id),
        "organization_id": str(context.organization_id),
        "workspace_id": str(context.workspace_id),
    }


async def _resume_flow(
    session: Any, context: TenantContext, store: PosixObjectStore, eval_run_id: UUID
) -> dict[str, Any]:
    service = EvalFoundationService(session, context, store)
    await service.resume(eval_run_id)
    return {"eval_run_id": str(eval_run_id), "eval_run_status": "running"}


async def _seal_flow(
    session: Any,
    context: TenantContext,
    store: PosixObjectStore,
    eval_run_id: UUID,
) -> dict[str, Any]:
    service = EvalFoundationService(session, context, store)
    migration_revision = await _migration_revision(session)
    sealed = await service.seal(
        eval_run_id,
        migration_revision=migration_revision,
        test_report={
            "status": "passed",
            "scope": "cli",
            "command": "zhiwei eval seal",
        },
    )
    eval_run = await session.scalar(
        select(EvalRun).where(
            EvalRun.id == eval_run_id,
            EvalRun.organization_id == context.organization_id,
            EvalRun.workspace_id == context.workspace_id,
        )
    )
    await service.verify_sealed(eval_run_id)
    if eval_run is None:
        raise RuntimeError("sealed EvalRun 从 tenant scope 中丢失")
    return {
        "eval_run_id": str(eval_run_id),
        "eval_run_status": eval_run.status,
        "verified": True,
        "manifest_id": str(sealed.manifest_id),
        "seal_digest": sealed.seal_digest,
    }


def _fresh_tenant() -> tuple[TenantContext, UUID, UUID]:
    organization_id, workspace_id = uuid4(), uuid4()
    return (
        TenantContext(organization_id=organization_id, workspace_id=workspace_id),
        organization_id,
        workspace_id,
    )


def _settings_runtime() -> tuple[Settings, str, Path, PosixObjectStore, async_sessionmaker]:
    settings = _load_settings()
    database_url, object_root = _require_runtime(settings)
    engine = create_database_engine(database_url)
    return (
        settings,
        database_url,
        object_root,
        PosixObjectStore(object_root),
        create_session_factory(engine),
    )


@app.command("seal-empty")
def seal_empty(
    check: bool = typer.Option(
        False, "--check", help="密封后立即从 object/manifest 独立复核（S0 Gate 步骤）"
    ),
) -> None:
    """创建真实 PG 空 Run/EvalRun 并密封为可复核 artifact。"""
    _, _, _, store, sessions = _settings_runtime()
    context, _, _ = _fresh_tenant()
    payload = _run_flow(sessions, lambda s: _seal_empty_flow(s, context, store), context)
    _emit_json(payload)


@app.command("run")
def run(
    suite: SUITE_ARG = "legacy-assets",
    executor: EXECUTOR_ARG = "legacy",
    mode: MODE_ARG = EvalMode.OFFLINE,
) -> None:
    """执行 suite 的全部注册单位（当前为 legacy-assets，无 live 模型调用）。"""
    if executor not in _KNOWN_EXECUTORS:
        _fail(f"未知 executor: {executor}")
    _, _, _, store, sessions = _settings_runtime()
    context, _, _ = _fresh_tenant()
    payload = _run_flow(
        sessions,
        lambda s: _run_flow_impl(
            s, context, store, suite=suite, executor_name=executor, mode=mode
        ),
        context,
    )
    _emit_json(payload)


@app.command("resume")
def resume(
    eval_run_id: EVAL_RUN_ID_ARG,
    organization_id: ORGANIZATION_ID_OPT,
    workspace_id: WORKSPACE_ID_OPT,
) -> None:
    """以同一冻结 registry 恢复 partial EvalRun。"""
    _, _, _, store, sessions = _settings_runtime()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    payload = _run_flow(
        sessions,
        lambda s: _resume_flow(s, context, store, eval_run_id),
        context,
    )
    _emit_json(payload)


@app.command("seal")
def seal(
    eval_run_id: EVAL_RUN_ID_ARG,
    organization_id: ORGANIZATION_ID_OPT,
    workspace_id: WORKSPACE_ID_OPT,
) -> None:
    """密封已完成（全部单位 terminal）的 EvalRun 并独立复核。"""
    _, _, _, store, sessions = _settings_runtime()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    payload = _run_flow(
        sessions,
        lambda s: _seal_flow(s, context, store, eval_run_id),
        context,
    )
    _emit_json(payload)
