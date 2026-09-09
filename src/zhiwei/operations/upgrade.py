"""S11-T3 升级机制（docs/operations/upgrade.md §2 冻结契约）。

组件：
- OutboxRowView / read_outbox_rows_cross_era：backward reader（§2.3）；
- UpgradeManifest：升级清单（§2.4 marker + §2.6 preflight）；
- upgrade_run：preflight → expand → (checkpoint gate) → contract 编排（§2.6）；
- opensearch_rebuild_and_switch：真实 OpenSearch rebuild + alias 原子切换（§2.5）。

纪律：contract 是 destructive（NOT NULL 收紧），无显式 checkpoint 拒绝执行；
rollback 只承诺 expand 后、contract 前的窗口（§2.6）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from alembic import command
from alembic.config import Config
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# backward reader fallback 与 0026 回填常量必须一致（upgrade.md §2.2/§2.3）
GRACE_SECONDS = 300

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"
KNOWN_SCHEMA_VERSIONS = {1, 2}

# 三段式锚点 revision（§2.2）
EXPAND_REVISION = "0025_expand_dispatch_deadline"


class OutboxRowView(BaseModel):
    """outbox 行的跨代读取视图（reader 输入）。"""

    id: str
    topic: str
    schema_version: int
    available_at: datetime
    dispatch_deadline: datetime | None
    payload_schema_version: int


class OutboxRowReadView(BaseModel):
    """reader 输出：era 标注 + 生效 deadline（v1 行用 fallback）。"""

    id: str
    topic: str
    era: str
    effective_deadline: datetime
    legacy: bool


def read_outbox_rows_cross_era(
    rows: list[OutboxRowView],
    *,
    now: datetime,
) -> list[OutboxRowReadView]:
    """跨迁移代读取 outbox 行（§2.3 冻结契约）。

    - schema_version 不在已知集 → 拒绝（fail closed，未知 schema 一律拒绝）；
    - dispatch_deadline IS NULL → v1 代（expand 前写入）：fallback available_at+GRACE；
    - 非 NULL → v2 代，deadline 原样保留。
    """
    views: list[OutboxRowReadView] = []
    for row in rows:
        if row.schema_version not in KNOWN_SCHEMA_VERSIONS:
            raise ValueError(f"schema_version {row.schema_version} 未知（fail closed）")
        if row.payload_schema_version not in KNOWN_SCHEMA_VERSIONS:
            raise ValueError(
                f"payload schema_version {row.payload_schema_version} 未知（fail closed）"
            )
        if row.dispatch_deadline is None:
            views.append(
                OutboxRowReadView(
                    id=row.id,
                    topic=row.topic,
                    era="v1",
                    effective_deadline=row.available_at + timedelta(seconds=GRACE_SECONDS),
                    legacy=True,
                )
            )
        else:
            views.append(
                OutboxRowReadView(
                    id=row.id,
                    topic=row.topic,
                    era="v2",
                    effective_deadline=row.dispatch_deadline,
                    legacy=False,
                )
            )
    return views


class UpgradeManifest(BaseModel):
    """升级清单（§2.4/§2.6/§2.7）：preflight 的全部前置声明。

    claims_snapshot_digest：升级前 release claims 的聚合 digest（§2.7 version pin
    锚）；None = 该环境未启用 claims 校验（不声称已校验）。
    """

    previous_revision: str
    target_revision: str
    worker_build_id: str
    requires_checkpoint: bool
    opensearch_rebuild: bool
    temporal_target: str | None = None
    claims_snapshot_digest: str | None = None

    def check_matches(self, *, current_revision: str, worker_build_id: str) -> bool:
        return current_revision == self.previous_revision and worker_build_id == self.worker_build_id


def _preflight_temporal_reachable(manifest: UpgradeManifest) -> None:
    """§2.6 preflight：Temporal 可达（manifest 声明 target 时强制）。"""
    import socket as _socket
    from urllib.parse import urlparse

    if not manifest.temporal_target:
        return
    parsed = urlparse(f"//{manifest.temporal_target}")
    host, port = parsed.hostname, parsed.port or 7233
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        if sock.connect_ex((host, port)) != 0:
            raise RuntimeError(f"preflight 失败：Temporal {host}:{port} 不可达（abort）")
    finally:
        sock.close()


def _preflight_claims_pin(manifest: UpgradeManifest, dsn: str) -> None:
    """§2.7：升级前 claims pin 快照校验（声明了快照 digest 才校验——不声称即不校验）。

    聚合口径：系统级读取 claim_registry 全行（id/status/evidence/seal digest）的
    canonical JSON digest；与 manifest.claims_snapshot_digest 不符 → abort。
    读取经系统级连接（maintenance 口径，与 release checker 同一约定）。
    """
    import hashlib
    import json as _json

    if manifest.claims_snapshot_digest is None:
        return
    from sqlalchemy import text

    from zhiwei.persistence.database import create_database_engine

    async def _snapshot() -> str:
        engine = create_database_engine(dsn)
        try:
            async with engine.connect() as conn:
                # maintenance 读取：RLS 见「全部租户」需要特权角色——与 release
                # checker 同一口径（DSN 由调用方声明为系统级）。
                rows = (
                    await conn.execute(
                        text(
                            "SELECT claim_id, status, coalesce(evidence::text, 'null')"
                            " FROM claim_registry ORDER BY claim_id"
                        )
                    )
                ).all()
        finally:
            await engine.dispose()
        payload = _json.dumps([list(row) for row in rows], sort_keys=True)
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    actual = asyncio.run(_snapshot())
    if actual != manifest.claims_snapshot_digest:
        raise RuntimeError(
            f"preflight 失败：claims pin 快照不符（§2.7）——"
            f"manifest={manifest.claims_snapshot_digest[:16]}… actual={actual[:16]}…（abort）"
        )


class UpgradeResult(BaseModel):
    """升级序列停在的阶段：expand_done / contract_done。"""

    stopped_at: str
    revisions_applied: list[str]


def _alembic_config(dsn: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(Path(ALEMBIC_INI).parent / "migrations"))
    cfg.attributes["database_url"] = dsn
    return cfg


def _current_revision(dsn: str) -> str | None:
    from sqlalchemy import text

    from zhiwei.persistence.database import create_database_engine

    async def _query() -> str | None:
        engine = create_database_engine(dsn)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT version_num FROM alembic_version")
                )
                return result.scalar_one_or_none()
        finally:
            await engine.dispose()

    return asyncio.run(_query())


def upgrade_run(
    manifest: UpgradeManifest,
    *,
    dsn: str,
    checkpoint: bool = False,
    contract_phase: bool = False,
    worker_build_id: str | None = None,
) -> UpgradeResult:
    """preflight → expand → (checkpoint gate) → contract（§2.6 冻结序列）。

    - preflight 失败 → abort（不执行、不自动降级）；
    - 无 checkpoint：执行到 expand 后停下（旧代码可继续跑）；
    - contract_phase=True 且 checkpoint=True：执行 contract（destructive）；
    - expand 后、contract 前 downgrade 由调用方经 alembic 显式执行（rollback 窗口）。
    """
    current_build_id = worker_build_id or os.environ.get("ZHIWEI_WORKER_BUILD_ID") or "dev-local"
    current = _current_revision(dsn)
    if current is None:
        raise RuntimeError("preflight 失败：alembic_version 不存在（未迁移的数据库）")
    resuming = current == EXPAND_REVISION
    # 断点续跑（expand 已执行、停在 checkpoint 前）也必须核 build id（F-P2-15 修复：
    # 续跑不是免检通道）；其余 revision 不符 → abort（§2.6）。
    if not resuming and not manifest.check_matches(
        current_revision=current, worker_build_id=current_build_id
    ):
        raise RuntimeError(
            f"preflight 失败：revision={current} 或 build id 与 manifest 不符（abort）"
        )
    if resuming and current_build_id != manifest.worker_build_id:
        raise RuntimeError("preflight 失败：续跑 build id 与 manifest 不符（abort）")
    _preflight_temporal_reachable(manifest)
    _preflight_claims_pin(manifest, dsn)

    applied: list[str] = []
    if not resuming:
        # expand：0025
        command.upgrade(_alembic_config(dsn), EXPAND_REVISION)
        applied.append(EXPAND_REVISION)

    if not contract_phase:
        return UpgradeResult(stopped_at="expand_done", revisions_applied=applied)

    # checkpoint gate：contract 是 destructive，必须显式确认（§2.2/§2.6）
    if not checkpoint:
        raise PermissionError("contract 是 destructive 迁移，需要显式 checkpoint 才能执行")
    command.upgrade(_alembic_config(dsn), manifest.target_revision)
    applied.append(manifest.target_revision)
    return UpgradeResult(stopped_at="contract_done", revisions_applied=applied)


class OpenSearchRebuildSummary(BaseModel):
    alias: str
    new_index: str
    document_count: int
    alias_targets_new_index: bool


def opensearch_rebuild_and_switch(
    *,
    endpoint: str,
    alias: str,
    documents: list[dict[str, Any]],
) -> OpenSearchRebuildSummary:
    """rebuild + alias 原子切换（§2.5）：新索引 bulk → 同一 _alias 动作 remove+add。

    Source Ledger 不参与（索引操作不触账——OpenSearchPort 同一纪律）。
    """
    async def _run() -> OpenSearchRebuildSummary:
        async with httpx.AsyncClient(timeout=30) as client:
            # 解析当前 alias 版本
            alias_resp = await client.get(f"{endpoint}/_alias/{alias}")
            current_version = 0
            if alias_resp.status_code == 200:
                indices = alias_resp.json()
                for index_name in indices:
                    suffix = index_name.rsplit("-v", 1)[-1]
                    if suffix.isdigit():
                        current_version = max(current_version, int(suffix))
            new_version = current_version + 1
            new_index = f"{alias}-v{new_version}"

            create = await client.put(
                f"{endpoint}/{new_index}",
                json={"settings": {"number_of_shards": 1, "number_of_replicas": 0}},
            )
            if create.status_code not in (200, 201):
                raise RuntimeError(f"创建索引失败: {create.text[:200]}")

            if documents:
                bulk_body = ""
                for doc in documents:
                    bulk_body += (
                        json.dumps(
                            {"index": {"_index": new_index, "_id": doc["id"]}},
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
                    bulk_body += json.dumps(doc) + "\n"
                bulk = await client.post(
                    f"{endpoint}/_bulk",
                    content=bulk_body,
                    headers={"Content-Type": "application/x-ndjson"},
                )
                if bulk.status_code != 200:
                    raise RuntimeError(f"bulk 写入失败: {bulk.text[:200]}")

            await client.post(f"{endpoint}/{new_index}/_refresh")

            # 原子切换：同一 _alias 动作内 remove 旧 + add 新
            actions: list[dict[str, Any]] = [
                {"add": {"index": new_index, "alias": alias}}
            ]
            if current_version > 0:
                actions.insert(
                    0, {"remove": {"index": f"{alias}-v{current_version}", "alias": alias}}
                )
            switch = await client.post(
                f"{endpoint}/_aliases",
                json={"actions": actions},
            )
            if switch.status_code != 200:
                raise RuntimeError(f"alias 切换失败: {switch.text[:200]}")

            check = await client.get(f"{endpoint}/{new_index}/_count")
            count = check.json().get("count", 0)
            alias_state = await client.get(f"{endpoint}/_alias/{alias}")
            targets_new = new_index in (alias_state.json() or {})
            return OpenSearchRebuildSummary(
                alias=alias,
                new_index=new_index,
                document_count=count,
                alias_targets_new_index=targets_new,
            )

    return asyncio.run(_run())
