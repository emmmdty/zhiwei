"""S10-T2：Agent Studio draft API——PG 持久 draft revision + ETag/CAS。

事实源：specs/s10 §3、plan Task 2；冻结契约
tests/contract/api/test_agents_studio_frozen.py（GREEN 阶段不得修改）。

语义要点：

- draft 是 agent_definitions 的当前工作态（0016 增补列）：每次保存按
  「WHERE revision = 期望值」条件 UPDATE 原子自增——单条语句完成 CAS，不依赖
  FOR UPDATE 锁语义；ETag = 强 ETag（"revision"）；
- If-Match 缺失 → 428、过期 → 412，响应体顶级携带 machine-readable reason
  （契约断言 body["reason"]，与 HTTPException 的 detail 嵌套形状不同，故这两个
  拒绝面走 JSONResponse）；
- 无旁路生命周期写：不存在 PATCH status 路由；发布只经 S9 release commands
  （POST /{id}/releases → ReleaseService.create_draft，agent_builder 走
  agent_publish.request cell，与 api/releases.py 先例同型）；
- validate 只读：请求体 parse 为 TaskGraph（环 / 依赖不一致 / 重复 task_id 在
  构造期拒绝 → 422），validate_studio_graph 以 agent 记录的 declared
  capabilities 求值，返回冻结 issue 代码；draft 允许非法中间态，校验不阻塞保存；
- DELETE 移除（S2 内存版遗留端点）：grep apps/web 对 /api/v1/agents 零消费；
  draft 退役属生命周期语义，裸 DELETE 会绕过 release 状态机——与「无旁路生命
  周期写」同纪律；
- PUT 为部分文档合并（UI 恒发送全量 draft）；缺省字段保持存储值，避免部分
  客户端丢数据；task_graph 只做「是 JSON 对象」的结构门，语义合法性由 validate
  端点报告（draft 是允许非法的中间态，发布合法性由 S9 release Gate 把关）。

【已知策略缺口，需设计方裁决】冻结 Rego 矩阵中 agent_draft.read 仅授
workspace_admin，auditor 持有的是 read_version_gate（版本门读 cell）而非 draft
读 cell——真实策略下 builder 无法回读自己创建的 draft。本 router 读路径按
agent_draft.read 接线，不自行放宽矩阵；测试与 UI mock 走 allow-all fake。

工厂模式同 api/releases.py：组合期注入 actor 依赖 / session factory / policy
enforcer；endpoint 内不做授权语义决策。注意本模块【不用】from __future__ import
annotations——endpoint 签名里的 `Annotated[ActorContext, Depends(actor_dependency)]`
引用工厂闭包变量，必须在 def 期立即求值才能被 FastAPI 解析。
"""

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Annotated, Any, Literal, NamedTuple, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from zhiwei.agents.release import ReleaseManifest, ReleaseService
from zhiwei.agents.rollout import RollbackPolicy, RolloutPolicy
from zhiwei.agents.task_graph import TaskGraph, validate_studio_graph
from zhiwei.api.policy_gate import (
    append_allowed_audit,
    append_failed_mutation_audit,
    authorize_mutation,
    authorize_read,
    request_trace,
)
from zhiwei.api.releases import ReleaseView
from zhiwei.contracts.identifiers import new_id
from zhiwei.contracts.time import utc_now
from zhiwei.identity.domain import ActorContext
from zhiwei.persistence.models import AgentDefinition as AgentDefinitionRow
from zhiwei.persistence.models import AgentVersion
from zhiwei.persistence.models import EvalRun as EvalRunRow
from zhiwei.persistence.models import Run as RunRow
from zhiwei.persistence.tenant import TenantContext, tenant_session
from zhiwei.policy.enforcement import PolicyEnforcer
from zhiwei.policy.roles import Action, Purpose, ResourceType


class CreateAgentDraftRequest(BaseModel):
    """POST /agents 请求体（冻结契约只发送 name/description/capabilities）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    instructions: str = ""
    capabilities: list[str] = []


class UpdateAgentDraftRequest(BaseModel):
    """PUT /agents/{id} 请求体：全字段可选（部分文档合并，见模块 docstring）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    instructions: str | None = None
    capabilities: list[str] | None = None
    task_graph: dict[str, Any] | None = None


