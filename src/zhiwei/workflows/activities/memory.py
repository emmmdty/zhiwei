"""S7-T4 Memory Activity for Temporal: memory retrieval and write through Activity boundary.

Per S7 spec §4/§5:
- Memory retrieval goes through Memory Activity
- Typed candidates produced through canonical events
- Write pipeline: policy evaluation → candidate queue or auto-confirm

事实源：S7 spec §4、§5、ADR-009。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from uuid import UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.contracts.canonical import canonical_json
from zhiwei.identity.domain import PrincipalKind
from zhiwei.memory.candidates import CandidateQueue, DedupKey
from zhiwei.memory.conflicts import TemporalConflictManager
from zhiwei.memory.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
    SourceRef,
)
from zhiwei.memory.events import (
    CANDIDATE_RECORDED_EVENT,
    PAYLOAD_SCHEMA_VERSION,
    REFUSAL_EVENT,
    MemoryLifecycleLedger,
    candidate_idempotency_key,
    candidate_payload,
    memory_event_schema_registry,
    refusal_idempotency_key,
    refusal_payload,
)
from zhiwei.memory.policy import evaluate_write_policy
from zhiwei.memory.repositories import PgMemoryRepository
from zhiwei.memory.retrieval import HardFilters, MemoryRetriever
from zhiwei.persistence.events import EventCommand
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.persistence.unit_of_work import CanonicalUnitOfWork
from zhiwei.runtime.triggers.discovery import BackgroundRunContext

logger = logging.getLogger(__name__)

# 服务级 fail-closed 语义：ServiceAccount principal（如 background Discover）永远
# 不能读取 personal memory（S7 spec §3）。显式针对 personal scope 的查询直接拒绝；
# 一般检索从结果中排除 USER scope 记录（personal_memory_excluded 标记供审计）。
_SERVICE_ACCOUNT_PERSONAL_DENIAL = (
    "service_account_personal_memory_denied: background ServiceAccount "
    "不能读取 personal memory"
)


def principal_kind_for_background_run(context: BackgroundRunContext) -> PrincipalKind:
    """从后台 run 的主体声明推导 MemoryActivityInput.principal_kind（显式入口）。

    principal_kind 必填后，组合根仍需一个从 run 主体声明到主体类型的显式推导点：
    后台 run（Discover trigger 等）的主体唯一来源是
    DiscoveryTriggerService.background_run_context 返回的 BackgroundRunContext
    （service identity，无创建者身份、无 personal memory）。取值不在
    PrincipalKind 枚举内时抛 ValueError——绝不回退为 USER（fail closed）。
    """
    return PrincipalKind(context.principal_kind)


@dataclass
class MemoryActivityInput:
    """Input for a memory activity execution.

    Carries the action (retrieve or write), query/filters, and ACL context
    for the Memory Activity to process.
    """

    run_id: str
    task_id: str
    attempt_no: int
    organization_id: str
    workspace_id: str
    principal_id: str
    action: str  # "retrieve" | "write"
    # 必填（S7 spec §3）：「ServiceAccount 不可读 personal memory」依赖主体类型被
    # 显式声明——带默认值 USER 时调用方漏传即静默获得 USER 语义（fail open）。
    # 组合根必须显式传 USER/SERVICE_ACCOUNT；后台 run 经
    # principal_kind_for_background_run 从 BackgroundRunContext 推导。
    principal_kind: PrincipalKind
    query: dict[str, Any] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    actor_ref: str = "agent-runtime:worker"


@dataclass
class MemoryActivityOutput:
    """Output from a memory activity execution.

    Carries the retrieval results or write outcome for the workflow
    to interpret and record as canonical events.
    """

    task_id: str
    status: str  # completed | refused | error
    action: str
    results: list[dict[str, Any]] = field(default_factory=list)
    result_count: int = 0
    record_id: str | None = None
    decision: str | None = None
    refusal_reason: str | None = None
    personal_memory_excluded: bool = False
    error: str | None = None


class MemoryWriteStore(Protocol):
    """Memory 写路径的持久化端口：记录 + canonical event 同事务（plan Task 2）。

    实现方负责：记录 upsert（ADR-009 去重）、candidate/refusal canonical event、
    生命周期台账 + 审计，全部在一个租户事务内提交或整体回滚。
    """

    async def commit_write(
        self,
        record: MemoryRecord,
        *,
        decision: str,
        run_id: UUID,
        task_id: UUID | None,
        actor_ref: str,
    ) -> MemoryRecord: ...

    async def commit_refusal(
        self,
        record: MemoryRecord,
        *,
        decision: str,
        reason: str,
        run_id: UUID,
        task_id: UUID | None,
        actor_ref: str,
    ) -> None: ...


class PgMemoryWriteStore:
    """MemoryWriteStore 的生产实现：每个写入一个原子租户事务。

    记录 upsert（PgMemoryRepository，含生命周期台账 + audit chain）与 canonical
    event（CanonicalUnitOfWork：canonical_events + audit + outbox）同事务提交，
    任一失败整体回滚——candidate/refusal 不会脱离事件链存在。
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def commit_write(
        self,
        record: MemoryRecord,
        *,
        decision: str,
        run_id: UUID,
        task_id: UUID | None,
        actor_ref: str,
    ) -> MemoryRecord:
        record = _ensure_record_identity(record)
        context = _tenant_context_of(record)
        async with tenant_session(self._sessions, context) as session:
            ledger = MemoryLifecycleLedger(session, context)
            repository = PgMemoryRepository(session, context, ledger=ledger)
            if decision == "auto_confirm":
                final_record = await repository.write_confirmed(record)
            else:
                final_record = await repository.add_candidate(record)
            uow = CanonicalUnitOfWork(
                session, context, schema_registry=memory_event_schema_registry()
            )
            await uow.append_event(
                EventCommand(
                    run_id=run_id,
                    event_type=CANDIDATE_RECORDED_EVENT,
                    payload_schema_version=PAYLOAD_SCHEMA_VERSION,
                    payload=candidate_payload(final_record, decision=decision),
                    actor_ref=actor_ref,
                    idempotency_key=candidate_idempotency_key(final_record),
                    task_id=task_id,
                )
            )
            return final_record

    async def commit_refusal(
        self,
        record: MemoryRecord,
        *,
        decision: str,
        reason: str,
        run_id: UUID,
        task_id: UUID | None,
        actor_ref: str,
    ) -> None:
        context = _tenant_context_of(record)
        payload = refusal_payload(record, decision=decision, reason=reason)
        async with tenant_session(self._sessions, context) as session:
            uow = CanonicalUnitOfWork(
                session, context, schema_registry=memory_event_schema_registry()
            )
            await uow.append_event(
                EventCommand(
                    run_id=run_id,
                    event_type=REFUSAL_EVENT,
                    payload_schema_version=PAYLOAD_SCHEMA_VERSION,
                    payload=payload,
                    actor_ref=actor_ref,
                    idempotency_key=refusal_idempotency_key(payload),
                    task_id=task_id,
                )
            )


