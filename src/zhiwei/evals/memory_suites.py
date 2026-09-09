"""S7 enterprise-memory-v1 suite 注册表：代码定义的内部行为契约 units。

事实源：specs/s7-memory.md §6（内部 suite 覆盖 write precision、retrieval、temporal
conflict、scope leakage、forget completeness、poisoning）、§4 + ADR-009（队列收敛）、
ADR-013 决策 2。

- units 由代码定义：本 suite 评测的是 memory 生产服务的行为契约，场景即断言，
  不依赖外部冻结语料（LongMemEval/LoCoMo 是外部诊断，不能替代企业 ACL/team/case
  lifecycle，见 spec §6）。
- 全部为 single 单位（unit_id == sample_id）：每个行为场景是一个独立统计单位。
- executor 绑定生产路径：WriteMemoryCandidateHandler → Memory policy →
  candidate/confirm/conflict/revoke/forget 生产服务；不旁路 repository 直调
  （沿 spec §7「Ask/Discover 不直接调用 repository」的同一纪律）。
- 判分语义在 executors/memory.py：行为事实 → 标签/断言，生产行为改变会判 0 分，
  而不是反查场景回填答案。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from zhiwei.evals.domain import RegisteredUnit

ENTERPRISE_MEMORY_V1 = "enterprise-memory-v1"

# suite 绑定的生产路径与 executor 种类（路径契约的事实源，executor 模块引用之）。
EXECUTOR_KIND = "memory-lifecycle"
PRODUCTION_MEMORY_PATH = (
    "WriteMemoryCandidateHandler->MemoryPolicy->"
    "CandidateQueue-ConfirmationWorkflow-ConflictManager-ForgetManager"
)

UNIT_CATEGORIES: tuple[str, ...] = (
    "write_matrix",
    "retrieval",
    "temporal_conflict",
    "scope_leakage",
    "forget_completeness",
    "poisoning",
    "queue_convergence",
)

# 稳定只读视图：suite 声明的全部 unit 类别（消费方按 frozenset 比较覆盖面）。
ENTERPRISE_MEMORY_UNIT_CATEGORIES = frozenset(UNIT_CATEGORIES)


@dataclass(frozen=True, slots=True)
class MemoryUnitDefinition:
    """一个 enterprise-memory 行为契约单位：类别 + 生产路径场景描述。"""

    sample_id: str
    unit_id: str
    category: str
    description: str


ENTERPRISE_MEMORY_UNITS: tuple[MemoryUnitDefinition, ...] = (
    MemoryUnitDefinition(
        sample_id="write-matrix/user-preference-auto-confirm",
        unit_id="write-matrix/user-preference-auto-confirm",
        category="write_matrix",
        description="user 低风险 preference 按 profile policy 自动确认",
    ),
    MemoryUnitDefinition(
        sample_id="write-matrix/team-decision-candidate",
        unit_id="write-matrix/team-decision-candidate",
        category="write_matrix",
        description="team decision 必须 Memory Steward 确认（candidate → steward confirm）",
    ),
    MemoryUnitDefinition(
        sample_id="write-matrix/secret-subject-forbidden",
        unit_id="write-matrix/secret-subject-forbidden",
        category="write_matrix",
        description="secret 类 subject 禁止写入（policy forbidden → refusal）",
    ),
    MemoryUnitDefinition(
        sample_id="retrieval/hard-filter-and-rank",
        unit_id="retrieval/hard-filter-and-rank",
        category="retrieval",
        description="硬过滤（org/workspace/scope subject）先行，exact 命中排名 lexical 之前，结果携带 reason/provenance/freshness",
    ),
    MemoryUnitDefinition(
        sample_id="temporal/conflict-coexists-no-silent-overwrite",
        unit_id="temporal/conflict-coexists-no-silent-overwrite",
        category="temporal_conflict",
        description="同 key 不同 value 产生未解决 conflict 并投影，原值不被静默覆盖",
    ),
    MemoryUnitDefinition(
        sample_id="temporal/supersede-correction-resolves-conflict",
        unit_id="temporal/supersede-correction-resolves-conflict",
        category="temporal_conflict",
        description="纠正创建 superseding version：旧记录 superseded、新记录 confirmed、冲突了结",
    ),
    MemoryUnitDefinition(
        sample_id="scope-leakage/cross-user-team-org-denied",
        unit_id="scope-leakage/cross-user-team-org-denied",
        category="scope_leakage",
        description="cross-user（scope subject）/cross-team（ACL）/cross-org 硬过滤拒绝",
    ),
    MemoryUnitDefinition(
        sample_id="forget/revoke-cascade-tombstone",
        unit_id="forget/revoke-cascade-tombstone",
        category="forget_completeness",
        description="revoke 级联：record revoked + tombstone、index/cache invalidation、检索消失、历史 tombstone 保留",
    ),
    MemoryUnitDefinition(
        sample_id="poisoning/tool-instruction-refused",
        unit_id="poisoning/tool-instruction-refused",
        category="poisoning",
        description="tool/retrieval instruction 写入拒绝（memory poisoning 防线）",
    ),
    MemoryUnitDefinition(
        sample_id="poisoning/secret-credential-refused",
        unit_id="poisoning/secret-credential-refused",
        category="poisoning",
        description="secret/credential 写入拒绝",
    ),
    MemoryUnitDefinition(
        sample_id="poisoning/pii-refused",
        unit_id="poisoning/pii-refused",
        category="poisoning",
        description="未经授权个人信息（证件/卡号类）写入拒绝",
    ),
    MemoryUnitDefinition(
        sample_id="queue-convergence/dedup-merge-ttl-load",
        unit_id="queue-convergence/dedup-merge-ttl-load",
        category="queue_convergence",
        description="ADR-009 负载：N 个同键重复 candidate 经生产 handler 注入，待确认数不随 Run 数线性增长；合并保留全部 source_refs；TTL 过期留 tombstone",
    ),
)


@dataclass(frozen=True, slots=True)
class MemorySuiteDefinition:
    """enterprise-memory-v1 的冻结视图：units 与生产路径绑定。"""

    name: str
    definitions: tuple[MemoryUnitDefinition, ...]
    registered_units: tuple[RegisteredUnit, ...]
    executor_kind: str
    production_path: str


@cache
def _load_suite(suite: str) -> MemorySuiteDefinition:
    if suite != ENTERPRISE_MEMORY_V1:
        raise LookupError(f"未知 memory suite: {suite}")
    return MemorySuiteDefinition(
        name=suite,
        definitions=ENTERPRISE_MEMORY_UNITS,
        registered_units=tuple(
            RegisteredUnit(sample_id=definition.sample_id, unit_id=definition.unit_id)
            for definition in ENTERPRISE_MEMORY_UNITS
        ),
        executor_kind=EXECUTOR_KIND,
        production_path=PRODUCTION_MEMORY_PATH,
    )


def resolve_memory_suite(suite: str) -> MemorySuiteDefinition:
    """按名解析 memory suite；未知名称 fail closed（LookupError）。"""
    return _load_suite(suite)


def registered_memory_units() -> tuple[RegisteredUnit, ...]:
    """suite 的 registered units（与 resolve_memory_suite 同源）。"""
    return _load_suite(ENTERPRISE_MEMORY_V1).registered_units