class CreateAgentReleaseRequest(BaseModel):
    """POST /agents/{id}/releases 请求体：依赖 digest 由调用方提供；agent_id/
    agent_version 从 agent 记录派生（不经请求体声明，杜绝跨 agent 引用）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pack_digest: str
    model_digest: str
    knowledge_digest: str
    memory_digest: str
    capability_digest: str
    policy_digest: str
    eval_digests: list[str] = []
    approver: str | None = None
    rollout: RolloutPolicy
    rollback: RollbackPolicy


class AgentDraftView(BaseModel):
    """draft 投影：AgentDraft JSON 形状（agent_id 是 API 身份，区别于行内 id）。"""

    model_config = ConfigDict(frozen=True)

    agent_id: UUID
    name: str
    description: str
    instructions: str
    capabilities: list[str]
    task_graph: dict[str, Any] | None
    revision: int
    lifecycle: str
    updated_at: datetime


class AgentDiffField(BaseModel):
    """版本 diff 字段条目（S10-T3）：kind 词汇冻结为五类，budget 待版本快照
    存储落库后由任务图派生（0016 迁移 docstring 明确把 revision 历史留给后续
    表）——当前存储不出该 kind，词汇先行冻结以保证 UI 渲染面稳定。"""

    model_config = ConfigDict(frozen=True)

    field: str
    # wire 键是保留字 "from"，只能落在序列化别名上（验证别名取字段名——
    # pydantic/pyright 会按验证别名合成 __init__，保留字无法作 kwargs）
    from_value: str | None = Field(default=None, serialization_alias="from")
    to: str | None = None
    kind: Literal["dependency", "permission", "budget", "schema", "other"]


class AgentRevisionDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    fields: list[AgentDiffField]


class ReleaseReadinessCheck(BaseModel):
    """一条就绪检查结果：可计算的检查真算（缺数据=missing，绝不假设满足）；
    无法从 agent 记录泛化计算的检查以 kind=unknown 如实呈报原因（fail closed，
    unknown 与 missing 同样不计入 ready）。"""

    model_config = ConfigDict(frozen=True)

    kind: Literal["eval_seal", "connection", "capability_publish", "unknown"]
    detail: str


class ReleaseReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    ready: bool
    missing: list[ReleaseReadinessCheck]


class _RevisionSnapshot(NamedTuple):
    """一个 revision 参与字段级 diff 的全部已存储事实（缺项=该侧未记录）。"""

    schema_version: int | None
    content_digest: str | None
    manifest: ReleaseManifest | None


# release manifest 字段 → diff kind：依赖 digest 集（pack/model/knowledge/memory）
# 是依赖面，capability/policy digest 是权限面——manifest 是版本间唯一有字段级
# 落库记录的差异面（api/releases.py ReleaseManifest 冻结投影）。
_MANIFEST_DIFF_KINDS: Mapping[str, Literal["dependency", "permission"]] = {
    "pack_digest": "dependency",
    "model_digest": "dependency",
    "knowledge_digest": "dependency",
    "memory_digest": "dependency",
    "capability_digest": "permission",
    "policy_digest": "permission",
}


async def _resolve_revision_snapshot(
    session: AsyncSession,
    context: TenantContext,
    row: AgentDefinitionRow,
    revision: int,
) -> _RevisionSnapshot:
    """revision → 参与 diff 的已存事实。agent_versions 行优先（不可变既定参照，
    同号冲突时压过移动中的 draft）；draft 只有当前 revision 可寻址——0016 不存
    revision 历史，draft 侧可参与 diff 的事实只有 schema_version（历史快照表
    落库后 budget/依赖等任务图派生字段随之接入，见迁移 docstring）。"""
    version_row = await session.scalar(
        select(AgentVersion).where(
            AgentVersion.organization_id == context.organization_id,
            AgentVersion.workspace_id == context.workspace_id,
            AgentVersion.agent_definition_id == row.id,
            AgentVersion.version == revision,
        )
    )
    if version_row is not None:
        # 同 agent_version 的多个 release 取最新（list 按 created_at, id 升序，
        # 反向首个即最新）；manifest 是发布时刻冻结的不可变依赖记录
        releases = await ReleaseService(session, context).list()
        manifest = next(
            (
                record.manifest
                for record in reversed(releases)
                if record.agent_id == row.id and record.agent_version == revision
            ),
            None,
        )
        return _RevisionSnapshot(
            schema_version=version_row.schema_version,
            content_digest=version_row.content_digest,
            manifest=manifest,
        )
    if revision == row.revision:
        return _RevisionSnapshot(
            schema_version=row.schema_version, content_digest=None, manifest=None
        )
    raise LookupError(f"revision {revision} not found for agent {row.id}")


def _diff_revisions(
    from_snapshot: _RevisionSnapshot, to_snapshot: _RevisionSnapshot
) -> AgentRevisionDiff:
    """只比较两侧都有记录的字段：单侧未记录的一律不产出条目（不发明 from/to 值）。"""
    fields: list[AgentDiffField] = []
    if (
        from_snapshot.schema_version is not None
        and to_snapshot.schema_version is not None
        and from_snapshot.schema_version != to_snapshot.schema_version
    ):
        fields.append(
            AgentDiffField(
                field="schema_version",
                from_value=str(from_snapshot.schema_version),
                to=str(to_snapshot.schema_version),
                kind="schema",
            )
        )
    if (
        from_snapshot.content_digest is not None
        and to_snapshot.content_digest is not None
        and from_snapshot.content_digest != to_snapshot.content_digest
    ):
        fields.append(
            AgentDiffField(
                field="content_digest",
                from_value=from_snapshot.content_digest,
                to=to_snapshot.content_digest,
                kind="other",
            )
        )
    from_manifest = from_snapshot.manifest
    to_manifest = to_snapshot.manifest
    if from_manifest is not None and to_manifest is not None:
        for field_name, kind in _MANIFEST_DIFF_KINDS.items():
            previous = getattr(from_manifest, field_name)
            current = getattr(to_manifest, field_name)
            if previous != current:
                fields.append(
                    AgentDiffField(field=field_name, from_value=previous, to=current, kind=kind)
                )
        if list(from_manifest.eval_digests) != list(to_manifest.eval_digests):
            fields.append(
                AgentDiffField(
                    field="eval_digests",
                    from_value=", ".join(from_manifest.eval_digests) or None,
                    to=", ".join(to_manifest.eval_digests) or None,
                    kind="other",
                )
            )
    return AgentRevisionDiff(fields=fields)


def _parse_if_match(header: str) -> int | None:
    """If-Match → 期望 revision；弱 ETag / 非整数一律拒绝（fail closed，不猜）。"""
    value = header.strip()
    if value.startswith("W/"):
        return None
    if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
        return None
    inner = value[1:-1]
    if not inner.isdigit():
        return None
    return int(inner)


def _refusal(code: int, reason: str, message: str) -> JSONResponse:
    # CAS 拒绝面走顶级 reason 字段（冻结契约断言 body["reason"]；HTTPException
    # 会把 detail 嵌套在 detail 键下，形状不符）。
    return JSONResponse(status_code=code, content={"reason": reason, "message": message})


def create_agents_router(
    *,
    actor_dependency: Callable[[], ActorContext],
    sessions: async_sessionmaker[AsyncSession],
    policy_enforcer: PolicyEnforcer,
) -> APIRouter:
    """组合期接线 agents draft router（依赖缺失即 TypeError，fail closed）。"""
    if policy_enforcer is None:
        raise TypeError("policy_enforcer must be provided (fail closed)")
    router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

    def _tenant(actor: ActorContext) -> TenantContext:
        if actor.organization_id is None or actor.workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="organization and workspace context required",
            )
        return TenantContext(
            organization_id=actor.organization_id, workspace_id=actor.workspace_id
        )

    def _view(row: AgentDefinitionRow) -> AgentDraftView:
        return AgentDraftView(
            agent_id=row.id,
            name=row.name,
            description=row.description,
            instructions=row.instructions,
            capabilities=list(row.capabilities or []),
            task_graph=row.task_graph,
            revision=row.revision,
            lifecycle=row.lifecycle,
            updated_at=row.updated_at,
        )

    @router.get("", response_model=list[AgentDraftView])
    async def list_agents(
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> list[AgentDraftView]:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_DRAFT,
            policy_action=Action.READ,
            resource_id=context.organization_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            rows = (
                await session.scalars(
                    select(AgentDefinitionRow)
                    .where(
                        AgentDefinitionRow.organization_id == context.organization_id,
                        AgentDefinitionRow.workspace_id == context.workspace_id,
                    )
                    .order_by(AgentDefinitionRow.created_at, AgentDefinitionRow.id)
                )
            ).all()
        return [_view(row) for row in rows]

    @router.post("", status_code=status.HTTP_201_CREATED, response_model=AgentDraftView)
    async def create_agent(
        request: CreateAgentDraftRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> AgentDraftView:
        context = _tenant(actor)
        agent_id = new_id()
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action="agent.draft.create",
            resource_type="agent_draft",
            policy_type=ResourceType.AGENT_DRAFT,
            policy_action=Action.CREATE_EDIT_RUN,
            resource_id=agent_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        now = utc_now()
        async with tenant_session(sessions, context) as session:
            row = AgentDefinitionRow(
                id=agent_id,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                name=request.name,
                lifecycle="draft",
                schema_version=1,
                description=request.description,
                instructions=request.instructions,
                capabilities=list(request.capabilities),
                task_graph=None,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="agent.draft.create",
                resource_type="agent_draft",
                resource_id=agent_id,
                resource_version=1,
                authorization=authorization,
            )
        return _view(row)

    @router.get("/{agent_id}", response_model=AgentDraftView)
    async def get_agent(
        agent_id: UUID,
        request_scope: Request,
        response: Response,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> AgentDraftView:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_DRAFT,
            policy_action=Action.READ,
            resource_id=agent_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            row = await session.scalar(
                select(AgentDefinitionRow).where(
                    AgentDefinitionRow.id == agent_id,
                    AgentDefinitionRow.organization_id == context.organization_id,
                    AgentDefinitionRow.workspace_id == context.workspace_id,
                )
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="agent not found"
            )
        # ETag 在响应头（CAS 前置：先读后写）；response 参数仅用于携带头字段
        response.headers["ETag"] = f'"{row.revision}"'
        return _view(row)

    @router.put("/{agent_id}", response_model=AgentDraftView)
    async def update_agent(
        agent_id: UUID,
        request: UpdateAgentDraftRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> JSONResponse:
        context = _tenant(actor)
        if_match = request_scope.headers.get("if-match")
        if not if_match:
            return _refusal(
                status.HTTP_428_PRECONDITION_REQUIRED,
                "if_match_required",
                "If-Match header required for draft writes",
            )
        expected = _parse_if_match(if_match)
        if expected is None:
            return _refusal(
                status.HTTP_400_BAD_REQUEST,
                "if_match_invalid",
                "If-Match must be a strong ETag quoting the current revision",
            )
        request_id, trace_id = request_trace(request_scope)
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action="agent.draft.update",
            resource_type="agent_draft",
            policy_type=ResourceType.AGENT_DRAFT,
            policy_action=Action.CREATE_EDIT_RUN,
            resource_id=agent_id,
            resource_version=expected,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            row = await session.scalar(
                select(AgentDefinitionRow).where(
                    AgentDefinitionRow.id == agent_id,
                    AgentDefinitionRow.organization_id == context.organization_id,
                    AgentDefinitionRow.workspace_id == context.workspace_id,
                )
            )
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="agent not found"
                )
            # 条件 UPDATE：WHERE 带客户端期望的 revision（CAS）——并发写者只有
            # 一个 rowcount=1，落败方在语句级被拒（无锁依赖，READ COMMITTED 下
            # 同样正确）。注意匹配的是 expected 而非存储值：存储值恒自匹配。
            values: dict[str, Any] = {
                "revision": expected + 1,
                "updated_at": utc_now(),
            }
            if request.name is not None:
                values["name"] = request.name
            if request.description is not None:
                values["description"] = request.description
            if request.instructions is not None:
                values["instructions"] = request.instructions
            if request.capabilities is not None:
                values["capabilities"] = list(request.capabilities)
            if request.task_graph is not None:
                values["task_graph"] = request.task_graph
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(AgentDefinitionRow)
                    .where(
                        AgentDefinitionRow.id == agent_id,
                        AgentDefinitionRow.organization_id == context.organization_id,
                        AgentDefinitionRow.workspace_id == context.workspace_id,
                        AgentDefinitionRow.revision == expected,
                    )
                    .values(**values)
                ),
            )
            if result.rowcount == 0:
                return _refusal(
                    status.HTTP_412_PRECONDITION_FAILED,
                    "revision_conflict",
                    f"stored revision is no longer {expected}",
                )
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="agent.draft.update",
                resource_type="agent_draft",
                resource_id=agent_id,
                resource_version=expected + 1,
                authorization=authorization,
            )
        updated = AgentDraftView(
            agent_id=row.id,
            name=values.get("name", row.name),
            description=values.get("description", row.description),
            instructions=values.get("instructions", row.instructions),
            capabilities=values.get("capabilities", list(row.capabilities or [])),
            task_graph=values.get("task_graph", row.task_graph),
            revision=expected + 1,
            lifecycle=row.lifecycle,
            updated_at=values["updated_at"],
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=updated.model_dump(mode="json"),
            headers={"ETag": f'"{expected + 1}"'},
        )

    @router.post("/{agent_id}/validate")
    async def validate_agent(
        agent_id: UUID,
        body: dict[str, Any],
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> dict[str, Any]:
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_DRAFT,
            policy_action=Action.READ,
            resource_id=agent_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            row = await session.scalar(
                select(AgentDefinitionRow).where(
                    AgentDefinitionRow.id == agent_id,
                    AgentDefinitionRow.organization_id == context.organization_id,
                    AgentDefinitionRow.workspace_id == context.workspace_id,
                )
            )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="agent not found"
            )
        try:
            graph = TaskGraph.model_validate(body)
        except ValidationError as exc:
            # 环 / 依赖不一致 / 重复 task_id 在构造期拒绝（fail closed），不发 issues
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        issues = validate_studio_graph(
            graph, declared_capabilities=frozenset(row.capabilities or [])
        )
        return {"issues": [issue.model_dump() for issue in issues]}

    @router.post(
        "/{agent_id}/releases",
        status_code=status.HTTP_201_CREATED,
        response_model=ReleaseView,
    )
    async def create_agent_release(
        agent_id: UUID,
        request: CreateAgentReleaseRequest,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ReleaseView:
        context = _tenant(actor)
        request_id, trace_id = request_trace(request_scope)
        # builder 的发布请求（矩阵：agent_builder 对 publish 只能 request，与
        # api/releases.py 同 cell——Studio 不引入第二套发布入口）
        authorization = await authorize_mutation(
            enforcer=policy_enforcer,
            sessions=sessions,
            actor=actor,
            bootstrap=False,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            audit_action="agent.release.create",
            resource_type="agent_release",
            policy_type=ResourceType.AGENT_PUBLISH,
            policy_action=Action.REQUEST,
            resource_id=agent_id,
            resource_version=1,
            purpose=Purpose.GENERAL,
            request_id=request_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            row = await session.scalar(
                select(AgentDefinitionRow).where(
                    AgentDefinitionRow.id == agent_id,
                    AgentDefinitionRow.organization_id == context.organization_id,
                    AgentDefinitionRow.workspace_id == context.workspace_id,
                )
            )
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="agent not found"
                )
            next_version = await session.scalar(
                select(func.max(AgentVersion.version)).where(
                    AgentVersion.organization_id == context.organization_id,
                    AgentVersion.workspace_id == context.workspace_id,
                    AgentVersion.agent_definition_id == agent_id,
                )
            )
            agent_version = (next_version or 0) + 1
            try:
                manifest = ReleaseManifest(
                    agent_id=agent_id,
                    agent_version=agent_version,
                    pack_digest=request.pack_digest,
                    model_digest=request.model_digest,
                    knowledge_digest=request.knowledge_digest,
                    memory_digest=request.memory_digest,
                    capability_digest=request.capability_digest,
                    policy_digest=request.policy_digest,
                    eval_digests=tuple(request.eval_digests),
                    approver=request.approver or str(actor.principal_id),
                    rollout=request.rollout,
                    rollback=request.rollback,
                )
            except (ValidationError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
                ) from exc
            try:
                record = await ReleaseService(session, context).create_draft(manifest)
            except Exception as error:
                await append_failed_mutation_audit(
                    sessions,
                    actor=actor,
                    organization_id=context.organization_id,
                    workspace_id=context.workspace_id,
                    action="agent.release.create",
                    resource_type="agent_release",
                    resource_id=agent_id,
                    error=error,
                    request_id=authorization.request_id,
                    trace_id=authorization.trace_id,
                )
                raise
            await append_allowed_audit(
                session,
                actor=actor,
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                action="agent.release.create",
                resource_type="agent_release",
                resource_id=record.release_id,
                resource_version=1,
                authorization=authorization,
            )
        return ReleaseView(
            release_id=str(record.release_id),
            agent_id=str(record.agent_id),
            agent_version=record.agent_version,
            state=record.state.value,
            manifest_digest=record.manifest.content_digest,
            default_version=record.rollout.default_version,
        )

    @router.get("/{agent_id}/release-readiness", response_model=ReleaseReadiness)
    async def get_release_readiness(
        agent_id: UUID,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
    ) -> ReleaseReadiness:
        """发布就绪检查（只读支撑面，S10-T3）：eval_seal 经 PG 绑定链真算；
        connection/capability_publish 的状态在 capability hub 的进程内存储里、
        与 agent 记录之间没有任何持久化关联，无法泛化计算——以 unknown 呈报
        而非假设满足（NO fabricated readiness）。"""
        context = _tenant(actor)
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_DRAFT,
            policy_action=Action.READ,
            resource_id=agent_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            row = await session.scalar(
                select(AgentDefinitionRow).where(
                    AgentDefinitionRow.id == agent_id,
                    AgentDefinitionRow.organization_id == context.organization_id,
                    AgentDefinitionRow.workspace_id == context.workspace_id,
                )
            )
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="agent not found"
                )
            # eval_seal 绑定链：eval_runs.run_id → runs.agent_version_id →
            # agent_versions.agent_definition_id；sealed_at 为空不算密封件
            sealed = await session.scalar(
                select(EvalRunRow.id)
                .join(
                    RunRow,
                    and_(
                        RunRow.organization_id == EvalRunRow.organization_id,
                        RunRow.workspace_id == EvalRunRow.workspace_id,
                        RunRow.id == EvalRunRow.run_id,
                    ),
                )
                .join(
                    AgentVersion,
                    and_(
                        AgentVersion.organization_id == RunRow.organization_id,
                        AgentVersion.workspace_id == RunRow.workspace_id,
                        AgentVersion.id == RunRow.agent_version_id,
                    ),
                )
                .where(
                    EvalRunRow.organization_id == context.organization_id,
                    EvalRunRow.workspace_id == context.workspace_id,
                    AgentVersion.agent_definition_id == agent_id,
                    EvalRunRow.sealed_at.is_not(None),
                )
                .limit(1)
            )
        missing: list[ReleaseReadinessCheck] = [
            ReleaseReadinessCheck(
                kind="unknown",
                detail=(
                    "connection readiness cannot be computed from the agent record: "
                    "workspace connections are capability-scoped in the capability hub "
                    "with no agent linkage in persistent storage"
                ),
            ),
            ReleaseReadinessCheck(
                kind="unknown",
                detail=(
                    "capability publish state cannot be computed from the agent record: "
                    "declared capabilities are opaque strings with no capability-version "
                    "mapping in persistent storage"
                ),
            ),
        ]
        if sealed is None:
            missing.insert(
                0,
                ReleaseReadinessCheck(
                    kind="eval_seal",
                    detail=(
                        "no sealed eval run is bound to this agent "
                        "(eval_runs.run_id -> runs.agent_version_id -> agent_versions)"
                    ),
                ),
            )
        return ReleaseReadiness(ready=not missing, missing=missing)

    @router.get("/{agent_id}/diff", response_model=AgentRevisionDiff)
    async def diff_agent_revisions(
        agent_id: UUID,
        request_scope: Request,
        actor: Annotated[ActorContext, Depends(actor_dependency)],
        from_revision: int = Query(..., ge=1),
        to_revision: int = Query(..., ge=1),
    ) -> AgentRevisionDiff:
        """版本 diff（只读支撑面，S10-T3）：字段级条目只来自真实存储（release
        manifest digest 集 / content_digest / schema_version）；未知 revision
        一律 404（fail closed，不猜测未记录的版本状态）。"""
        context = _tenant(actor)
        if from_revision > to_revision:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="from_revision must not exceed to_revision",
            )
        _, trace_id = request_trace(request_scope)
        await authorize_read(
            enforcer=policy_enforcer,
            actor=actor,
            organization_id=context.organization_id,
            workspace_id=context.workspace_id,
            policy_type=ResourceType.AGENT_DRAFT,
            policy_action=Action.READ,
            resource_id=agent_id,
            trace_id=trace_id,
        )
        async with tenant_session(sessions, context) as session:
            row = await session.scalar(
                select(AgentDefinitionRow).where(
                    AgentDefinitionRow.id == agent_id,
                    AgentDefinitionRow.organization_id == context.organization_id,
                    AgentDefinitionRow.workspace_id == context.workspace_id,
                )
            )
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="agent not found"
                )
            try:
                from_snapshot = await _resolve_revision_snapshot(
                    session, context, row, from_revision
                )
                to_snapshot = await _resolve_revision_snapshot(
                    session, context, row, to_revision
                )
            except LookupError as error:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
                ) from None
        return _diff_revisions(from_snapshot, to_snapshot)

    return router
