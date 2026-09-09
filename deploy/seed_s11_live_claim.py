#!/usr/bin/env python3
"""S11 live 演示 claim seeding（followup-3，operator 2026-09-09 裁决）。

把 live-synthesis-v1 的 live 密封 run 绑定进 Claim Registry（ADR-016 直达边）：

- scope.environment=live-production：升级路径 planned → implemented（手工）→
  live_verified——(IMPLEMENTED, LIVE_VERIFIED) 是 ADR-016 新增证据边，仅接受
  live/shadow 密封件；mix-rule 保证 live-production claim 拒绝 offline 证据；
- 口径纪律与 seed_s9_gate_claims.py 同源：mode/version/date 从密封件与 EvalRun
  行复制，model 从密封 run 的 EvalSample.result 复制（逐单位一致才采信）——
  不在脚本里发明口径值；
- bound_value 聚合自密封 run 的 sample 终态（verdict=pass 计数），模板填充走
  render_claim 的 SealedValue provenance 路径，落库经服务层 bind_value 唯一入口。

输入：--runs-json，`zhiwei eval run --suite live-synthesis-v1 --mode live --seal`
输出的 verbatim JSON（单元素数组）。仅取 eval_run_id/organization_id/workspace_id
做租户定位；密封 digest、mode、migration revision 一律由服务层/密封件复算。

环境变量：ZHIWEI_DATABASE_URL（maintenance 可读 + app 写面复用同一 DSN 时需
BYPASSRLS 角色——live run 的租户行是 app 建的，seed 以维护角色直读复核）。
不读 .env。幂等：已 live_verified 且 evidence 指向同一 eval_run 的 claim 跳过。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select

from zhiwei.agents.claims import (
    ClaimNotFound,
    ClaimRegistryService,
    ClaimScope,
    ClaimStatus,
    SealedValue,
    render_claim,
)
from zhiwei.evals.runs import EvalFoundationService
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import EvalRun, EvalSample
from zhiwei.persistence.tenant import TenantContext, tenant_session

CLAIM_ID = "live-synthesis-v1.behavior-pass"
STATEMENT = "live-synthesis-v1 live 合成 behavior 判分通过单位 {{value}}（{{detail}}）"
SCOPE_ENVIRONMENT = "live-production"
SCOPE_CORPUS = "live-synthesis-v1"


def _metric(rows: list[EvalSample], registered: int) -> tuple[str, str]:
    """behavior-pass 口径：全部单位 verdict=pass（可答判分 + 短路行为标签）。"""
    total = len(rows)
    if total != registered:
        raise RuntimeError(f"live-synthesis-v1: samples {total} != registered {registered}")
    passed = sum(1 for row in rows if (row.result or {}).get("verdict") == "pass")
    if passed != total:
        raise RuntimeError(
            f"live-synthesis-v1: 存在非 pass 单位（{passed}/{total}），拒绝绑定"
        )
    return f"{passed}/{total}", "units pass on live production path"


def _model_of(rows: list[EvalSample]) -> str:
    """从密封 run 的 sample result 复制 model 口径；不一致即拒绝（不猜）。"""
    models = {str(value) for row in rows if (value := (row.result or {}).get("model"))}
    if len(models) != 1:
        raise RuntimeError(f"live-synthesis-v1: sample model 口径不一致: {sorted(models)}")
    return models.pop()


async def seed(runs_json: Path) -> int:
    settings = load_settings_guarded()
    entries = json.loads(runs_json.read_text(encoding="utf-8"))
    if len(entries) != 1:
        print(f"[seed] ✗ 期望单元素 runs-json，收到 {len(entries)}", file=sys.stderr)
        return 1
    entry = entries[0]
    if entry.get("suite") != SCOPE_CORPUS:
        print(f"[seed] ✗ suite 不是 {SCOPE_CORPUS}: {entry.get('suite')}", file=sys.stderr)
        return 1
    context = TenantContext(
        organization_id=UUID(entry["organization_id"]),
        workspace_id=UUID(entry["workspace_id"]),
    )
    eval_run_id = UUID(entry["eval_run_id"])
    store = PosixObjectStore(settings.object_store_root)
    engine = create_database_engine(settings.database_url.get_secret_value())
    sessions = create_session_factory(engine)

    async with tenant_session(sessions, context) as session:
        # 复核密封件（从 object store 复算）+ 读取口径标签的权威来源
        artifact = await EvalFoundationService(session, context, store).verify_sealed(
            eval_run_id
        )
        eval_run = await session.get(EvalRun, eval_run_id)
        if eval_run is None or eval_run.status != "sealed" or eval_run.sealed_at is None:
            print("[seed] ✗ sealed EvalRun 缺失或未密封", file=sys.stderr)
            return 1
        if artifact.mode != "live":
            print(
                f"[seed] ✗ 密封件 mode 不是 live: {artifact.mode}（live-production "
                "claim 拒绝非 live 证据，mix-rule）",
                file=sys.stderr,
            )
            return 1
        rows = list(
            await session.scalars(select(EvalSample).where(EvalSample.eval_run_id == eval_run_id))
        )
        value, detail = _metric(rows, len(artifact.registered_units))
        model = _model_of(rows)
        scope_date = eval_run.sealed_at.date().isoformat()

        registry = ClaimRegistryService(session, context, store)
        try:
            record = await registry.get(CLAIM_ID)
        except ClaimNotFound:
            record = await registry.register(
                claim_id=CLAIM_ID,
                statement=STATEMENT,
                scope=ClaimScope(
                    mode=artifact.mode,
                    model=model,
                    version=artifact.migration_revision,
                    date=scope_date,
                    corpus=SCOPE_CORPUS,
                    environment=SCOPE_ENVIRONMENT,
                ),
            )
        if (
            record.status is ClaimStatus.LIVE_VERIFIED
            and record.evidence is not None
            and record.evidence.eval_run_id == eval_run_id
        ):
            print(f"[seed] • {CLAIM_ID} 已 live_verified（幂等跳过）{record.evidence.seal_digest}")
            return 0
        if record.status is ClaimStatus.PLANNED:
            # ADR-016：planned → implemented 是手工步（无 eval 证据）
            record = await registry.upgrade(
                CLAIM_ID, target=ClaimStatus.IMPLEMENTED, eval_run_id=None
            )
        if record.status is ClaimStatus.IMPLEMENTED:
            # ADR-016 直达边：implemented → live_verified，仅接受 live 密封件
            record = await registry.upgrade(
                CLAIM_ID, target=ClaimStatus.LIVE_VERIFIED, eval_run_id=eval_run_id
            )
        if record.status is not ClaimStatus.LIVE_VERIFIED or record.evidence is None:
            print(f"[seed] ✗ {CLAIM_ID} 未达 live_verified", file=sys.stderr)
            return 1
        if record.evidence.eval_run_id != eval_run_id:
            print(f"[seed] ✗ {CLAIM_ID}: evidence 绑定到其它 run", file=sys.stderr)
            return 1
        digest = record.evidence.seal_digest
        rendered = render_claim(
            record,
            {
                "value": SealedValue(value=value, source="sealed_artifact", seal_digest=digest),
                "detail": SealedValue(
                    value=detail, source="sealed_artifact", seal_digest=digest
                ),
            },
            verified_seal_digest=digest,
        )
        # 绑定值经服务层唯一入口落库（fail closed：verified 态 + 证据
        # seal_digest 与复核 digest 一致才写入）
        await registry.bind_value(CLAIM_ID, rendered, digest)
        print(
            json.dumps(
                {
                    "claim_id": CLAIM_ID,
                    "status": record.status.value,
                    "mode": artifact.mode,
                    "model": model,
                    "environment": SCOPE_ENVIRONMENT,
                    "version": artifact.migration_revision,
                    "date": scope_date,
                    "seal_digest": digest,
                    "bound_value": rendered,
                },
                ensure_ascii=False,
            )
        )
    return 0


def load_settings_guarded() -> Any:
    from zhiwei.config.settings import load_settings

    settings = load_settings()
    if settings.database_url is None or settings.object_store_root is None:
        print("[seed] ✗ 需要 ZHIWEI_DATABASE_URL 与 ZHIWEI_OBJECT_STORE_ROOT", file=sys.stderr)
        sys.exit(1)
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-json", type=Path, required=True)
    args = parser.parse_args()
    return asyncio.run(seed(args.runs_json))


if __name__ == "__main__":
    sys.exit(main())
