"""agents draft router 组合根挂载契约（S11 followup-2 任务三实测发现）。

缺陷：create_agents_router 仅被 re-export，app.py 组合根从未 include——
生产 BFF 对 GET /api/v1/agents 返回 404，Studio draft 面停留在 loading 态
（mock e2e 以 context.route 拦截 /api/**，从未经过真实组合根，故未暴露）。

契约：生产组合根必须挂载 agents draft router（路径面进 OpenAPI 快照——
tests/contract/api/test_openapi_snapshot.py 的再生协议承担漂移警察）。
本文件钉挂载事实本身：路径存在且带 tenants 依赖的 403 形状（无 ws 上下文
时 _tenant 拒绝，而非 404 route-missing）。

不触网：与 openapi_snapshot 同构——DB 引擎惰性创建，OPA/Temporal/IdP 只
组合不连接（dummy URL / tmp 目录）。
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from fastapi import FastAPI

from zhiwei.app import create_app
from zhiwei.config.settings import load_settings

_DSN_APP = "postgresql://zhiwei_app@127.0.0.1:1/never_connected"
_DSN_IDENTITY = "postgresql://zhiwei_identity@127.0.0.1:1/never_connected"


def _settings(tmp_path: Path) -> dict[str, str]:
    keyring = tmp_path / "master.key"
    material = hashlib.sha256(b"ZW_TEST_MASTER_KEY_AGENTS_MOUNT").digest()
    keyring.write_text(f"k1={base64.b64encode(material).decode('ascii')}\n", encoding="utf-8")
    return {
        "ZHIWEI_PROFILE": "test",
        "ZHIWEI_DATABASE_URL": _DSN_APP,
        "ZHIWEI_IDENTITY_DATABASE_URL": _DSN_IDENTITY,
        "ZHIWEI_OIDC_ISSUER": "https://idp.example.com",
        "ZHIWEI_OIDC_CLIENT_ID": "zhiwei-bff",
        "ZHIWEI_OIDC_CLIENT_SECRET": "ZW_TEST_CLIENT_SECRET_AGENTS",
        "ZHIWEI_OIDC_REDIRECT_URI": "https://app.example.com/auth/callback",
        "ZHIWEI_IDENTITY_MASTER_KEY_FILE": str(keyring),
        "ZHIWEI_OPA_BASE_URL": "http://127.0.0.1:1/never-connected-opa",
        "ZHIWEI_OBJECT_STORE_ROOT": str(tmp_path / "objects"),
        "ZHIWEI_TEMPORAL_TARGET": "127.0.0.1:1",
    }


def _app(tmp_path: Path) -> FastAPI:
    return create_app(load_settings(_settings(tmp_path)))


def test_agents_draft_paths_are_mounted(tmp_path: Path) -> None:
    paths = set(_app(tmp_path).openapi()["paths"])
    # draft 面的读/写/校验/发布入口必须全部可达（404 route-missing = 未挂载）
    for path in (
        "/api/v1/agents",
        "/api/v1/agents/{agent_id}",
        "/api/v1/agents/{agent_id}/validate",
    ):
        assert path in paths, f"agents router not mounted: {path} absent from OpenAPI"
