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
- 并发 revision fencing：revision/cache 的关联提交在 per-client 锁的临界区内
  串行化（远程求值仍在锁外并发——等待中的请求必须能完成，锁不能跨 HTTP）。
  revision 是 opaque 值，绝不按值比较新旧。fence generation 是单调序号，
  发送时捕获：无法证明时序的冲突响应会推进它并清空缓存；任何在旧 generation
  发出的响应此后都不能修改 revision/cache 或复用缓存（fail closed：allow
  一律 stale，deny/传输失败原样返回）——因此 revision 不可能因旧响应回退
  （ABA），旧 allow 也无法以 claim == current 绕过 stale 判断重新入缓存。
  采纳只发生在：声称的正是当前 revision；发送时刻的 revision 未被他人迁移
  （本请求是在途期间第一个迁移者，或顺序迁移，响应是新的首条证据）；或
  无法证明新旧的 deny 且缓存中存在将被该迁移清出的 allow（清出失效 allow
  是采纳迁移的唯一必要场景）。其余情况——无法证明新鲜的 allow、声称的正是
  已被取代 bundle 的 revision、或无需清出缓存的无法证明新旧的 deny——一律
  fail closed（opa_stale_response 或原样 deny），决策不进入缓存；
- 本层不实现授权语义（Rego 唯一事实）：只做传输与决策对象构造。
"""

from __future__ import annotations

import asyncio
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
    """OPA HTTP 决策客户端（httpx）。

    evaluate 可能被多个 asyncio task 并发调用（FastAPI/PEP 每请求一个 task，
    在 await 处交错）：revision 与 cache 的关联提交由 per-client asyncio.Lock
    串行化，防止任何完成顺序下旧决策覆盖新决策。
    """

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
        # fence generation：单调递增的并发防线。revision 迁移后仍可能有旧
        # generation 的在途响应到达——它们不能再修改 revision/cache。只有
        # 无法证明时序的冲突响应会推进它（并清空缓存）；成功响应采纳迁移时
        # revision 总是立即失效全部旧缓存，因此不需要退役 revision 集合。
        self._fence = 0
        # 提交临界区锁：revision/cache 的关联读写（含 LRU）都在这把锁内完成。
        # 锁不覆盖远程求值——冻结并发测试要求一个在途请求等待时其他请求能
        # 完整完成，锁跨 HTTP 会互相阻塞；串行化的是「重新检查缓存 → 提交」。
        self._lock = asyncio.Lock()

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

        # 并发 fencing。sent_at / sent_fence 是发送时刻的快照：读取与 POST 发起
        # 之间没有 await 点，asyncio 单线程下不会有其他 task 插队，因此快照即
        # 发送时的状态。revision 是 opaque 值，不能按字符串/字典序比较新旧——
        # 新旧只能由「迁移发生在我发送之后」来推断；fence generation 是单调
        # 序号，用来区分迁移前发送的旧响应。
        sent_at = self._revision
        sent_fence = self._fence
        decision = await self._evaluate_remote(normalized, now, input_digest)
        async with self._lock:
            # generation 检查先于锁内 cache recheck：旧 generation 的响应即使
            # 在 revision ABA 后与 current_revision 相同，也不得复用或写入缓存。
            # 它本身就是「无法证明时序的冲突响应」：清空缓存并推进 generation，
            # 使其余旧 generation 响应一并失效；allow 一律 stale，deny/传输
            # 失败可以原样 fail closed 返回（不允许假 allow，故返回 deny 安全）。
            if sent_fence < self._fence:
                self._cache.clear()
                self._fence += 1
                if decision.allow:
                    return _fail_closed(
                        "opa_stale_response",
                        evaluated_at=now,
                        input_digest=input_digest,
                    )
                return decision
            # 锁内重新检查缓存：在途期间其他请求可能已填充同 key 条目。只有本
            # 请求的响应与缓存条目属于同一 revision 时才可复用该条目——否则
            # 复用会让缓存里的旧 allow 顶替本请求携带的更新策略 deny（deny 被
            # 丢弃），或把 transport 失败变成缓存 allow 兜底（fail closed
            # 契约：需要求值的请求失败即拒绝）。失败决策 revision 为 None，
            # 天然不满足条件。用独立变量解包，不得遮蔽本请求的 decision。
            cached = self._cache.get(cache_key)
            if cached is not None:
                cached_decision, cached_revision, cached_at = cached
                if (cached_revision == self._revision
                        and cached_at + self._ttl > now
                        and decision.revision == cached_revision):
                    return cached_decision
            if decision.revision is not None:
                claim = decision.revision
                if claim != self._revision:
                    if self._revision != sent_at:
                        # 在途期间已有其他响应迁移了 revision，本响应无法证明
                        # 新旧：
                        # - 声称的正是发送时刻已知的 revision（被取代 bundle 的
                        #   陈旧响应）或 allow → stale，fail closed，不回退
                        #   current_revision、决策不进入缓存；
                        # - deny 可以原样返回，但不得迁移 revision 或写缓存——
                        #   迁移会让其后声称同 revision 的旧 allow 因
                        #   claim == current 重新获得缓存资格（revision ABA）。
                        #   仅当缓存中存在 allow 条目（该迁移是清出失效 allow
                        #   所必需）时才采纳迁移；否则推进 fence generation 并
                        #   清空缓存，使后续旧 generation 响应全部失效。
                        if claim == sent_at or decision.allow:
                            return _fail_closed(
                                "opa_stale_response",
                                evaluated_at=now,
                                input_digest=input_digest,
                            )
                        if not any(entry[0].allow for entry in self._cache.values()):
                            self._cache.clear()
                            self._fence += 1
                            return decision
                        self._cache.clear()
                        self._fence += 1
                        self._revision = claim
                    else:
                        # 发送时刻与当前一致：本请求是在途期间第一个移动
                        # revision 的人（或顺序迁移），响应是新的首条证据，
                        # 迁移 revision 并清空全部旧缓存。
                        self._cache.clear()
                        self._revision = claim
                self._cache[cache_key] = (decision, claim, decision.evaluated_at)
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