def _tenant_context_of(record: MemoryRecord) -> TenantContext:
    return TenantContext(
        organization_id=record.organization_id, workspace_id=record.workspace_id
    )


# 确定性记录 id：activity 未携带 id 时由 canonical 输入内容派生（uuid5），
# Temporal 重试同 input 得到同 id → canonical event 幂等键稳定，不产生重复事件。
_MEMORY_ID_NAMESPACE = UUID("7ec3a352-9f7a-5e5a-9a8e-2b6f1c0d4e51")


def _ensure_record_identity(record: MemoryRecord) -> MemoryRecord:
    if record.id != UUID(int=0):
        return record
    seed = canonical_json(record.model_dump(mode="json", exclude={"id"}))
    return record.model_copy(update={"id": uuid5(_MEMORY_ID_NAMESPACE, seed.decode())})


def _parse_event_ids(
    input: MemoryActivityInput,
) -> tuple[UUID | None, UUID | None, str | None]:
    """Parse run/task identifiers for the canonical event path.

    canonical event 的幂等键作用于 Run——run_id 必须是可解析 UUID，否则 fail closed。
    """
    try:
        run_id = UUID(input.run_id)
    except ValueError:
        return None, None, f"invalid run_id for memory persistence: {input.run_id}"
    try:
        task_id = UUID(input.task_id)
    except ValueError:
        return run_id, None, f"invalid task_id for memory persistence: {input.task_id}"
    return run_id, task_id, None


