#!/usr/bin/env python3
"""S1 tenancy e2e 种子：5 个 journey principal + OIDC 外部身份绑定。

N-1 裁决（operator，2026-09-05）：种子层解法——所有 principal 均为「裸」用户，
不播任何 membership。owner 首登时零角色 → bootstrap 放行（authz.rego
bootstrap_org_create 候选 1）；builder/member/approver/auditor 的 org 状态由
owner journey 的邀请步骤产生。改 Rego 或 journey 都不在本脚本范围内。

幂等：重复执行不报错也不改既有行（ON CONFLICT DO NOTHING），可安全复跑。
principal UUID 与 apps/web/e2e/tenancy.spec.ts 的 PRINCIPAL_UUID 一一对应；
subject 与 OIDC_SUBJECT（Keycloak 用户名）一一对应。realm 用户见
deploy/compose/keycloak/realm-template.json。

用法：
    ZHIWEI_OIDC_ISSUER=http://localhost:8080/realms/zhiwei \
        uv run python deploy/seed_identity_e2e.py

不读 .env；issuer 只从环境变量取（缺省用 compose identity profile 的本地地址）。
直连 zhiwei_migrator（identity-global 表不启用 RLS，zhiwei_app 在 0003 被撤销
对 principals/external_identities 的直接访问，应用路径不经本脚本）。
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

ADMIN_DSN = os.environ.get(
    "ZHIWEI_E2E_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
DEFAULT_ISSUER = "http://localhost:8080/realms/zhiwei"

# 与 apps/web/e2e/tenancy.spec.ts 的对应关系：
# - PRINCIPAL_UUID  → principals.id（ZhiWei 侧 principal）
# - Keycloak sub    → external_identities.subject：OIDC 回调以 id_token 的 sub
#   查 principal（Keycloak sub = realm 用户 UUID），故 subject 必须等于 realm
#   模板里固定的用户 id（deploy/compose/keycloak/realm-template.json 的 "id"），
#   用户名（owner-oidc 等）只用于 Keycloak 登录页。
JOURNEY_PRINCIPALS: tuple[tuple[str, str], ...] = (
    ("fd1b9dab-3f88-4b35-803d-f5ab19fae6a8", "7f3b1c2a-9d4e-4a5b-8c6d-1a2b3c4d5e6f"),
    ("3383f6a7-d17b-44c2-802c-d67c3974e13a", "8a4c2d3b-0e5f-4b6c-9d7e-2b3c4d5e6f7a"),
    ("4a3e5ad8-f81e-431d-937f-55b98def2bf2", "ac6e4f5d-2a7b-4d8e-bf9a-4d5e6f7a8b9c"),
    ("f740acc5-03c3-486e-8384-2a9335fd4285", "9b5d3e4c-1f6a-4c7d-ae8f-3c4d5e6f7a8b"),
    ("63d7ef96-75e0-4c47-8edb-10dd834c9f64", "bd7f5a6e-3b8c-4e9f-caab-5e6f7a8b9c0d"),
    # operator 2026-09-05 journey 修订裁决新增：无组织首登用户（empty-state 用）。
    ("3e5a7c9b-1d3f-4b5d-9f81-a2c4e6b8d0f2", "ce1f7a8b-2d3e-4f5a-9b0c-1d2e3f4a5b6c"),
)


async def seed() -> int:
    issuer = os.environ.get("ZHIWEI_OIDC_ISSUER", DEFAULT_ISSUER)
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        for principal_id, subject in JOURNEY_PRINCIPALS:
            await conn.execute(
                """
                INSERT INTO principals (id, kind, status, schema_version)
                VALUES ($1, 'user', 'active', 1)
                ON CONFLICT (id) DO UPDATE SET status = 'active'
                """,
                principal_id,
            )
            # subject 语义曾在早期草稿误用用户名：按 principal 清掉旧绑定，
            # 保证脚本对历史库也自洽（幂等且自纠）。
            await conn.execute(
                "DELETE FROM external_identities WHERE principal_id = $1",
                principal_id,
            )
            await conn.execute(
                """
                INSERT INTO external_identities (issuer, subject, principal_id)
                VALUES ($1, $2, $3)
                ON CONFLICT (issuer, subject) DO NOTHING
                """,
                issuer,
                subject,
                principal_id,
            )
        rows = await conn.fetch(
            """
            SELECT p.id, p.kind, p.status, e.subject
            FROM principals p
            JOIN external_identities e ON e.principal_id = p.id
            WHERE e.issuer = $1 AND e.subject = ANY($2)
            ORDER BY e.subject
            """,
            issuer,
            [subject for _, subject in JOURNEY_PRINCIPALS],
        )
        print(f"[seed] issuer={issuer}")
        for row in rows:
            print(f"[seed] {row['subject']} -> {row['id']} ({row['kind']}/{row['status']})")
        if len(rows) != len(JOURNEY_PRINCIPALS):
            print(f"[seed] ✗ 预期 {len(JOURNEY_PRINCIPALS)} 条绑定，实际 {len(rows)}", file=sys.stderr)
            return 1
        print("[seed] ✓ journey principal 绑定齐备（无 membership，N-1 种子层解法）")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(seed()))
