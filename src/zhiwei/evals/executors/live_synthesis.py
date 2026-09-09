"""live-synthesis-v1 executor：生产检索 + 真实模型合成 + 确定性行为判分。

事实源：specs/s5 §9 claim boundary（合成质量需要 live 模型）、specs/s6 §6
（行为契约经生产 Runtime 路径）、AGENTS.md（live 只由 operator 显式触发）。

接线原则——不写评测专用旁路：检索是 knowledge suite 同款生产 Retrieve
handler；模型调用走生产 egress 机器（ModelEgressAssembler 的门禁链 +
CaptureTransport + AuditedEndpointResolver 首次留痕 + ManifestSink 落账），
测试经 inner=MockTransport 注入（client_factory 文档化路径）。可答单位的
run id 由组合根注入的 run_provisioner 供给（生产命令路径创建真实 Run 行）——
canonical 落账要求 run 在租户作用域存在。判分确定性：
证据在场 + 答案含 ground truth（逐字）+ 引用标记在场；拒绝合成质量评分。

operator 门禁：token 为空/空白 → 每单位 FAILED（operator_token_required），
零 egress 零落账——live 触发是 operator 显式动作，不是配置项。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2 as httpx

from zhiwei.evals.domain import RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.executors.knowledge import _CONNECTOR_SOURCES, PoolEntry, _entry_payload
from zhiwei.evals.live_synthesis_suites import (
    LiveSynthesisSuiteDefinition,
    deterministic_uuid,
)
from zhiwei.knowledge.contracts import SourceVersionState
from zhiwei.knowledge.freshness import FreshnessPolicy
from zhiwei.knowledge.planner import KnowledgePlanner
from zhiwei.knowledge.query import KnowledgeQuery, QuerySource, SortField
from zhiwei.models.contracts import ClassificationCeiling, ModelProfile, WireProtocol
from zhiwei.models.egress import ModelEgressAssembler
from zhiwei.models.first_use import AuditedEndpointResolver
from zhiwei.models.transports.base import NormalizedRequest
from zhiwei.models.transports.openai_chat import OpenAIChatTransport
from zhiwei.runtime.handlers.base import TaskInput
from zhiwei.runtime.handlers.retrieve import RetrieveHandler

_CITATION_PATTERN = re.compile(r"\[\d+\]")

# live 合成的超时预算：reasoning 模型端到端延迟（reasoning + 生成）常态超过
# httpx 库默认 5s read timeout（bad case 7cd50885：2 单位 ReadTimeout）；
# 连接阶段单独收紧。连接重试仍为 0（重试由 Runtime 上移，ADR-001）。
_MODEL_CALL_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class ProviderEgressError(RuntimeError):
    """provider 侧错误（HTTP 4xx/5xx、超时等 transport 异常的包装）。"""


class LiveSynthesisExecutor:
    """一个注册单位 = 生产检索 → （可答单位）真实模型合成 → 确定性判分。"""

    def __init__(
        self,
        suite: LiveSynthesisSuiteDefinition,
        *,
        operator_token: str,
        endpoints_path: Path,
        env_overrides: Mapping[str, str] | None = None,
        first_use_sink: Any,
        manifest_sink: Any,
        run_provisioner: Any,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._suite = suite
        self._operator_token = operator_token
        self._endpoints_path = endpoints_path
        self._env_overrides = dict(env_overrides or {})
        self._first_use_sink = first_use_sink
        self._manifest_sink = manifest_sink
        self._run_provisioner = run_provisioner
        self._inner = inner
        self._assembler = ModelEgressAssembler(manifest_sink=manifest_sink)
        self._transport = OpenAIChatTransport()
        planner = KnowledgePlanner(
            freshness_policies={
                "files": FreshnessPolicy(connector="files", aging_threshold=timedelta(days=7))
            },
        )
        self._handler = RetrieveHandler(planner)

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome:
        if not self._operator_token or not self._operator_token.strip():
            return self._outcome(
                unit, SampleStatus.FAILED, error="operator_token_required"
            )
        spec = self._suite.answerable_specs.get(unit.sample_id)
        try:
            if spec is not None:
                return await self._execute_answerable(unit, spec)
            if unit.sample_id == self._suite.abstain_sample_id:
                return self._execute_short_circuit(
                    unit, behavior="abstain_no_evidence", pool_docs=()
                )
            if unit.sample_id == self._suite.acl_sample_id:
                doc = self._suite.evidence_docs[0]
                return self._execute_short_circuit(
                    unit, behavior="acl_refusal", pool_docs=(doc,), deny_principal=True
                )
            return self._outcome(
                unit, SampleStatus.FAILED, error=f"unit 未注册于 suite: {unit.sample_id}"
            )
        except ProviderEgressError as exc:
            return self._outcome(
                unit,
                SampleStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
                error_kind="provider_error",
            )
        except Exception as exc:
            return self._outcome(
                unit,
                SampleStatus.ERROR,
                error=f"{type(exc).__name__}: {exc}",
                error_kind="executor_error",
            )

    # ------------------------------------------------------------- 可答单位

    async def _execute_answerable(self, unit: RegisteredUnit, spec: Any) -> SampleOutcome:
        entry = self._pool_entry(spec.doc)
        surfaced = self._retrieve(
            query_text=spec.question,
            sample_id=spec.sample_id,
            entries=[entry],
            deny_principal=False,
        )
        if not surfaced:
            return self._outcome(
                unit,
                SampleStatus.FAILED,
                query_id=spec.sample_id,
                model=self._env_overrides.get("OPENAI_MODEL", ""),
                failures=["evidence_missing"],
            )
        # run 供给先于模型调用：canonical 落账（first use / wire manifest）经
        # CanonicalUnitOfWork 要求 run_id 在租户作用域有真实 Run 行（fail
        # closed, RunNotFound）。供给走生产命令路径（RunCommandService），由
        # 组合根注入——executor 自身不触碰 DB。
        run_id = await self._run_provisioner(spec.sample_id)
        answer_text = await self._call_model(
            run_id=run_id,
            question=spec.question,
            evidence=[(doc_uri, spec.doc.content) for doc_uri in [spec.doc.uri]],
        )
        failures: list[str] = []
        if spec.ground_truth not in answer_text:
            failures.append("ground_truth_missing")
        citations = len(set(_CITATION_PATTERN.findall(answer_text)))
        if citations == 0:
            failures.append("citation_missing")
        if failures:
            return self._outcome(
                unit,
                SampleStatus.FAILED,
                query_id=spec.sample_id,
                model=self._env_overrides.get("OPENAI_MODEL", ""),
                answer_text=answer_text,
                citations=citations,
                failures=failures,
            )
        return self._outcome(
            unit,
            SampleStatus.COMPLETED,
            query_id=spec.sample_id,
            model=self._env_overrides.get("OPENAI_MODEL", ""),
            answer_text=answer_text,
            citations=citations,
            failures=[],
        )

    # ------------------------------------------------------------- 短路单位

    def _execute_short_circuit(
        self,
        unit: RegisteredUnit,
        *,
        behavior: str,
        pool_docs: tuple[Any, ...],
        deny_principal: bool = False,
    ) -> SampleOutcome:
        entries = [self._pool_entry(doc) for doc in pool_docs]
        surfaced = self._retrieve(
            query_text=behavior,
            sample_id=unit.sample_id,
            entries=entries,
            deny_principal=deny_principal,
        )
        # abstain：池为空 → 无证据 abstain；ACL：池有文档但 principal 被拒。
        # 两者通过条件一致——候选必须为空（观察到候选 = 执行序漂移 / ACL 泄漏）
        ok = len(surfaced) == 0
        return self._outcome(
            unit,
            SampleStatus.COMPLETED if ok else SampleStatus.FAILED,
            behavior=behavior,
            surfaced_candidates=len(surfaced),
            failures=[] if ok else ["short_circuit_violated"],
        )

    # ------------------------------------------------------------- 生产检索

    def _pool_entry(self, doc: Any) -> PoolEntry:
        from datetime import UTC, datetime

        from zhiwei.knowledge.contracts import (
            ACLSnapshot,
            Classification,
            Locator,
            SourceVersion,
        )

        observed_at = datetime(2026, 9, 4, tzinfo=UTC)
        version = SourceVersion(
            id=deterministic_uuid("version", doc.doc_id),
            source_object_id=deterministic_uuid("object", doc.doc_id),
            version_seq=1,
            locator=Locator(connector=doc.connector, uri=doc.uri),
            content_digest=doc.content_digest,
            observed_at=observed_at,
            valid_at=observed_at,
            acl=ACLSnapshot(
                allowed_principals=(),
                denied_principals=(),
                allowed_groups=("workspace-members",),
            ),
            classification=Classification.PUBLIC,
            state=SourceVersionState.ACTIVE,
        )
        return PoolEntry(version=version, acl_state="granted", classification_declared="public")

    def _retrieve(
        self,
        *,
        query_text: str,
        sample_id: str,
        entries: list[PoolEntry],
        deny_principal: bool,
    ) -> list[dict[str, Any]]:
        organization_id = deterministic_uuid("org", sample_id)
        workspace_id = deterministic_uuid("workspace", sample_id)
        principal_id = deterministic_uuid("principal", sample_id)
        sources: list[QuerySource] = []
        for entry in entries:
            source = _CONNECTOR_SOURCES.get(entry.version.locator.connector)
            if source is not None and source not in sources:
                sources.append(source)
        if not sources:
            sources = [QuerySource.DOC]
        acl_payload: dict[str, Any] = {
            "principal_id": str(principal_id),
            "organization_id": str(organization_id),
            "workspace_id": str(workspace_id),
            "allowed_principals": [],
            "allowed_groups": ["workspace-members"],
            "denied_principals": sorted([str(principal_id)] if deny_principal else []),
            "classification_ceiling": ClassificationCeiling.PUBLIC.value,
        }
        query = KnowledgeQuery(
            query_id=sample_id,
            organization_id=organization_id,
            workspace_id=workspace_id,
            principal_id=principal_id,
            text=query_text,
            sources=tuple(sources),
            classification_ceiling=ClassificationCeiling.PUBLIC.value,
            top_k=max(len(entries), 1),
            sort_by=SortField.SCORE,
        )
        task_input = TaskInput(
            task_id=f"eval:{sample_id}",
            attempt_id=deterministic_uuid("attempt", sample_id),
            input_values={
                "query": query.model_dump(mode="json"),
                "acl": acl_payload,
                "candidates": [_entry_payload(entry) for entry in entries],
            },
        )
        output = self._handler.execute(task_input)
        values = output.output_values
        if values.get("status") != "completed":
            return []
        return list(values.get("candidates", []))

    # ------------------------------------------------------------- 模型调用

    async def _call_model(
        self, *, run_id: UUID, question: str, evidence: list[tuple[str, str]]
    ) -> str:
        model_name = self._env_overrides.get("OPENAI_MODEL", "")
        if not model_name:
            raise RuntimeError("OPENAI_MODEL is required for live synthesis")
        resolver = AuditedEndpointResolver(
            self._first_use_sink,
            endpoints_path=self._endpoints_path,
            env_overrides=self._env_overrides,
        )
        endpoint = await resolver.resolve_default(run_id=run_id)
        profile = ModelProfile(
            id="live-synthesis",
            endpoint_id=endpoint.id,
            model_name=model_name,
            wire_protocol=WireProtocol.OPENAI_CHAT,
            api_path="/chat/completions",
            context_window=131_072,
            max_output=4_096,
        )
        prepared = self._assembler.prepare(
            endpoint=endpoint,
            profile=profile,
            context_classification=ClassificationCeiling.PUBLIC.value,
            inner=self._inner,
            client_timeout=_MODEL_CALL_TIMEOUT,
        )
        request = NormalizedRequest(
            model=model_name,
            messages=self._build_messages(question, evidence),
            temperature=0.0,
            max_tokens=1_024,
        )
        api_key = self._env_overrides.get(endpoint.credential_env, "")
        if not api_key:
            raise RuntimeError(
                f"credential env {endpoint.credential_env} is not set; refusing "
                "unauthenticated live egress (fail closed)"
            )

        async def send(client: httpx.AsyncClient) -> None:
            # 凭据注入：transport.send 不携带 headers 形参，经 client.headers
            # 注入（CaptureTransport 照常捕获；manifest 侧 authorization 已
            # redact——presend._SECRET_HEADERS）。凭据不进 NormalizedRequest。
            client.headers["Authorization"] = f"Bearer {api_key}"
            try:
                response = await self._transport.send(client, endpoint.base_url, request)
            except Exception as exc:
                # assembler.execute 会把 send 异常吞成 EgressResult.error 字符串——
                # 这里留对象引用，保留 provider_error 的分类信息
                self._send_error = exc
                raise ProviderEgressError(str(exc)) from exc
            self._last_answer = response.content

        self._last_answer = ""
        self._send_error: Exception | None = None
        egress_result = await self._assembler.execute(prepared, send, run_id=run_id)
        # assembler 吞 send 异常进 EgressResult（门禁拒绝/网络错误同形）——
        # 未发出即 raise，让单位落 ERROR/FAILED 终态而非静默空答案
        if self._send_error is not None:
            raise ProviderEgressError(
                f"{type(self._send_error).__name__}: {self._send_error}"
            ) from self._send_error
        if not egress_result.sent:
            raise RuntimeError(
                f"model egress failed: {egress_result.error or 'no captures recorded'}"
            )
        if not self._last_answer:
            raise RuntimeError("model egress produced no answer (see wire manifest)")
        return self._last_answer

    def _build_messages(
        self, question: str, evidence: list[tuple[str, str]]
    ) -> list[dict[str, Any]]:
        blocks = "\n\n".join(
            f"[{index}] 证据来源 {uri}：\n{content}"
            for index, (uri, content) in enumerate(evidence, start=1)
        )
        # 「逐字引用」是判分契约（答案含 ground truth 逐字）的 prompt 侧支撑：
        # bad case b4cb503d 显示模型默认会改写/加粗/拆分重组证据表述，事实正确
        # 但破坏逐字匹配——要求第一句逐字引用原文并禁用 Markdown 格式化。
        system = (
            "你是企业知识助手。只依据给出的证据回答。回答的第一句必须逐字引用"
            "证据原文中直接回答问题的片段——不得改写、不得增删字词或调整语序、"
            "不得使用 Markdown 加粗或列表——并在句末标注引用编号（如 [1]）。"
            "证据中没有的事实用「证据中未提及」明确说明，不得编造；第一句之外"
            "不要重复或展开其他内容。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{blocks}\n\n问题：{question}"},
        ]

    # ------------------------------------------------------------- 落账形态

    def _outcome(
        self,
        unit: RegisteredUnit,
        status: SampleStatus,
        *,
        behavior: str | None = None,
        query_id: str | None = None,
        model: str | None = None,
        answer_text: str | None = None,
        citations: int | None = None,
        surfaced_candidates: int | None = None,
        failures: list[str] | None = None,
        error: str | None = None,
        error_kind: str | None = None,
    ) -> SampleOutcome:
        result: dict[str, Any] = {
            "suite": self._suite.name,
            "executor": self._suite.executor_kind,
            "mode": "live",
            "verdict": "pass" if status is SampleStatus.COMPLETED else "fail",
        }
        if behavior is not None:
            result["behavior"] = behavior
        if query_id is not None:
            result["query_id"] = query_id
        if model is not None:
            result["model"] = model
        if answer_text is not None:
            result["answer_text"] = answer_text
        if citations is not None:
            result["citations"] = citations
        if surfaced_candidates is not None:
            result["surfaced_candidates"] = surfaced_candidates
        if failures is not None:
            result["failures"] = failures
        if error is not None:
            result["error"] = error
        if error_kind is not None:
            result["error_kind"] = error_kind
        return SampleOutcome(unit=unit, status=status, result=result)