def _run_id_required_output(input: MemoryActivityInput) -> MemoryActivityOutput:
    """持久化路径缺 run_id 时 fail closed：不写记录、不留痕失败可观测。

    run_id 是 canonical event 幂等键的定位依据，没有它既不能写记录也不能落
    refusal event——显式拒绝而不是带 None 落账（也替代被 strip 的 assert）。
    """
    return MemoryActivityOutput(
        task_id=input.task_id,
        status="refused",
        action=input.action,
        refusal_reason=(
            "persistent memory path requires a resolvable run_id for canonical events"
        ),
    )


def build_persistent_memory_activity(
    sessions: async_sessionmaker[AsyncSession],
) -> MemoryActivity:
    """组合根接线（S7 spec §7 / plan Task 2）：写路径经 PG repository 落账。

    policy/repository I/O 在 Memory Activity 执行，candidate/refusal 以 canonical
    event 同事务提交；Run 外生命周期转移在 PgMemoryRepository 内走 lifecycle
    ledger + audit chain。检索不经本工厂——由 Context Compiler 的 Memory port 完成。
    """
    return MemoryActivity(store=PgMemoryWriteStore(sessions))


class MemoryActivity:
    """Temporal activity boundary for memory operations.

    Orchestrates:
    1. Retrieve: parse filters → hard filter → multi-stage retrieval → rerank
    2. Write: parse memory → evaluate policy → candidate queue or auto-confirm
    """

    def __init__(
        self,
        retriever: MemoryRetriever | None = None,
        queue: CandidateQueue | None = None,
        conflict_manager: TemporalConflictManager | None = None,
        store: MemoryWriteStore | None = None,
    ) -> None:
        self._retriever = retriever or MemoryRetriever()
        self._queue = queue or CandidateQueue()
        self._conflict_manager = conflict_manager or TemporalConflictManager()
        # store 缺省走内存态队列（单测/评测执行路径）；生产组装经
        # build_persistent_memory_activity 注入 PG 写存储（S7 spec §7 / plan Task 2）
        self._store = store

    async def execute(self, input: MemoryActivityInput) -> MemoryActivityOutput:
        """Execute a memory activity.

        Args:
            input: Memory activity input with action, query/filters, and memory data.

        Returns:
            MemoryActivityOutput with results or write outcome.
        """
        try:
            if input.action == "retrieve":
                return self._execute_retrieve(input)
            elif input.action == "write":
                return await self._execute_write(input)
            else:
                return MemoryActivityOutput(
                    task_id=input.task_id,
                    status="error",
                    action=input.action,
                    error=f"unknown action: {input.action}",
                )
        except Exception as exc:
            # 如实呈现失败：返回 error payload 不是 Temporal 重试信号（activity
            # 正常返回对 Temporal 即成功）；需要平台侧重试的路径应显式抛
            # ApplicationError。error 字段只带异常类型名，内部细节（SQL 约束、
            # 路径等）留在日志，不回传给 workflow。
            logger.exception("Memory activity execution failed")
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="error",
                action=input.action,
                error=type(exc).__name__,
            )

    def _execute_retrieve(self, input: MemoryActivityInput) -> MemoryActivityOutput:
        """Execute a memory retrieval."""
        filters = self._build_filters(input.filters)
        personal_memory_excluded = False
        if input.principal_kind is PrincipalKind.SERVICE_ACCOUNT:
            if input.filters.get("scope") == MemoryScope.USER.value:
                # 显式针对 personal memory 的 ServiceAccount 查询：fail closed 拒绝。
                return MemoryActivityOutput(
                    task_id=input.task_id,
                    status="refused",
                    action="retrieve",
                    refusal_reason=_SERVICE_ACCOUNT_PERSONAL_DENIAL,
                )
            filters = replace(
                filters,
                excluded_scopes=frozenset({MemoryScope.USER}) | filters.excluded_scopes,
            )
            personal_memory_excluded = True

        query_text = input.query.get("text", "")
        query_key = input.query.get("key")
        query_embedding = input.query.get("embedding")
        top_k = input.query.get("top_k", 10)

        response = self._retriever.retrieve(
            query_text=query_text,
            filters=filters,
            query_key=query_key,
            query_embedding=query_embedding,
            top_k=top_k,
        )

        result_dicts = [
            {
                "record_id": str(r.record.id),
                "score": r.score,
                "reason": r.reason,
                "provenance": list(r.provenance),
                "conflicts": [str(c) for c in r.conflicts],
                "freshness_seconds": r.freshness_seconds,
                "subject": r.record.subject,
                "key": r.record.key,
                "canonical_value": r.record.canonical_value,
                "status": r.record.status.value,
            }
            for r in response.results
        ]

        return MemoryActivityOutput(
            task_id=input.task_id,
            status="completed",
            action="retrieve",
            results=result_dicts,
            result_count=response.total_passed,
            personal_memory_excluded=personal_memory_excluded,
        )

    async def _execute_write(self, input: MemoryActivityInput) -> MemoryActivityOutput:
        """Execute a memory write."""
        memory_dict = input.memory
        actor_id_str = input.principal_id

        try:
            actor_id = UUID(actor_id_str)
        except ValueError:
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="refused",
                action="write",
                refusal_reason=f"invalid principal_id: {actor_id_str}",
            )

        try:
            record = self._build_record(memory_dict, actor_id)
        except Exception as exc:
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="refused",
                action="write",
                refusal_reason=f"invalid memory record: {exc}",
            )

        # 持久化路径要求 run/task 可定位 canonical event（Run 作用域幂等键）；
        # 内存态路径（单测/评测执行）不做此要求
        run_id: UUID | None = None
        task_id: UUID | None = None
        if self._store is not None:
            run_id, task_id, parse_error = _parse_event_ids(input)
            if parse_error is not None:
                return MemoryActivityOutput(
                    task_id=input.task_id,
                    status="refused",
                    action="write",
                    refusal_reason=parse_error,
                )
        actor_ref = f"memory:write:{actor_id}"

        policy_result = evaluate_write_policy(
            scope=record.scope,
            mem_type=record.type,
            sensitivity=record.sensitivity,
            subject=record.subject,
            canonical_value=record.canonical_value,
        )

        if policy_result.decision == "forbidden":
            if self._store is not None:
                # refusal 与 candidate 同为 canonical event（plan Task 2）；落账
                # 失败会抛出并由 execute() 转为 error——绝不静默放过未留痕的拒绝
                if run_id is None:
                    return _run_id_required_output(input)
                await self._store.commit_refusal(
                    record,
                    decision=policy_result.decision,
                    reason=policy_result.reason,
                    run_id=run_id,
                    task_id=task_id,
                    actor_ref=actor_ref,
                )
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="refused",
                action="write",
                refusal_reason=policy_result.reason,
                decision=policy_result.decision,
            )

        if self._store is not None:
            # 持久化路径：任何失败都向上抛给 execute()（转为 error output 返回——
            # 注意这不是 Temporal 重试信号），不与 policy 拒绝（refused）混淆
            if run_id is None:
                return _run_id_required_output(input)
            pending = (
                record.model_copy(update={"status": MemoryStatus.CONFIRMED})
                if policy_result.decision == "auto_confirm"
                else record
            )
            final_record = await self._store.commit_write(
                pending,
                decision=policy_result.decision,
                run_id=run_id,
                task_id=task_id,
                actor_ref=actor_ref,
            )
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="completed",
                action="write",
                record_id=str(final_record.id),
                decision=policy_result.decision,
            )

        try:
            if policy_result.decision == "auto_confirm":
                confirmed = record.model_copy(update={"status": MemoryStatus.CONFIRMED})
                dedup = DedupKey.from_record(confirmed)
                self._queue.records[dedup.as_tuple()] = confirmed
                final_record = confirmed
            else:
                final_record = self._queue.add_candidate(record)
        except Exception as exc:
            return MemoryActivityOutput(
                task_id=input.task_id,
                status="refused",
                action="write",
                refusal_reason=f"queue error: {exc}",
                decision=policy_result.decision,
            )

        return MemoryActivityOutput(
            task_id=input.task_id,
            status="completed",
            action="write",
            record_id=str(final_record.id),
            decision=policy_result.decision,
        )

    @staticmethod
    def _build_filters(filters_dict: dict[str, Any]) -> HardFilters:
        """Build HardFilters from dict representation."""
        return HardFilters(
            organization_id=UUID(filters_dict["organization_id"])
            if "organization_id" in filters_dict
            else None,
            workspace_id=UUID(filters_dict["workspace_id"])
            if "workspace_id" in filters_dict
            else None,
            scope_subject_id=UUID(filters_dict["scope_subject_id"])
            if "scope_subject_id" in filters_dict
            else None,
            allowed_principals=frozenset(filters_dict.get("allowed_principals", [])),
            max_sensitivity=SensitivityLevel(filters_dict["max_sensitivity"])
            if "max_sensitivity" in filters_dict
            else None,
            allowed_statuses=frozenset(
                MemoryStatus(s) for s in filters_dict["allowed_statuses"]
            )
            if "allowed_statuses" in filters_dict
            else None,
            allowed_profile_refs=frozenset(filters_dict.get("allowed_profile_refs", []))
            if "allowed_profile_refs" in filters_dict
            else None,
        )

    @staticmethod
    def _build_record(memory_dict: dict[str, Any], actor_id: UUID) -> MemoryRecord:
        """Build a MemoryRecord from a dict."""
        now_str = memory_dict.get("created_at", "2025-01-01T00:00:00+00:00")

        source_refs_raw = memory_dict.get("source_refs", [])
        source_refs = tuple(
            SourceRef(
                source_id=sr.get("source_id", ""),
                source_type=sr.get("source_type", ""),
                description=sr.get("description", ""),
            )
            for sr in source_refs_raw
        )

        return MemoryRecord(
            id=UUID(memory_dict["id"]) if "id" in memory_dict else UUID(int=0),
            version=memory_dict.get("version", 1),
            organization_id=UUID(memory_dict["organization_id"]),
            workspace_id=UUID(memory_dict["workspace_id"]),
            scope=MemoryScope(memory_dict["scope"]),
            scope_subject_id=UUID(memory_dict["scope_subject_id"]),
            type=MemoryType(memory_dict["type"]),
            subject=memory_dict["subject"],
            key=memory_dict["key"],
            canonical_value=memory_dict["canonical_value"],
            source_refs=source_refs,
            observed_at=memory_dict.get("observed_at", now_str),
            confidence=memory_dict.get("confidence", 0.5),
            sensitivity=SensitivityLevel(memory_dict.get("sensitivity", "low")),
            status=MemoryStatus(memory_dict.get("status", "candidate")),
            author_ref=UUID(memory_dict.get("author_ref", str(actor_id))),
            created_at=memory_dict.get("created_at", now_str),
            updated_at=memory_dict.get("updated_at", now_str),
        )
