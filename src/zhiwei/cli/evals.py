"""`zhiwei eval` 命令组：最小执行器与 sealing 工作流。

所有子命令都必须走真实 PostgreSQL/ObjectStore/event/outbox 管线：`seal-empty --check` 在密封后
独立复核，legacy run 实际执行全部 checksum registry，再由 `seal` 显式恢复 tenant 上下文密封。
不提供占位 help 命令；未知 mode 在进入执行前用 Enum 拒绝。

密封的 provenance 绑定真实内容：code/config/schema digest 取自仓库当前源码、配置与 migration
文件（确定性树摘要），test report 取自已实际执行的 S0 eval 契约测试输出——不写死摘要值。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import UUID, uuid4

import click
import typer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from zhiwei.config.settings import Settings, load_settings
from zhiwei.contracts.canonical import digest, digest_bytes
from zhiwei.evals.ask_contracts import ASK_V1_SUITE
from zhiwei.evals.domain import EvalMode, RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.executors import EmptyExecutor, LegacyExecutor
from zhiwei.evals.executors.knowledge import KnowledgeRetrievalExecutor
from zhiwei.evals.external import (
    ExternalAdapterSpec,
    probe_adapter,
    resolve_external_adapter,
    run_available_adapter,
)
from zhiwei.evals.factqa_suites import FACTQA_V1, resolve_factqa_suite
from zhiwei.evals.knowledge_suites import (
    KNOWLEDGE_SUITE_NAMES,
    resolve_knowledge_suite,
)
from zhiwei.evals.legacy_assets import LegacyAssetInventory
from zhiwei.evals.memory_suites import (
    ENTERPRISE_MEMORY_UNIT_CATEGORIES,
    ENTERPRISE_MEMORY_V1,
    resolve_memory_suite,
)
from zhiwei.evals.risk_suites import NUMERIC_RISK_V1, RISK_SUITE_NAMES
from zhiwei.evals.runs import (
    CreateEvalRunCommand,
    EvalFoundationService,
    SealEmptyCommand,
)
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import EvalRun, EvalSample, Run
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

app = typer.Typer(
    help="评测执行与密封（fixture/offline，不调用 live 模型）",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
_KNOWN_EXECUTORS = frozenset({"legacy", "empty", "agent-runtime"})
# S6 suite：factqa-v1（Evidence/SQL regression）与 ask-v1（Ask 行为契约）。
_S6_SUITE_NAMES = frozenset({FACTQA_V1, ASK_V1_SUITE})
# S7 suite：enterprise-memory-v1（memory 行为契约，代码定义 units）。
_S7_SUITE_NAMES = frozenset({ENTERPRISE_MEMORY_V1})
# S8 suite：numeric-risk-v1（冻结资产）与 discover-blind-v1（代码定义 blind 快照）。
_S8_SUITE_NAMES = frozenset(RISK_SUITE_NAMES)
# suite 解析集：eval run 只接受注册在案的 suite；未知 suite 在触碰任何 runtime 依赖
# （DB/ObjectStore）之前 fail closed。knowledge suite 见 zhiwei.evals.knowledge_suites，
# S6 suite（factqa-v1 / ask-v1）见 _S6_SUITE_NAMES，S7 memory suite 见 memory_suites，
# S8 risk suite 见 zhiwei.evals.risk_suites。
_KNOWN_SUITES: frozenset[str] = frozenset(
    {"legacy-assets", "runtime-contract-v1"}
) | KNOWLEDGE_SUITE_NAMES | _S6_SUITE_NAMES | _S7_SUITE_NAMES | _S8_SUITE_NAMES
# executor 由注册表绑定生产路径的 suite（指定 empty/agent-runtime 会落账伪造结果
# 或走错执行面，进入执行前拒绝）。
_REGISTRY_BOUND_SUITES = KNOWLEDGE_SUITE_NAMES | _S6_SUITE_NAMES | _S7_SUITE_NAMES | _S8_SUITE_NAMES

# seal-empty 的 test report 证据范围：S0 eval 单元与 CLI 契约测试。
# 不含 integration/foundation/test_empty_run.py——该文件本身会调用 seal-empty，纳入会递归。
_EVAL_EVIDENCE_TARGETS = (
    "tests/unit/evals",
    "tests/contract/cli/test_assets_cli.py",
    "tests/contract/cli/test_eval_cli.py",
)

SUITE_ARG = Annotated[str, typer.Option("--suite", help="suite 名称")]
EXECUTOR_ARG = Annotated[str, typer.Option("--executor", help="executor 名称")]
MODE_ARG = Annotated[EvalMode, typer.Option("--mode", help="执行模式")]
SEAL_ARG = Annotated[bool, typer.Option("--seal", help="执行完毕后密封 EvalRun")]
CONFIG_ARG = Annotated[
    Path | None,
    typer.Option("--config", help="adapter 清单路径（默认 config/evals/external_adapters.yaml）"),
]
DATA_ROOT_ARG = Annotated[
    Path | None, typer.Option("--data-root", help="清单相对路径的解析根（默认仓库根）")
]
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


def _tree_digest(paths: tuple[Path, ...]) -> str:
    """对仓库内一组路径做确定性内容摘要：`{相对路径: sha256}` 的 canonical digest。

    目录递归、稳定排序、跳过 `__pycache__`；任何文件内容或相对路径变化都会改变 digest。
    """
    entries: dict[str, str] = {}
    for root in paths:
        if not root.exists():
            continue
        if root.is_file():
            entries[root.relative_to(REPO_ROOT).as_posix()] = digest_bytes(
                root.read_bytes()
            )
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            entries[path.relative_to(REPO_ROOT).as_posix()] = digest_bytes(
                path.read_bytes()
            )
    return digest({"files": [{"path": p, "digest": entries[p]} for p in sorted(entries)]})


def _provenance_digests() -> dict[str, str]:
    """当前仓库的真实 provenance：源码、配置、migration 内容摘要。"""
    return {
        "code_digest": _tree_digest((REPO_ROOT / "src",)),
        "config_digest": _tree_digest(
            (REPO_ROOT / "pyproject.toml", REPO_ROOT / "config")
        ),
        "schema_digest": _tree_digest((REPO_ROOT / "migrations",)),
    }


def _eval_test_evidence() -> dict[str, Any]:
    """实际执行 S0 eval 契约测试并采集汇总；失败也如实记录，不伪造 passed。"""
    command = [sys.executable, "-m", "pytest", "-q", *_EVAL_EVIDENCE_TARGETS]
    completed = subprocess.run(
        command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=300
    )
    combined = (completed.stdout + completed.stderr).splitlines()
    tail = "\n".join(line for line in combined if line.strip())[-400:]
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "command": " ".join(command),
        "summary": tail,
    }


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
    evidence = _eval_test_evidence()
    if evidence["status"] != "passed":
        raise RuntimeError(
            f"S0 eval 契约测试未通过，拒绝密封 empty gate（exit {evidence['exit_code']}）"
        )
    repository = TenantRepository(session, context)
    await repository.create_organization(context.organization_id, status="active")
    await repository.create_workspace(context.workspace_id, name="S0-T6")
    service = EvalFoundationService(session, context, store)
    migration_revision = await _migration_revision(session)
    provenance = _provenance_digests()
    sealed = await service.seal_empty(
        SealEmptyCommand(
            mode=EvalMode.FIXTURE,
            code_digest=provenance["code_digest"],
            config_digest=provenance["config_digest"],
            schema_digest=provenance["schema_digest"],
            migration_revision=migration_revision,
            test_report={
                "status": evidence["status"],
                "scope": "s0-eval-contract-tests",
                "command": evidence["command"],
                "exit_code": evidence["exit_code"],
                "summary": evidence["summary"],
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


async def _runtime_contract_flow(
    sessions: Any,
    context: TenantContext,
    store: PosixObjectStore,
    *,
    mode: EvalMode,
    seal: bool,
) -> dict[str, Any]:
    """runtime-contract-v1：全部单位经生产 Runtime 命令路径执行后统一落账。"""
    from zhiwei.evals.executors.agent_runtime import (
        RuntimeEvalEnvironment,
    )
    from zhiwei.evals.runtime_contracts import RUNTIME_CONTRACT_UNITS

    if context.workspace_id is None:
        raise RuntimeError("run 需要 workspace 上下文")
    from zhiwei.persistence.tenant import tenant_session as _tenant_session

    async with _tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(context.organization_id, status="active")
        await repository.create_workspace(context.workspace_id, name="S2-T6")

    # 执行阶段：executor 自管会话/环境（真实 Temporal dev server + worker + dispatcher）
    runtime_env = await RuntimeEvalEnvironment.start(sessions=sessions, context=context)
    async with runtime_env as env:
        from zhiwei.evals.executors.agent_runtime import AgentRuntimeExecutor

        executor = AgentRuntimeExecutor(env)
        outcomes = []
        for unit in RUNTIME_CONTRACT_UNITS:
            outcomes.append(await executor.execute(unit))

    # 落账阶段：EvalRun + outcomes + （可选）密封
    async with _tenant_session(sessions, context) as session:
        service = EvalFoundationService(session, context, store)
        provenance = _provenance_digests()
        created = await service.create(
            CreateEvalRunCommand(
                mode=mode,
                registered_units=RUNTIME_CONTRACT_UNITS,
                dataset_payload={
                    "suite": "runtime-contract-v1",
                    "registered_units": [
                        {"sample_id": unit.sample_id, "unit_id": unit.unit_id}
                        for unit in RUNTIME_CONTRACT_UNITS
                    ],
                },
                code_digest=provenance["code_digest"],
                config_digest=provenance["config_digest"],
                schema_digest=provenance["schema_digest"],
            )
        )
        for outcome in outcomes:
            await service.record_outcome(created.eval_run_id, outcome)

        status_counts: dict[str, int] = {}
        for outcome in outcomes:
            status_counts[outcome.status.value] = status_counts.get(outcome.status.value, 0) + 1
        all_terminal = all(
            outcome.status.value in {"completed", "failed", "refused", "error"}
            for outcome in outcomes
        )
        result: dict[str, Any] = {
            "suite": "runtime-contract-v1",
            "mode": mode.value,
            "executor": "agent-runtime",
            "registered_units": len(RUNTIME_CONTRACT_UNITS),
            "terminal_units": len(outcomes) if all_terminal else sum(
                1 for o in outcomes
                if o.status.value in {"completed", "failed", "refused", "error"}
            ),
            "status_counts": status_counts,
            "eval_run_id": str(created.eval_run_id),
            "organization_id": str(context.organization_id),
            "workspace_id": str(context.workspace_id),
        }
        if seal:
            if not all_terminal:
                raise RuntimeError("存在非终态单位，拒绝密封 runtime-contract-v1")
            migration_revision = await _migration_revision(session)
            sealed = await service.seal(
                created.eval_run_id,
                migration_revision=migration_revision,
                test_report={
                    "status": "passed" if all(
                        o.status.value == "completed" for o in outcomes
                    ) else "failed",
                    "scope": "runtime-contract-v1",
                    "command": "zhiwei eval run --suite runtime-contract-v1 --mode fixture --seal",
                    "status_counts": status_counts,
                },
            )
            result["sealed"] = True
            result["seal_digest"] = sealed.seal_digest
    return result


async def _knowledge_suite_flow(
    sessions: Any,
    context: TenantContext,
    store: PosixObjectStore,
    *,
    suite_name: str,
    mode: EvalMode,
    seal: bool,
) -> dict[str, Any]:
    """knowledge suite：registered units 经生产检索路径执行后统一落账。

    executor 由 suite 注册表绑定（Retrieve TaskHandler → Knowledge Planner）；
    corpus digest 进 dataset payload，使密封 artifact 可回溯到冻结语料字节。
    """
    suite = resolve_knowledge_suite(suite_name)
    if context.workspace_id is None:
        raise RuntimeError("run 需要 workspace 上下文")
    from zhiwei.persistence.tenant import tenant_session as _tenant_session

    async with _tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(context.organization_id, status="active")
        await repository.create_workspace(context.workspace_id, name=suite.name)

    executor = KnowledgeRetrievalExecutor(suite)
    outcomes = [await executor.execute(unit) for unit in suite.registered_units]

    async with _tenant_session(sessions, context) as session:
        service = EvalFoundationService(session, context, store)
        provenance = _provenance_digests()
        created = await service.create(
            CreateEvalRunCommand(
                mode=mode,
                registered_units=suite.registered_units,
                dataset_payload={
                    "suite": suite.name,
                    "corpus_path": suite.corpus_path.relative_to(REPO_ROOT).as_posix(),
                    "corpus_digest": suite.corpus_digest,
                    "production_path": suite.production_path,
                    "registered_units": [
                        {"sample_id": unit.sample_id, "unit_id": unit.unit_id}
                        for unit in suite.registered_units
                    ],
                },
                code_digest=provenance["code_digest"],
                config_digest=provenance["config_digest"],
                schema_digest=provenance["schema_digest"],
            )
        )
        for outcome in outcomes:
            await service.record_outcome(created.eval_run_id, outcome)

        status_counts: dict[str, int] = {}
        for outcome in outcomes:
            status_counts[outcome.status.value] = status_counts.get(outcome.status.value, 0) + 1
        all_terminal = all(
            outcome.status.value in {"completed", "failed", "refused", "error"}
            for outcome in outcomes
        )
        result: dict[str, Any] = {
            "suite": suite.name,
            "mode": mode.value,
            "executor": suite.executor_kind,
            "production_path": suite.production_path,
            "corpus_digest": suite.corpus_digest,
            "registered_units": len(suite.registered_units),
            "terminal_units": sum(
                1
                for outcome in outcomes
                if outcome.status.value in {"completed", "failed", "refused", "error"}
            ),
            "status_counts": status_counts,
            "eval_run_id": str(created.eval_run_id),
            "organization_id": str(context.organization_id),
            "workspace_id": str(context.workspace_id),
        }
        if seal:
            if not all_terminal:
                raise RuntimeError(f"存在非终态单位，拒绝密封 {suite.name}")
            migration_revision = await _migration_revision(session)
            sealed = await service.seal(
                created.eval_run_id,
                migration_revision=migration_revision,
                test_report={
                    "status": "passed"
                    if all(o.status.value == "completed" for o in outcomes)
                    else "failed",
                    "scope": suite.name,
                    "command": (
                        f"zhiwei eval run --suite {suite.name} "
                        f"--mode {mode.value} --seal"
                    ),
                    "status_counts": status_counts,
                },
            )
            result["sealed"] = True
            result["seal_digest"] = sealed.seal_digest
    return result


async def _external_status_flow(
    session: Any,
    context: TenantContext,
    store: PosixObjectStore,
    *,
    spec: ExternalAdapterSpec,
    root: Path,
    seal: bool,
) -> dict[str, Any]:
    """external-status：二选一 sealed artifact（specs/s7 §8）。

    - available：附数据许可/version/checksum 并实际运行 adapter（离线 corpus-integrity）；
    - unavailable：附缺失许可/数据的机器可读原因；
    - 两种 artifact 都不解锁外部基准质量 claim——LongMemEval claim 保持
      planned/unavailable（质量诊断需要 live 模型，由 operator 显式触发）。
    """
    if context.workspace_id is None:
        raise RuntimeError("external-status 需要 workspace 上下文")
    repository = TenantRepository(session, context)
    await repository.create_organization(context.organization_id, status="active")
    await repository.create_workspace(context.workspace_id, name=f"external-status:{spec.name}")

    probe = probe_adapter(spec, root=root)
    unit = RegisteredUnit(sample_id=f"external-status:{spec.name}", unit_id="availability-probe")
    run_result: dict[str, Any] | None = None
    if probe.status == "available":
        # available 即实际运行（离线完整性执行）；数据/schema 违例在此抛出 → fail closed。
        run_result = run_available_adapter(spec, probe, root=root)
        outcome_status = SampleStatus.COMPLETED
    else:
        outcome_status = SampleStatus.REFUSED
    outcome = SampleOutcome(
        unit=unit,
        status=outcome_status,
        result={
            "suite": spec.name,
            "benchmark": spec.benchmark,
            "external_status": probe.status,
            "run_kind": run_result["run_kind"] if run_result else "none",
            "reasons": list(probe.reasons),
            "run": run_result,
        },
    )

    service = EvalFoundationService(session, context, store)
    provenance = _provenance_digests()
    created = await service.create(
        CreateEvalRunCommand(
            mode=EvalMode.OFFLINE,
            registered_units=(unit,),
            dataset_payload={
                "suite": spec.name,
                "kind": "external-status",
                "benchmark": spec.benchmark,
                "external_status": probe.status,
                "reasons": list(probe.reasons),
                "expected_paths": {
                    "license": spec.license_file,
                    "version": spec.version_file,
                    "data_dir": spec.data_dir,
                },
            },
            code_digest=provenance["code_digest"],
            config_digest=provenance["config_digest"],
            schema_digest=provenance["schema_digest"],
        )
    )
    await service.record_outcome(created.eval_run_id, outcome)

    result: dict[str, Any] = {
        "suite": spec.name,
        "benchmark": spec.benchmark,
        "external_status": probe.status,
        "reasons": list(probe.reasons),
        "run_kind": run_result["run_kind"] if run_result else "none",
        # 机器可读的 claim 侧字段：外部基准质量 claim 不因本命令解锁。
        "claim": {"benchmark": spec.claim_id, "claim_status": "planned/unavailable"},
        "eval_run_id": str(created.eval_run_id),
        "organization_id": str(context.organization_id),
        "workspace_id": str(context.workspace_id),
    }
    if seal:
        migration_revision = await _migration_revision(session)
        sealed = await service.seal(
            created.eval_run_id,
            migration_revision=migration_revision,
            test_report={
                "status": probe.status,
                "scope": f"external-status:{spec.name}",
                "command": f"zhiwei eval external-status --suite {spec.name} --seal",
                "claim_status": "planned/unavailable",
                "reasons": list(probe.reasons),
            },
        )
        result["sealed"] = True
        result["seal_digest"] = sealed.seal_digest
    return result


async def _memory_suite_flow(
    sessions: Any,
    context: TenantContext,
    store: PosixObjectStore,
    *,
    mode: EvalMode,
    seal: bool,
) -> dict[str, Any]:
    """enterprise-memory-v1：代码定义的行为 units 经生产 memory 服务路径执行。"""
    from zhiwei.evals.executors.memory import MemoryLifecycleExecutor

    suite = resolve_memory_suite(ENTERPRISE_MEMORY_V1)
    await _prepare_s6_tenant(sessions, context, suite.name)
    executor = MemoryLifecycleExecutor(suite)
    outcomes = [await executor.execute(unit) for unit in suite.registered_units]
    return await _s6_suite_bookkeeping(
        sessions,
        context,
        store,
        suite=suite.name,
        mode=mode,
        seal=seal,
        outcomes=outcomes,
        registered_units=suite.registered_units,
        executor_name=suite.executor_kind,
        production_path=suite.production_path,
        dataset_extra={"unit_categories": sorted(ENTERPRISE_MEMORY_UNIT_CATEGORIES)},
    )


async def _prepare_s6_tenant(sessions: Any, context: TenantContext, suite: str) -> None:
    """S6 suite 的 tenant 准备：org/workspace 行必须先于生产命令路径存在。"""
    if context.workspace_id is None:
        raise RuntimeError("run 需要 workspace 上下文")
    from zhiwei.persistence.tenant import tenant_session as _tenant_session

    async with _tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(context.organization_id, status="active")
        await repository.create_workspace(context.workspace_id, name=suite)


async def _s6_suite_bookkeeping(
    sessions: Any,
    context: TenantContext,
    store: PosixObjectStore,
    *,
    suite: str,
    mode: EvalMode,
    seal: bool,
    outcomes: list[Any],
    registered_units: Any,
    executor_name: str,
    production_path: str,
    dataset_extra: dict[str, Any],
) -> dict[str, Any]:
    """S6 suite 的统一落账：EvalRun 创建 + outcomes + 可选密封。

    tenant（org/workspace）由调用方在执行前经 _prepare_s6_tenant 准备——
    ask-v1 的生产命令路径在落账前就依赖它们存在。
    """
    async with tenant_session(sessions, context) as session:
        service = EvalFoundationService(session, context, store)
        provenance = _provenance_digests()
        created = await service.create(
            CreateEvalRunCommand(
                mode=mode,
                registered_units=registered_units,
                dataset_payload={
                    "suite": suite,
                    "executor": executor_name,
                    "production_path": production_path,
                    "registered_units": [
                        {"sample_id": unit.sample_id, "unit_id": unit.unit_id}
                        for unit in registered_units
                    ],
                    **dataset_extra,
                },
                code_digest=provenance["code_digest"],
                config_digest=provenance["config_digest"],
                schema_digest=provenance["schema_digest"],
            )
        )
        for outcome in outcomes:
            await service.record_outcome(created.eval_run_id, outcome)

        status_counts: dict[str, int] = {}
        for outcome in outcomes:
            status_counts[outcome.status.value] = status_counts.get(outcome.status.value, 0) + 1
        all_terminal = all(
            outcome.status.value in {"completed", "failed", "refused", "error"}
            for outcome in outcomes
        )
        result: dict[str, Any] = {
            "suite": suite,
            "mode": mode.value,
            "executor": executor_name,
            "production_path": production_path,
            "registered_units": len(registered_units),
            "terminal_units": sum(
                1
                for outcome in outcomes
                if outcome.status.value in {"completed", "failed", "refused", "error"}
            ),
            "status_counts": status_counts,
            "eval_run_id": str(created.eval_run_id),
            "organization_id": str(context.organization_id),
            "workspace_id": str(context.workspace_id),
        }
        if seal:
            if not all_terminal:
                raise RuntimeError(f"存在非终态单位，拒绝密封 {suite}")
            migration_revision = await _migration_revision(session)
            sealed = await service.seal(
                created.eval_run_id,
                migration_revision=migration_revision,
                test_report={
                    "status": "passed"
                    if all(o.status.value == "completed" for o in outcomes)
                    else "failed",
                    "scope": suite,
                    "command": f"zhiwei eval run --suite {suite} --mode {mode.value} --seal",
                    "status_counts": status_counts,
                },
            )
            result["sealed"] = True
            result["seal_digest"] = sealed.seal_digest
    return result


async def _factqa_suite_flow(
    sessions: Any,
    context: TenantContext,
    store: PosixObjectStore,
    *,
    mode: EvalMode,
    seal: bool,
) -> dict[str, Any]:
    """factqa-v1：冻结题集 → 生产 Evidence 路径（snapshot 重放 → verifier 复算）。"""
    from zhiwei.evals.executors.factqa import FactQAEvidenceExecutor

    suite = resolve_factqa_suite()
    await _prepare_s6_tenant(sessions, context, suite.name)
    executor = FactQAEvidenceExecutor(suite)
    outcomes = [await executor.execute(unit) for unit in suite.registered_units]
    return await _s6_suite_bookkeeping(
        sessions,
        context,
        store,
        suite=suite.name,
        mode=mode,
        seal=seal,
        outcomes=outcomes,
        registered_units=suite.registered_units,
        executor_name=suite.executor_kind,
        production_path=suite.production_path,
        dataset_extra={"corpus_digest": suite.corpus_digest},
    )


async def _ask_suite_flow(
    sessions: Any,
    context: TenantContext,
    store: PosixObjectStore,
    *,
    mode: EvalMode,
    seal: bool,
) -> dict[str, Any]:
    """ask-v1：Ask 行为契约场景经生产 Runtime 命令路径执行（真实 Temporal dev server）。"""
    from zhiwei.evals.ask_contracts import ASK_V1_SUITE, ASK_V1_UNITS
    from zhiwei.evals.executors.ask import (
        AskRuntimeExecutor,
        build_ask_environment,
    )

    await _prepare_s6_tenant(sessions, context, ASK_V1_SUITE)
    environment = await build_ask_environment(sessions=sessions, context=context)
    try:
        executor = AskRuntimeExecutor(environment)
        outcomes = [await executor.execute(unit) for unit in ASK_V1_UNITS]
    finally:
        await environment.aclose()
    return await _s6_suite_bookkeeping(
        sessions,
        context,
        store,
        suite=ASK_V1_SUITE,
        mode=mode,
        seal=seal,
        outcomes=outcomes,
        registered_units=ASK_V1_UNITS,
        executor_name="agent-runtime",
        production_path="RunCommandService->AgentRunWorkflow->AskTaskGraph",
        dataset_extra={},
    )


async def _risk_suite_flow(
    sessions: Any,
    context: TenantContext,
    store: PosixObjectStore,
    *,
    suite: str,
    mode: EvalMode,
    seal: bool,
) -> dict[str, Any]:
    """S8 risk suite：units 经生产 detector→hypothesis→falsification 路径执行后统一落账。

    执行本身是离线确定性的（不触 DB）；落账与密封走与其它 suite 相同的生产
    EvalFoundationService 路径。falsification 一等指标进 dataset payload 与 test report。
    """
    from zhiwei.evals.executors.risk import (
        DiscoverBlindSuiteExecutor,
        NumericRiskSuiteExecutor,
    )
    from zhiwei.evals.risk_suites import resolve_risk_suite

    suite_def = resolve_risk_suite(suite)
    await _prepare_s6_tenant(sessions, context, suite_def.name)
    if suite == NUMERIC_RISK_V1:
        executor: Any = NumericRiskSuiteExecutor()
    else:
        executor = DiscoverBlindSuiteExecutor()
    outcomes = [await executor.execute(unit) for unit in suite_def.units]

    falsification = {
        key: value
        for outcome in outcomes
        for key, value in outcome.result.items()
        if key in {"falsification_coverage", "hypothesis_refutation_rate", "injected_refuted"}
    }
    correct = sum(1 for outcome in outcomes if outcome.result.get("correct") is True)
    return await _s6_suite_bookkeeping(
        sessions,
        context,
        store,
        suite=suite_def.name,
        mode=mode,
        seal=seal,
        outcomes=outcomes,
        registered_units=suite_def.units,
        executor_name=suite_def.executor_kind,
        production_path=suite_def.production_path,
        dataset_extra={
            "source": suite_def.source,
            "asset_digest": suite_def.asset_digest,
            "falsification": falsification,
            "units_correct": correct,
        },
    )


async def _run_flow_impl(
    session: Any,
    context: TenantContext,
    store: PosixObjectStore,
    *,
    suite: str,
    executor_name: str,
    mode: EvalMode,
    seal: bool = False,
) -> dict[str, Any]:
    if suite != "legacy-assets":
        raise ValueError(f"未知 suite: {suite}")
    if context.workspace_id is None:
        raise RuntimeError("run 需要 workspace 上下文")
    repository = TenantRepository(session, context)
    await repository.create_organization(context.organization_id, status="active")
    await repository.create_workspace(context.workspace_id, name="S0-T6")
    inventory = LegacyAssetInventory.load(REPO_ROOT / "evals")
    if executor_name == "agent-runtime":
        # agent-runtime executor 只绑定 runtime-contract-v1（生产 Runtime 命令路径），
        # 在 _runtime_contract_flow 内构造；legacy-assets 走 legacy/empty executor。
        raise ValueError(
            "agent-runtime executor requires --suite runtime-contract-v1"
        )
    executor = LegacyExecutor(inventory) if executor_name == "legacy" else EmptyExecutor()
    units = inventory.registered_units
    service = EvalFoundationService(session, context, store)
    provenance = _provenance_digests()
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
            code_digest=provenance["code_digest"],
            config_digest=provenance["config_digest"],
            schema_digest=provenance["schema_digest"],
        )
    )
    terminal_units = 0
    for unit in units:
        outcome = await executor.execute(unit)
        await service.record_outcome(created.eval_run_id, outcome)
        terminal_units += 1
    result: dict[str, Any] = {
        "mode": mode.value,
        "executor": executor_name,
        "registered_units": len(units),
        "terminal_units": terminal_units,
        "eval_run_id": str(created.eval_run_id),
        "organization_id": str(context.organization_id),
        "workspace_id": str(context.workspace_id),
    }
    if seal:
        migration_revision = await _migration_revision(session)
        sealed = await service.seal(
            created.eval_run_id,
            migration_revision=migration_revision,
            test_report={
                "status": "passed",
                "scope": "run-outcomes",
                "command": "zhiwei eval run --seal",
            },
        )
        result["sealed"] = True
        result["seal_digest"] = sealed.seal_digest
    return result


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
    sample_rows = (
        await session.scalars(
            select(EvalSample).where(
                EvalSample.organization_id == context.organization_id,
                EvalSample.workspace_id == context.workspace_id,
                EvalSample.eval_run_id == eval_run_id,
            )
        )
    ).all()
    status_counts = {
        status: sum(1 for row in sample_rows if row.status == status)
        for status in sorted({row.status for row in sample_rows})
    }
    all_terminal = bool(sample_rows) and all(
        row.status in {"completed", "failed", "refused", "error"}
        for row in sample_rows
    )
    sealed = await service.seal(
        eval_run_id,
        migration_revision=migration_revision,
        test_report={
            "status": "passed" if all_terminal else "partial",
            "scope": "run-outcomes",
            "command": "zhiwei eval seal",
            "sample_status_counts": status_counts,
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
    seal: bool = typer.Option(False, "--seal", help="执行完毕后密封 EvalRun"),
) -> None:
    """执行 suite 的全部注册单位（legacy-assets、runtime-contract-v1 与 knowledge suite，无 live 模型调用）。"""
    if executor not in _KNOWN_EXECUTORS:
        _fail(f"未知 executor: {executor}")
    if suite not in _KNOWN_SUITES:
        # suite 解析先于 runtime 依赖：未知 suite 不需要 DB 就该被拒绝
        _fail(f"未知 suite: {suite}")
    if suite in _REGISTRY_BOUND_SUITES and executor != "legacy":
        # knowledge/S6 suite 的 executor 由注册表绑定生产路径；指定 empty/agent-runtime
        # 会落账伪造结果或走错执行面，进入执行前拒绝。
        _fail(f"executor {executor!r} 不适用于 suite {suite}")
    _, _, _, store, sessions = _settings_runtime()
    context, _, _ = _fresh_tenant()
    if suite == "runtime-contract-v1":
        payload = asyncio.run(
            _runtime_contract_flow(sessions, context, store, mode=mode, seal=seal)
        )
        _emit_json(payload)
        return
    if suite in KNOWLEDGE_SUITE_NAMES:
        payload = asyncio.run(
            _knowledge_suite_flow(
                sessions, context, store, suite_name=suite, mode=mode, seal=seal
            )
        )
        _emit_json(payload)
        return
    if suite == "factqa-v1":
        payload = asyncio.run(
            _factqa_suite_flow(sessions, context, store, mode=mode, seal=seal)
        )
        _emit_json(payload)
        return
    if suite == "ask-v1":
        payload = asyncio.run(
            _ask_suite_flow(sessions, context, store, mode=mode, seal=seal)
        )
        _emit_json(payload)
        return
    if suite == ENTERPRISE_MEMORY_V1:
        payload = asyncio.run(
            _memory_suite_flow(sessions, context, store, mode=mode, seal=seal)
        )
        _emit_json(payload)
        return
    if suite in _S8_SUITE_NAMES:
        payload = asyncio.run(
            _risk_suite_flow(sessions, context, store, suite=suite, mode=mode, seal=seal)
        )
        _emit_json(payload)
        return
    payload = _run_flow(
        sessions,
        lambda s: _run_flow_impl(
            s, context, store, suite=suite, executor_name=executor, mode=mode, seal=seal
        ),
        context,
    )
    _emit_json(payload)


@app.command("external-status")
def external_status(
    suite: SUITE_ARG = "longmemeval-adapter",
    seal: SEAL_ARG = False,
    config: CONFIG_ARG = None,
    data_root: DATA_ROOT_ARG = None,
) -> None:
    """外部基准 adapter 可用性探测并密封二选一 artifact（specs/s7 §8）。

    available 附许可/version/checksum 并实际运行；unavailable 附机器可读缺失原因。
    两者都不解锁外部质量 claim（LongMemEval 保持 planned/unavailable）。
    """
    # adapter 解析先于 runtime 依赖：未知名称不需要 DB 就该被拒绝（fail closed）。
    try:
        spec = (
            resolve_external_adapter(suite)
            if config is None
            else _resolve_adapter_from_manifest(config, suite)
        )
    except LookupError as exc:
        _fail(str(exc))
    _, _, _, store, sessions = _settings_runtime()
    context, _, _ = _fresh_tenant()
    payload = _run_flow(
        sessions,
        lambda s: _external_status_flow(
            s, context, store, spec=spec, root=data_root or REPO_ROOT, seal=seal
        ),
        context,
    )
    _emit_json(payload)


def _resolve_adapter_from_manifest(config: Path, suite: str) -> ExternalAdapterSpec:
    """从显式清单解析 adapter（fixture 数据驱动 available 分支的注入点）。"""
    from zhiwei.evals.external import load_adapter_manifest

    for spec in load_adapter_manifest(config):
        if spec.name == suite:
            return spec
    raise LookupError(f"未知 external adapter: {suite}")


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
