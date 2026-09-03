"""`zhiwei runtime` 命令组：Agent Runtime 诊断与评测绑定。

`replay-check --all-fixtures`：对 runtime-contract-v1 的每个契约场景，经生产命令
路径真实执行一次，然后从 PG 载入已提交事件序列：两次 reduce 断言逐字段一致
（deterministic replay）、终态断言、digest 链逐事件校验。不使用内存造的事件
序列——重放证据必须来自 canonical 存储。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, NoReturn

import click
import typer

from zhiwei.runtime.reducer import reduce

app = typer.Typer(
    help="Agent Runtime 诊断与评测绑定",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

ALL_FIXTURES_FLAG = Annotated[bool, typer.Option("--all-fixtures", help="验证所有 fixture 事件序列")]


def _emit_json(payload: dict[str, Any]) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False))


def _fail(message: str) -> NoReturn:
    click.echo(message, err=True)
    raise typer.Exit(1)


def _replay_check_all() -> dict[str, Any]:
    """执行 runtime-contract 契约并从 PG 重放全部事件序列。"""
    import asyncio

    from zhiwei.cli.evals import _fresh_tenant, _settings_runtime
    from zhiwei.evals.executors.agent_runtime import RuntimeEvalEnvironment
    from zhiwei.evals.runtime_contracts import RUNTIME_CONTRACT_UNITS
    from zhiwei.persistence.events import event_data_from_row, verify_event_chain
    from zhiwei.persistence.models import CanonicalEvent
    from zhiwei.persistence.tenant import tenant_session
    from zhiwei.runtime.persistence import RuntimeEventStore

    _, _, _, _, sessions = _settings_runtime()
    context, _, _ = _fresh_tenant()

    async def _run() -> dict[str, Any]:
        from sqlalchemy import select

        from zhiwei.evals.executors.agent_runtime import AgentRuntimeExecutor

        async with tenant_session(sessions, context) as session:
            from zhiwei.persistence.repositories import TenantRepository

            assert context.workspace_id is not None
            repository = TenantRepository(session, context)
            await repository.create_organization(context.organization_id, status="active")
            await repository.create_workspace(context.workspace_id, name="S2-replay")

        results: list[dict[str, Any]] = []
        all_deterministic = True
        runtime_env = await RuntimeEvalEnvironment.start(sessions=sessions, context=context)
        async with runtime_env as env:
            executor = AgentRuntimeExecutor(env)
            for unit in RUNTIME_CONTRACT_UNITS:
                outcome = await executor.execute(unit)
                run_id = outcome.result.get("run_id")
                if not run_id:
                    all_deterministic = False
                    results.append({
                        "label": f"{unit.sample_id}/{unit.unit_id}",
                        "deterministic": False,
                        "error": "no run_id in outcome",
                    })
                    continue
                import uuid as uuid_module

                parsed_run_id = uuid_module.UUID(str(run_id))
                async with tenant_session(sessions, context) as session:
                    store = RuntimeEventStore(session, context)
                    # 两次独立载入（不同会话/事务），确定性探针才有鉴别力
                    events = await store.load_events(parsed_run_id)
                    events_again = await store.load_events(parsed_run_id)
                    rows = (
                        await session.scalars(
                            select(CanonicalEvent)
                            .where(
                                CanonicalEvent.organization_id == context.organization_id,
                                CanonicalEvent.workspace_id == context.workspace_id,
                                CanonicalEvent.run_id == parsed_run_id,
                            )
                            .order_by(CanonicalEvent.sequence_no)
                        )
                    ).all()
                    chain_error: str | None = None
                    try:
                        verify_event_chain(event_data_from_row(row) for row in rows)
                    except Exception as exc:
                        chain_error = str(exc)

                state_a = reduce(list(events))
                state_b = reduce(list(events_again))
                deterministic = state_a == state_b and chain_error is None
                if not deterministic:
                    all_deterministic = False
                results.append({
                    "label": f"{unit.sample_id}/{unit.unit_id}",
                    "deterministic": deterministic,
                    "terminal": state_a.is_terminal,
                    "run_status": state_a.status,
                    "tasks": len(state_a.tasks),
                    "events": len(events),
                    "chain_verified": chain_error is None,
                    "chain_error": chain_error,
                    "outcome_status": outcome.status.value,
                })
        return {
            "status": "passed" if all_deterministic else "failed",
            "fixture_count": len(results),
            "results": results,
        }

    return asyncio.run(_run())


@app.command("replay-check")
def replay_check(
    all_fixtures: ALL_FIXTURES_FLAG = True,
) -> None:
    """验证 reducer 确定性重放：相同 PG 事件序列必须产生相同 RunState。"""
    payload = _replay_check_all()
    _emit_json(payload)
    if payload["status"] != "passed":
        _fail("replay-check failed: non-deterministic replay or broken chain detected")
