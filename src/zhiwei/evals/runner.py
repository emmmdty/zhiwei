"""EvalRunner：薄编排层——驱动 executor 跑完 EvalRun 的 pending 单位。

职责边界：本模块不新增任何存储、不开第二套运行时路径。执行一律走
EvalFoundationService.record_outcome（行锁串行化 + 终态校验 + 持久化），
pending 单位从既有 EvalSample 行读出；pause/seal/verify 直接委托服务。

质量口径：executor 产出原始 SampleOutcome；注册了确定性 scorer 时，COMPLETED
结果由 scorer 计算 pass/fail 并写回 result["passed"]——scorer 是唯一质量权威，
executor 自带的通过标记只是 scorer 的输入，不是平行结论。scorer=None 时不打分：
executor 提供的 result["passed"]（如有）原样生效，runner 不代判也不覆盖。
refused/error 终态不再评分：它们已在完整分母内计为非成功，对其补打分等于
用缺失输出伪造质量信号。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from zhiwei.evals.domain import (
    EvalMode,
    RegisteredUnit,
    SampleOutcome,
    SampleStatus,
    is_terminal,
)
from zhiwei.evals.executors.base import EvalExecutor
from zhiwei.evals.runs import (
    CreatedEvalRun,
    CreateEvalRunCommand,
    EvalFoundationService,
    EvalRunState,
    RunPhase,
    SealedEvalRun,
)
from zhiwei.evals.scorers.base import Scorer, ScorerInput
from zhiwei.evals.sealing import SealedEvalArtifact
from zhiwei.object_store.ports import ObjectStore
from zhiwei.persistence.models import EvalRun, EvalSample
from zhiwei.persistence.tenant import TenantContext, TenantContextRequired
from zhiwei.telemetry.traces import SpanNames, start_span


class EvalRunnerError(RuntimeError):
    """runner 编排被违反（executor 返回非终态、注册单位错位等）时拒绝。"""


@runtime_checkable
class ReferenceLookup(Protocol):
    """scorer 参考答案的来源端口；实现由 dataset 冻结资产提供。"""

    def reference_for(self, unit: RegisteredUnit) -> dict[str, Any]: ...


class MappingReferenceLookup:
    """最简单的 ReferenceLookup：静态映射；缺失参考返回空 dict（fail closed）。"""

    def __init__(self, references: dict[tuple[str, str], dict[str, Any]]) -> None:
        self._references = dict(references)

    def reference_for(self, unit: RegisteredUnit) -> dict[str, Any]:
        # 缺失参考不抛异常也不猜测答案：空 reference 会让确定性 scorer 判
        # 不通过（保守口径），原始输出仍保留在 result 里供事后审计。
        return self._references.get((unit.sample_id, unit.unit_id), {})


class EvalRunner:
    """把 executor 与 EvalRun 状态机串起来；会话/租户上下文与调用方共享事务。"""

    def __init__(
        self,
        session: AsyncSession,
        context: TenantContext,
        store: ObjectStore,
        executor: EvalExecutor,
        *,
        scorer: Scorer | None = None,
        references: ReferenceLookup | None = None,
    ) -> None:
        if context is None or context.workspace_id is None:
            raise TenantContextRequired("eval runner requires workspace context")
        if scorer is not None and references is None:
            # 带 scorer 却没有参考来源，只能产出全不通过——这等价于伪造 0% 报告，
            # 构造期直接拒绝，而不是让运行「成功」地全红。
            raise EvalRunnerError("scorer requires a reference lookup")
        self._session = session
        self._context = context
        self._service = EvalFoundationService(session, context, store)
        self._executor = executor
        self._scorer = scorer
        self._references = references

    async def create(self, command: CreateEvalRunCommand) -> CreatedEvalRun:
        return await self._service.create(command)

    async def execute_pending(self, eval_run_id: UUID) -> tuple[SampleOutcome, ...]:
        """逐个执行 pending 单位并即时落库；任一单位失败不阻断其余单位。

        分母完整性优先：provider 抛错转成 ERROR 终态记录，让 seal 仍能以
        「全部 terminal」收口，而不是留下无法密封的 partial 残局。
        """
        recorded: list[SampleOutcome] = []
        units = await self._pending_units(eval_run_id)
        # S9 §6 eval span：record loop 是评分口径的权威落账点（分母完整性在
        # 此收口）。metadata-only：run 身份 + 单位计数；题目/输出/参考答案
        # 绝不进 span。start_span 不吞异常——单位失败已折叠为 ERROR 终态，
        # span 只观测不改变分母语义。
        with start_span(
            SpanNames.EVAL,
            {"eval_run_id": str(eval_run_id), "pending_units": len(units)},
        ) as span:
            for unit in units:
                outcome = await self._execute_unit(unit)
                await self._service.record_outcome(eval_run_id, outcome)
                recorded.append(outcome)
            span.set_attribute("recorded_units", len(recorded))
        return tuple(recorded)

    async def load_state(self, eval_run_id: UUID) -> EvalRunState:
        """只读重建 EvalRun 状态（tenant 范围内），供 sealing/统计复用。"""
        eval_run = await self._load_eval_run(eval_run_id)
        rows = list(
            (
                await self._session.scalars(
                    select(EvalSample)
                    .where(
                        EvalSample.organization_id == self._context.organization_id,
                        EvalSample.workspace_id == self._context.workspace_id,
                        EvalSample.eval_run_id == eval_run_id,
                    )
                    .order_by(EvalSample.sample_id, EvalSample.unit_id)
                )
            ).all()
        )
        units = tuple(
            RegisteredUnit(sample_id=row.sample_id, unit_id=row.unit_id) for row in rows
        )
        outcomes: list[SampleOutcome] = []
        for row in rows:
            try:
                status = SampleStatus(row.status)
            except ValueError as exc:
                raise EvalRunnerError(
                    f"eval sample status is unknown: {row.status!r}"
                ) from exc
            if is_terminal(status):
                outcomes.append(
                    SampleOutcome(
                        unit=RegisteredUnit(sample_id=row.sample_id, unit_id=row.unit_id),
                        status=status,
                        result=dict(row.result or {}),
                    )
                )
        try:
            mode = EvalMode(eval_run.mode)
            run_status = RunPhase(eval_run.status)
        except ValueError as exc:
            raise EvalRunnerError(f"eval run row is inconsistent: {exc}") from exc
        return EvalRunState.restore(
            mode=mode,
            registered_units=units,
            outcomes=tuple(outcomes),
            status=run_status,
            code_digest=eval_run.code_digest,
            config_digest=eval_run.config_digest,
            schema_digest=eval_run.schema_digest,
        )

    async def pause(self, eval_run_id: UUID) -> None:
        await self._service.pause(eval_run_id)

    async def resume(self, eval_run_id: UUID) -> None:
        await self._service.resume(eval_run_id)

    async def seal(
        self, eval_run_id: UUID, *, migration_revision: str, test_report: dict[str, Any]
    ) -> SealedEvalRun:
        return await self._service.seal(
            eval_run_id,
            migration_revision=migration_revision,
            test_report=test_report,
        )

    async def verify_sealed(self, eval_run_id: UUID) -> SealedEvalArtifact:
        return await self._service.verify_sealed(eval_run_id)

    async def _execute_unit(self, unit: RegisteredUnit) -> SampleOutcome:
        try:
            outcome = await self._executor.execute(unit)
        except Exception as exc:
            # provider 异常转 ERROR 终态：只记异常类型不记消息——异常文本可能
            # 携带连接串/密钥，metadata-only 纪律在这里同样适用。
            return SampleOutcome(
                unit=unit,
                status=SampleStatus.ERROR,
                result={"reason": "executor_error", "error": type(exc).__name__},
            )
        if outcome.unit != unit:
            raise EvalRunnerError(
                f"executor answered a different unit: asked {unit!r}, got {outcome.unit!r}"
            )
        if not is_terminal(outcome.status):
            raise EvalRunnerError(
                f"executor returned non-terminal status {outcome.status.value!r} "
                f"for unit {unit.sample_id!r}"
            )
        if outcome.status is not SampleStatus.COMPLETED or self._scorer is None:
            return outcome
        references = self._references
        if references is None:
            # 构造期已拒绝「scorer 而无 references」的组合；这里只是为类型收窄兜底。
            raise EvalRunnerError("scorer requires a reference lookup")
        try:
            verdict = self._scorer.score(
                ScorerInput(
                    unit=unit,
                    output=outcome.result,
                    reference=references.reference_for(unit),
                    context={},
                )
            )
        except Exception as exc:
            return SampleOutcome(
                unit=unit,
                status=SampleStatus.ERROR,
                result={"reason": "scorer_error", "error": type(exc).__name__},
            )
        return SampleOutcome(
            unit=unit,
            status=outcome.status,
            result={
                **outcome.result,
                "passed": verdict.passed,
                "score": verdict.score,
            },
        )

    async def _pending_units(self, eval_run_id: UUID) -> tuple[RegisteredUnit, ...]:
        await self._load_eval_run(eval_run_id)
        rows = (
            await self._session.scalars(
                select(EvalSample)
                .where(
                    EvalSample.organization_id == self._context.organization_id,
                    EvalSample.workspace_id == self._context.workspace_id,
                    EvalSample.eval_run_id == eval_run_id,
                )
                .order_by(EvalSample.sample_id, EvalSample.unit_id)
            )
        ).all()
        pending: list[RegisteredUnit] = []
        for row in rows:
            try:
                status = SampleStatus(row.status)
            except ValueError as exc:
                raise EvalRunnerError(
                    f"eval sample status is unknown: {row.status!r}"
                ) from exc
            if not is_terminal(status):
                pending.append(
                    RegisteredUnit(sample_id=row.sample_id, unit_id=row.unit_id)
                )
        return tuple(pending)

    async def _load_eval_run(self, eval_run_id: UUID) -> EvalRun:
        eval_run = await self._session.scalar(
            select(EvalRun).where(
                EvalRun.id == eval_run_id,
                EvalRun.organization_id == self._context.organization_id,
                EvalRun.workspace_id == self._context.workspace_id,
            )
        )
        if eval_run is None:
            raise EvalRunnerError("EvalRun is missing from tenant scope")
        return eval_run
