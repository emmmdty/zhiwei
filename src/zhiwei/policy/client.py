"""S1-T3 OPA client：transport、decision schema 严格校验、revision/freshness 与有界缓存。

契约（冻结，见 tests/unit/policy/test_client.py）：
- 决策响应必须同时包含布尔 allow、非空 reason、非空 decision_id、唯一 bundle 的
  非空 revision（经 ?provenance=true）；缺失任一 → 畸形响应，fail closed 拒绝，
  且不伪造 decision_id/revision；
- 非 200、连接失败、超时 → fail closed（reason 区分 opa_unavailable /
  opa_http_error:<status>）；
- **有界缓存契约**（PERMISSIONS.md:85「不能使用缓存 allow 超过明确 TTL/版本」）：
  同 input 的决策只在「TTL 内 + 当前已知 revision」内复用（不联系 OPA）；任何
  需要求值的请求（TTL 过期 / revision 变化 / 其他 input）都必须真的求值，OPA
  不可用时直接拒绝，**绝不回落到任何缓存 allow**——缓存只缩短有界窗口内的
  重复请求，不提供故障兜底；
- 完整 schema 校验：evaluate 以 PolicyInput 校验整个 input（含嵌套字段）；
  任何非法/未声明字段（含 secret 形状）在发送前拒绝，且不进入 digest
  （PERMISSIONS.md:13 secret 不进入 decision log）；
- 并发 revision fencing：比当前 revision 更旧的响应若晚到（先于它采纳的新
  revision 已生效），按 stale 丢弃（fail closed，reason opa_stale_response），
  current_revision 不得回退、其决策不得进入缓存；
- 本层不实现授权语义（Rego 唯一事实）：只做传输与决策对象构造。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from pydantic import ValidationError

from zhiwei.contracts.canonical import digest
from zhiwei.contracts.time import utc_now
from zhiwei.policy.input import PolicyInput

DEFAULT_DECISION_PATH = "zhiwei/authz"
_MALFORMED_REASONS: tuple[str, ...] = (
    "missing_result",
    "missing_allow",
    "allow_not_boolean",
    "missing_reason",
    "empty_reason",
    "missing_decision_id",
    "empty_decision_id",
    "missing_provenance",
    "bundles_missing",
    "bundles_not_single",
    "revision_missing",
    "empty_revision",
    "invalid_json",
)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """一次授权决策及其 T4 audit 所需 metadata。

    fail-closed 决策（本地拒绝路径）decision_id/revision 为 None——绝不伪造
    OPA 的 decision_id；input_digest 为规范化 input 的 SHA-256。
    """

    allow: bool
    decision_id: str | None
    revision: str | None
    reason: str
    evaluated_at: datetime
    input_digest: str | None


def _fail_closed(reason: str, *, evaluated_at: datetime,
                 input_digest: str | None = None) -> PolicyDecision:
    return PolicyDecision(
        allow=False,
        decision_id=None,
        revision=None,
        reason=reason,
        evaluated_at=evaluated_at,
        input_digest=input_digest,
    )


class OPAClient:
    """OPA HTTP 决策客户端（httpx）。线程安全不需要：FastAPI/PEP 每请求一个 task。"""

    def __init__(
        self,
        base_url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
        cache_maxsize: int = 256,
        cache_ttl_seconds: float = 30.0,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._decision_url = f"{self._base_url}/v1/data/{DEFAULT_DECISION_PATH}?provenance=true"
        if http_client is not None:
            self._http = http_client
            self._owns_client = False
        else:
            # trust_env=False：OPA 是 loopback 授权边车，env 代理会把「容器已停止」
            # 变成代理的 502/503，破坏 opa_unavailable 的判定；策略请求绝不走代理
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout), trust_env=False)
            self._owns_client = True
        self._clock = clock
        self._cache: OrderedDict[str, tuple[PolicyDecision, str, datetime]] = OrderedDict()
        self._cache_maxsize = max(1, cache_maxsize)
        self._ttl = timedelta(seconds=max(0.0, cache_ttl_seconds))
        self._revision: str | None = None
        # 并发 fencing：_fence_token 是每请求单调序号（asyncio 单线程内自增+读取
        # 无 await，原子）；_last_applied_token 记录最后一次采纳响应的序号——晚到
        # 的旧响应（token 小于等于它）基于已撤权旧 bundle，必须丢弃
        self._fence_token = 0
        self._last_applied_token = 0

    @property
    def current_revision(self) -> str | None:
        return self._revision

    def fail_closed(self, reason: str) -> PolicyDecision:
        """PEP 本地拒绝路径（不经过 OPA）：带结构化 reason，不伪造 OPA metadata。"""
        return _fail_closed(reason, evaluated_at=self._clock())

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def evaluate(self, input_document: Mapping[str, object]) -> PolicyDecision:
        """对规范化 input 求值；任何失败都返回 fail-closed deny，不抛异常。

        input_document 为 PolicyInput 形状的 dict；先做完整 schema 校验（含
        嵌套字段），任何非法/未声明字段（含 secret 形状）在发送前拒绝且不
        进入 digest——decision log 永不回显未声明字段。
        """
        self._fence_token += 1
        token = self._fence_token
        now = self._clock()
        try:
            validated = PolicyInput.model_validate(input_document)
        except ValidationError:
            # 完整 schema 校验取代旧顶层键守卫：嵌套 extra（secret 形状）同样
            # 在发送前拒绝；被拒文档不得计算 digest（digest 只对规范化文档）
            return _fail_closed(
                "opa_input_invalid",
                evaluated_at=now,
                input_digest=None,
            )
        normalized = validated.model_dump(mode="json")
        # digest 与 transport body 都以规范化文档为准（等价 dict 键序不同时
        # digest 仍一致）；digest() 内部做 canonical_json
        input_digest = digest(normalized)
        cache_key = input_digest

        cached = self._cache.get(cache_key)
        if cached is not None:
            decision, revision, evaluated_at = cached
            # 有界缓存：条目只在「当前已知 revision + TTL 内」可复用。TTL 与
            # revision 是两条并行失效边界（PERMISSIONS.md:85 不能使用缓存 allow
            # 超过明确 TTL/版本）；revision 未知（从未成功求值过）或 OPA 变更过
            # revision 时不得使用。需要求值的请求必须真的求值，不做故障兜底。
            if revision == self._revision and evaluated_at + self._ttl > now:
                return decision

        decision = await self._evaluate_remote(normalized, now, input_digest)
        if decision.revision is not None:
            # 成功响应带 revision：与已知 revision 不一致时整体失效（撤权立即生效）
            if decision.revision != self._revision:
                if token <= self._last_applied_token:
                    # 晚到的旧在途响应：其 allow 基于已被新 revision 取代的旧
                    # bundle，revision 语义上已经过期——丢弃（fail closed），
                    # 不回退 current_revision、不写入缓存
                    return _fail_closed(
                        "opa_stale_response",
                        evaluated_at=now,
                        input_digest=input_digest,
                    )
                self._cache.clear()
                self._revision = decision.revision
            self._last_applied_token = max(self._last_applied_token, token)
            self._cache[cache_key] = (decision, decision.revision, decision.evaluated_at)
            self._cache.move_to_end(cache_key)
            while len(self._cache) > self._cache_maxsize:
                self._cache.popitem(last=False)
        return decision

    async def _evaluate_remote(
        self, input_document: Mapping[str, object], now: datetime, input_digest: str
    ) -> PolicyDecision:
        try:
            response = await self._http.post(self._decision_url, json={"input": input_document})
        except httpx.HTTPError:
            # 连接失败/超时/读错误统一 fail closed；OPA 挂了绝不用缓存兜底
            return _fail_closed("opa_unavailable", evaluated_at=now, input_digest=input_digest)

        if response.status_code != 200:
            return _fail_closed(
                f"opa_http_error:{response.status_code}",
                evaluated_at=now,
                input_digest=input_digest,
            )

        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            return _fail_closed(
                "opa_malformed_response:invalid_json",
                evaluated_at=now,
                input_digest=input_digest,
            )

        result = body.get("result")
        if not isinstance(result, dict):
            return self._malformed("missing_result", now, input_digest)
        allow = result.get("allow")
        if not isinstance(allow, bool):
            return self._malformed("allow_not_boolean", now, input_digest)
        reason = result.get("reason")
        if not isinstance(reason, str) or not reason:
            return self._malformed("missing_reason", now, input_digest)
        decision_id = body.get("decision_id")
        if not isinstance(decision_id, str) or not decision_id:
            return self._malformed("missing_decision_id", now, input_digest)
        revision, detail = self._extract_revision(body)
        if revision is None:
            return self._malformed(detail or "revision_missing", now, input_digest)

        return PolicyDecision(
            allow=allow is True,
            decision_id=decision_id,
            revision=revision,
            reason=reason,
            evaluated_at=now,
            input_digest=input_digest,
        )

    def _extract_revision(self, body: Mapping[str, object]) -> tuple[str | None, str | None]:
        """从 provenance 提取唯一 bundle revision。

        无法把决策绑定到某个 bundle revision 时视为畸形（未知 revision fail closed）：
        - provenance 缺失 / bundles 缺失 / bundle 数不为 1 / revision 缺失或为空。
        注意 CLI `-b` 加载的 bundle 在 provenance 里的 key 是挂载路径字符串，
        不是固定名——因此按「恰好一个 bundle」而不是按名字取。
        """
        provenance = body.get("provenance")
        if not isinstance(provenance, dict):
            return None, "missing_provenance"
        bundles = provenance.get("bundles")
        if not isinstance(bundles, dict) or not bundles:
            return None, "bundles_missing"
        if len(bundles) != 1:
            return None, "bundles_not_single"
        revision = next(iter(bundles.values()))
        if not isinstance(revision, dict):
            return None, "revision_missing"
        revision_value = revision.get("revision")
        if not isinstance(revision_value, str) or not revision_value:
            return None, "revision_missing"
        return revision_value, None

    def _malformed(self, detail: str, now: datetime, input_digest: str) -> PolicyDecision:
        if detail not in _MALFORMED_REASONS:  # 只允许冻结的畸形分类，防错字静默吞掉
            raise AssertionError(f"unlisted malformed reason: {detail}")
        return _fail_closed(
            f"opa_malformed_response:{detail}", evaluated_at=now, input_digest=input_digest
        )
