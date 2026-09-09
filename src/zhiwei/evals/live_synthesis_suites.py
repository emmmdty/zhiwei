"""live-synthesis-v1：live 模型合成 suite（S11 live 演示任务，operator 2026-09-09 裁决）。

specs/s5 §9 claim boundary：offline knowledge suite 只声明检索/ACL/freshness，
「答案合成质量需要 live 模型」。本 suite 是最小的诚实 live 面：

- 检索走生产 Retrieve handler（Knowledge Planner，knowledge suite 同款）；
- 合成经生产 egress 机器（ModelEgressAssembler 门禁链 + OpenAIChatTransport +
  AuditedEndpointResolver 首次留痕）调用真实模型；operator token 门禁；
- 判分全部确定性（行为级）：证据在场 + 答案含 ground truth + 引用标记在场；
  不声称开放域合成质量（那需要 judge）；
- 短路单位（abstain / ACL 拒绝）不发生模型调用——验证生产 fail-closed 行为。

语料形态说明：locator 级冻结语料（evals/knowledge/*.jsonl）不含文档正文，
live 合成需要可引文本——证据文档因此代码定义（discover-blind-v1「代码定义
blind 快照」同款先例），corpus digest 是其内容寻址；不改冻结资产。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from zhiwei.contracts.canonical import canonical_json, digest_bytes
from zhiwei.evals.domain import RegisteredUnit

LIVE_SYNTHESIS_V1 = "live-synthesis-v1"
EXECUTOR_KIND = "live-synthesis"
PRODUCTION_PATH = "RetrieveTaskHandler->KnowledgePlanner->ModelEgressAssembler->OpenAIChatTransport"

_ABSTAIN_SAMPLE_ID = "live-abstain-no-evidence"
_ACL_SAMPLE_ID = "live-acl-refusal"


@dataclass(frozen=True)
class EvidenceDocument:
    """代码定义的证据文档：正文含 ground truth（可引文本的最小载体）。"""

    doc_id: str
    connector: str
    uri: str
    content: str

    @property
    def content_digest(self) -> str:
        return digest_bytes(canonical_json({"doc_id": self.doc_id, "content": self.content}))


@dataclass(frozen=True)
class AnswerableSpec:
    """一个可答单位：问题 + 证据文档 + 判分锚点（ground truth 逐字在场）。"""

    sample_id: str
    question: str
    ground_truth: str
    doc: EvidenceDocument


_EVIDENCE_DOCS: tuple[EvidenceDocument, ...] = (
    EvidenceDocument(
        doc_id="LS-DOC-001",
        connector="files",
        uri="docs/live/vector-db.md",
        content=(
            "# 向量数据库运行手册\n\n"
            "检索服务默认使用 HNSW 索引，构建参数 ef_construction=200，查询参数 "
            "ef_search=96。索引每日 03:00 UTC 增量重建，重建期间查询走只读副本。\n"
        ),
    ),
    EvidenceDocument(
        doc_id="LS-DOC-002",
        connector="files",
        uri="docs/live/rate-limits.md",
        content=(
            "# API 速率限制\n\n"
            "工作区默认配额为 1000 RPM（每分钟请求数），突发上限 1200 RPM。"
            "超限请求返回 429 并携带 Retry-After 头。\n"
        ),
    ),
    EvidenceDocument(
        doc_id="LS-DOC-003",
        connector="files",
        uri="docs/live/backup-policy.md",
        content=(
            "# 备份策略\n\n"
            "业务库每日全量备份，保留 30 天；WAL 归档持续进行，恢复点目标 "
            "RPO 5 分钟，恢复时间目标 RTO 30 分钟。\n"
        ),
    ),
    EvidenceDocument(
        doc_id="LS-DOC-004",
        connector="files",
        uri="docs/live/key-rotation.md",
        content=(
            "# 密钥轮换\n\n"
            "数据主密钥每 90 天轮换一次；轮换采用信封加密，旧密钥保留 24 小时 "
            "用于在途请求解密，之后销毁。\n"
        ),
    ),
)

_ANSWERABLE_SPECS: tuple[AnswerableSpec, ...] = (
    AnswerableSpec(
        sample_id="live-ans-001",
        question="向量数据库的默认索引类型与查询参数 ef_search 是多少？",
        ground_truth="HNSW 索引，构建参数 ef_construction=200，查询参数 ef_search=96",
        doc=_EVIDENCE_DOCS[0],
    ),
    AnswerableSpec(
        sample_id="live-ans-002",
        question="工作区 API 默认速率配额是多少？",
        ground_truth="1000 RPM",
        doc=_EVIDENCE_DOCS[1],
    ),
    AnswerableSpec(
        sample_id="live-ans-003",
        question="备份的恢复点目标（RPO）是多少？",
        ground_truth="RPO 5 分钟",
        doc=_EVIDENCE_DOCS[2],
    ),
    AnswerableSpec(
        sample_id="live-ans-004",
        question="数据主密钥多久轮换一次？",
        ground_truth="每 90 天轮换一次",
        doc=_EVIDENCE_DOCS[3],
    ),
)


@dataclass(frozen=True)
class LiveSynthesisSuiteDefinition:
    """suite 注册表投影：units + 判分 spec + 密封 provenance。"""

    name: str
    registered_units: tuple[RegisteredUnit, ...]
    answerable_specs: dict[str, AnswerableSpec]
    abstain_sample_id: str
    acl_sample_id: str
    evidence_docs: tuple[EvidenceDocument, ...]
    corpus_digest: str
    corpus_path: str
    production_path: str
    executor_kind: str


def _registered_units() -> tuple[RegisteredUnit, ...]:
    units = [RegisteredUnit(sample_id=spec.sample_id, unit_id=spec.sample_id) for spec in _ANSWERABLE_SPECS]
    units.append(RegisteredUnit(sample_id=_ABSTAIN_SAMPLE_ID, unit_id=_ABSTAIN_SAMPLE_ID))
    units.append(RegisteredUnit(sample_id=_ACL_SAMPLE_ID, unit_id=_ACL_SAMPLE_ID))
    return tuple(units)


def _corpus_digest() -> str:
    payload = canonical_json(
        {
            "docs": [
                {"doc_id": doc.doc_id, "connector": doc.connector, "uri": doc.uri, "content": doc.content}
                for doc in _EVIDENCE_DOCS
            ],
            "questions": [
                {"sample_id": spec.sample_id, "question": spec.question, "ground_truth": spec.ground_truth}
                for spec in _ANSWERABLE_SPECS
            ],
            "short_circuit_units": [_ABSTAIN_SAMPLE_ID, _ACL_SAMPLE_ID],
        }
    )
    return digest_bytes(payload)


def resolve_live_synthesis_suite() -> LiveSynthesisSuiteDefinition:
    """suite 解析（幂等：units/corpus digest 逐字节确定）。"""
    return LiveSynthesisSuiteDefinition(
        name=LIVE_SYNTHESIS_V1,
        registered_units=_registered_units(),
        answerable_specs={spec.sample_id: spec for spec in _ANSWERABLE_SPECS},
        abstain_sample_id=_ABSTAIN_SAMPLE_ID,
        acl_sample_id=_ACL_SAMPLE_ID,
        evidence_docs=_EVIDENCE_DOCS,
        corpus_digest=_corpus_digest(),
        corpus_path="live-synthesis-v1 (code-defined evidence documents)",
        production_path=PRODUCTION_PATH,
        executor_kind=EXECUTOR_KIND,
    )


def deterministic_uuid(*parts: str):
    """UUID5 派生（knowledge executor 同款确定性纪律）。"""
    return uuid5(NAMESPACE_URL, "zhiwei:evals:live-synthesis:" + ":".join(parts))
