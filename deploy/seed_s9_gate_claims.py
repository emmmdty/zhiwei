#!/usr/bin/env python3
"""S9 Gate claim registry seeding（S9-T8）。

把 S9 Gate 步骤 3 经生产 CLI 密封的 suite artifact 绑定进 Claim Registry：

- 证据不经转述：ClaimRegistryService.upgrade 从 object store 独立复算密封件，
  域层状态机 planned → implemented（手工）→ offline_verified（仅接受 offline
  模式密封件——fixture 密封件在状态机处被拒，因此 claim 支撑 suite 以
  --mode offline 密封）；
- bound_value 聚合自密封 run 的 sample 终态（EvalSample.result 的逐 outcome
  落账，result digest 已封进密封件）；模板填充走 render_claim 的 SealedValue
  provenance 路径，digest 锚点为服务层复算 digest。bound_value 落库走 0015
  迁移显式授权的列级 UPDATE（status/evidence/bound_value/updated_at）——服务
  层暂无绑定值入口，种子层按迁移授权面写入；
- 外部基准（longmemeval）只注册 planned：unavailable 密封件是不可用性的绑定
  证据，不解锁质量 claim（specs/s9 §7：缺数据时 claim 保持 planned）。

输入：--runs-json，步骤 3 各 CLI 输出的 verbatim JSON 数组。仅取 eval_run_id/
organization_id/workspace_id 做租户定位；密封 digest、mode、migration revision
一律由服务层/密封件复算，不从输入文件采信。

环境变量：ZHIWEI_DATABASE_URL（app DSN，RLS 租户路径）、ZHIWEI_OBJECT_STORE_ROOT
（与密封时同一 object store，复核要从原 store 取件）。不读 .env。
幂等：已 offline_verified 且 evidence 指向同一 eval_run 的 claim 跳过升级；
planned 外部 claim 已存在即跳过。claim_id 租户内唯一，重复执行不产生重复行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from zhiwei.agents.claims import (
    ClaimNotFound,
    ClaimRegistryService,
    ClaimScope,
    ClaimStatus,
    ClaimUpgradeDenied,
    SealedValue,
    render_claim,
)
from zhiwei.config.settings import load_settings
from zhiwei.contracts.time import utc_now
from zhiwei.evals.runs import EvalFoundationService
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import ClaimRegistryRow, EvalRun, EvalSample, Workspace
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

# 计划中的外部基准 claim 落在专用 gate 租户（无内部密封证据可绑）。
GATE_ORG_ID = UUID("5ef1c2a8-9b30-4d57-a1c4-6e2f8b90d001")
GATE_WS_ID = UUID("a7d4e2f1-3c60-4b8a-9e25-1f0c7d3b6002")
GATE_WS_NAME = "s9-gate-claims"

# scope 口径标签：model/environment 是离线 fixture 参照绑定的固定口径；
# mode/version/corpus/date 逐 claim 从密封件与 EvalRun 行复制，不在此处发明。
SCOPE_MODEL = "reference-fixture"
SCOPE_ENVIRONMENT = "offline-fixture"


@dataclass(frozen=True)
class ClaimSpec:
    """一个可公开注册的 claim：suite 定位密封 run，metric 选择聚合口径。"""

    suite: str
    claim_id: str
    statement: str
    metric: str


CLAIM_SPECS: tuple[ClaimSpec, ...] = (
    ClaimSpec(
        suite="factqa-v1",
        claim_id="factqa-v1.accuracy",
        statement="factqa-v1 语料内 accuracy {{value}}（{{detail}}，evidence replay 路径）",
        metric="mean_score",
    ),
    ClaimSpec(
        suite="knowledge-doc-v1",
        claim_id="knowledge-doc-v1.retrieval",
        statement="knowledge-doc-v1 retrieval 判分通过率 {{value}}（{{detail}}）",
        metric="mean_score",
    ),
    ClaimSpec(
        suite="knowledge-code-github-v1",
        claim_id="knowledge-code-github-v1.retrieval",
        statement="knowledge-code-github-v1 retrieval 判分通过率 {{value}}（{{detail}}）",
        metric="mean_score",
    ),
    ClaimSpec(
        suite="knowledge-cross-source-v1",
        claim_id="knowledge-cross-source-v1.retrieval",
        statement="knowledge-cross-source-v1 跨源检索判分通过率 {{value}}（{{detail}}）",
        metric="mean_score",
    ),
    ClaimSpec(
        suite="knowledge-acl-freshness-v1",
        claim_id="knowledge-acl-freshness-v1.retrieval",
        statement="knowledge-acl-freshness-v1 ACL/新鲜度判分通过率 {{value}}（{{detail}}）",
        metric="mean_score",
    ),
    ClaimSpec(
        suite="enterprise-memory-v1",
        claim_id="enterprise-memory-v1.pass",
        statement="enterprise-memory-v1 lifecycle 判分通过率 {{value}}（{{detail}}）",
        metric="mean_score",
    ),
    ClaimSpec(
        suite="numeric-risk-v1",
        claim_id="numeric-risk-v1.recall-d0",
        statement="numeric-risk-v1 planted-target recall (D0) {{value}}（{{detail}}）",
        metric="planted_recall",
    ),
    ClaimSpec(
        suite="discover-blind-v1",
        claim_id="discover-blind-v1.blind-pass",
        statement="discover-blind-v1 blind 快照判分通过率 {{value}}（{{detail}}）",
        metric="correct_rate",
    ),
    ClaimSpec(
        suite="runtime-contract-v1",
        claim_id="runtime-contract-v1.contract-pass",
        statement="runtime-contract-v1 生产 Runtime 契约单位终态 {{value}}（{{detail}}）",
        metric="terminal",
    ),
    ClaimSpec(
        suite="ask-v1",
        claim_id="ask-v1.contract-pass",
        statement="ask-v1 行为契约单位终态 {{value}}（{{detail}}）",
        metric="terminal",
    ),
)

EXTERNAL_PLANNED_CLAIMS: tuple[tuple[str, str], ...] = (
    (
        "longmemeval.external-diagnostic",
        "LongMemEval 外部质量诊断（数据/许可未就绪，claim 保持 planned）",
    ),
)


def _metric(spec: ClaimSpec, rows: list[EvalSample], registered: int) -> tuple[str, str]:
    """从密封 run 的 sample 终态聚合绑定值；算不出就 fail closed，不造默认值。"""
    total = len(rows)
    if total != registered:
        raise RuntimeError(f"{spec.suite}: samples {total} != registered {registered}")
    completed = sum(1 for row in rows if row.status == "completed")
    if spec.metric == "mean_score":
        scores = [
            float(score)
            for row in rows
            for score in [(row.result or {}).get("score")]
            if isinstance(score, (int, float))
        ]
        if len(scores) != total:
            raise RuntimeError(f"{spec.suite}: score 缺失（{len(scores)}/{total}），拒绝聚合")
        return f"{sum(scores) / len(scores):.3f}", f"{total}/{total} samples"
    if spec.metric == "planted_recall":
        planted = [row for row in rows if row.sample_id.startswith("planted:")]
        if not planted:
            raise RuntimeError(f"{spec.suite}: 无 planted 单位，D0 recall 不可定义")
        matched = sum(1 for row in planted if (row.result or {}).get("matched") is True)
        return f"{matched / len(planted):.3f}", f"{matched}/{len(planted)} planted targets"
    if spec.metric == "correct_rate":
        correct = sum(1 for row in rows if (row.result or {}).get("correct") is True)
        return f"{correct / total:.3f}", f"{correct}/{total} units"
    if spec.metric == "terminal":
        if completed != total:
            raise RuntimeError(f"{spec.suite}: 存在非终态单位（{completed}/{total}）")
        return f"{completed}/{total}", "units terminal on production path"
    raise RuntimeError(f"未知 metric: {spec.metric}")


async def _ensure_gate_tenant(sessions: Any) -> TenantContext:
    """专用 gate 租户（planned 外部 claim 的宿主）：缺则建，存在即复用。"""
    context = TenantContext(organization_id=GATE_ORG_ID, workspace_id=GATE_WS_ID)
    async with tenant_session(sessions, context) as session:
        existing_ws = await session.scalar(select(Workspace).where(Workspace.id == GATE_WS_ID))
        if existing_ws is not None:
            return context
        repository = TenantRepository(session, context)
        await repository.create_organization(GATE_ORG_ID, status="active")
        await repository.create_workspace(GATE_WS_ID, name=GATE_WS_NAME)
    return context


async def seed(runs_json: Path) -> int:
    settings = load_settings()
    if settings.database_url is None or settings.object_store_root is None:
        print("[seed] ✗ 需要 ZHIWEI_DATABASE_URL 与 ZHIWEI_OBJECT_STORE_ROOT", file=sys.stderr)
        return 1
    entries = json.loads(runs_json.read_text(encoding="utf-8"))
    by_suite = {entry["suite"]: entry for entry in entries if "suite" in entry}
    store = PosixObjectStore(settings.object_store_root)
    engine = create_database_engine(settings.database_url.get_secret_value())
    sessions = create_session_factory(engine)

    results: list[dict[str, Any]] = []
    for spec in CLAIM_SPECS:
        entry = by_suite.get(spec.suite)
        if entry is None:
            print(f"[seed] ✗ runs-json 缺少 suite {spec.suite}", file=sys.stderr)
            return 1
        context = TenantContext(
            organization_id=UUID(entry["organization_id"]),
            workspace_id=UUID(entry["workspace_id"]),
        )
        eval_run_id = UUID(entry["eval_run_id"])
        async with tenant_session(sessions, context) as session:
            # 复核密封件（从 object store 复算）+ 读取口径标签的权威来源。
            artifact = await EvalFoundationService(session, context, store).verify_sealed(
                eval_run_id
            )
            eval_run = await session.get(EvalRun, eval_run_id)
            if eval_run is None or eval_run.status != "sealed" or eval_run.sealed_at is None:
                raise RuntimeError(f"{spec.suite}: sealed EvalRun 缺失或未密封")
            rows = list(
                await session.scalars(
                    select(EvalSample).where(EvalSample.eval_run_id == eval_run_id)
                )
            )
            value, detail = _metric(spec, rows, len(artifact.registered_units))
            scope_date = eval_run.sealed_at.date().isoformat()

            registry = ClaimRegistryService(session, context, store)
            try:
                record = await registry.get(spec.claim_id)
            except ClaimNotFound:
                record = await registry.register(
                    claim_id=spec.claim_id,
                    statement=spec.statement,
                    scope=ClaimScope(
                        mode=artifact.mode,
                        model=SCOPE_MODEL,
                        version=artifact.migration_revision,
                        date=scope_date,
                        corpus=spec.suite,
                        environment=SCOPE_ENVIRONMENT,
                    ),
                )
            if record.status is ClaimStatus.PLANNED:
                record = await registry.upgrade(
                    spec.claim_id, target=ClaimStatus.IMPLEMENTED, eval_run_id=None
                )
            if record.status is ClaimStatus.IMPLEMENTED:
                record = await registry.upgrade(
                    spec.claim_id, target=ClaimStatus.OFFLINE_VERIFIED, eval_run_id=eval_run_id
                )
            if record.status is not ClaimStatus.OFFLINE_VERIFIED or record.evidence is None:
                raise ClaimUpgradeDenied(f"{spec.claim_id}: 未达 offline_verified")
            if record.evidence.eval_run_id != eval_run_id:
                raise ClaimUpgradeDenied(f"{spec.claim_id}: evidence 绑定到其它 run")
            digest = record.evidence.seal_digest
            rendered = render_claim(
                record,
                {
                    "value": SealedValue(
                        value=value, source="sealed_artifact", seal_digest=digest
                    ),
                    "detail": SealedValue(
                        value=detail, source="sealed_artifact", seal_digest=digest
                    ),
                },
                verified_seal_digest=digest,
            )
            row = await session.scalar(
                select(ClaimRegistryRow).where(ClaimRegistryRow.claim_id == spec.claim_id)
            )
            if row is None:
                raise RuntimeError(f"{spec.claim_id}: registry 行丢失")
            row.bound_value = rendered
            row.updated_at = utc_now()
            await session.flush()
            results.append(
                {
                    "claim_id": spec.claim_id,
                    "suite": spec.suite,
                    "status": record.status.value,
                    "seal_digest": digest,
                    "mode": artifact.mode,
                    "version": artifact.migration_revision,
                    "date": scope_date,
                    "bound_value": rendered,
                }
            )
            print(f"[seed] ✓ {spec.claim_id} -> {record.status.value} {digest}")

    gate_context = await _ensure_gate_tenant(sessions)
    async with tenant_session(sessions, gate_context) as session:
        registry = ClaimRegistryService(session, gate_context, store)
        for claim_id, statement in EXTERNAL_PLANNED_CLAIMS:
            try:
                await registry.get(claim_id)
                print(f"[seed] • {claim_id} 已存在（保持 planned）")
            except ClaimNotFound:
                await registry.register(
                    claim_id=claim_id,
                    statement=statement,
                    scope=ClaimScope(
                        mode="offline",
                        model=SCOPE_MODEL,
                        version="0015_release_claims",
                        date=utc_now().date().isoformat(),
                        corpus="longmemeval-adapter",
                        environment="offline-fixture",
                    ),
                )
                print(f"[seed] ✓ {claim_id} -> planned（外部基准不可用，不解锁质量 claim）")
            results.append(
                {
                    "claim_id": claim_id,
                    "suite": "longmemeval-adapter",
                    "status": "planned",
                    "seal_digest": None,
                    "mode": None,
                    "version": None,
                    "date": None,
                    "bound_value": None,
                }
            )

    await engine.dispose()
    print(f"[seed] ✓ {len(results)} 条 claim 就绪")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="S9 Gate claim registry seeding")
    parser.add_argument(
        "--runs-json",
        type=Path,
        default=Path("artifacts/gates/s9/sealed-runs.json"),
        help="步骤 3 CLI 输出的 verbatim JSON 数组（租户定位用）",
    )
    raise SystemExit(asyncio.run(seed(parser.parse_args().runs_json)))
