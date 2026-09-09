"""API 面 OpenAPI 快照 diff 归因（REMEDIATION_PLAN T-P2a.5 / F-R6-09 新建机制）。

快照 = 生产组合根 create_app 的 app.openapi() 输出，落盘
tests/contract/api/openapi_snapshot.json。API 面变更（新增/删除/改签名 path）
必须：① 在引入该面的同一提交内再生快照；② 在提交信息中逐条归因 path 变更
（对齐 P1 golden 再生协议：diff 中归因不了的变更 = 未授权 API 面漂移，验收
窗口打回）。本测试不触网：DB 引擎惰性创建（openapi() 不查询），OPA/Temporal/
IdP/ObjectStore 只组合不连接（dummy URL / tmp 目录）。

条件挂载面（object_store/temporal）以 dummy 值显式开启——快照覆盖全部生产
挂载面，与组合根的挂载条件保持同构（新增条件挂载面时本文件 settings 需同
步补键，否则该面对快照不可见）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from zhiwei.app import create_app
from zhiwei.config.settings import load_settings

REPO_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT_PATH = Path(__file__).resolve().parent / "openapi_snapshot.json"

_DSN_APP = "postgresql://zhiwei_app@127.0.0.1:1/never_connected"
_DSN_IDENTITY = "postgresql://zhiwei_identity@127.0.0.1:1/never_connected"


def _settings(tmp_path: Path) -> dict[str, str]:
    keyring = tmp_path / "master.key"
    material = hashlib.sha256(b"ZW_TEST_MASTER_KEY_OPENAPI").digest()
    keyring.write_text(f"k1={base64.b64encode(material).decode('ascii')}\n", encoding="utf-8")
    return {
        "ZHIWEI_PROFILE": "test",
        "ZHIWEI_DATABASE_URL": _DSN_APP,
        "ZHIWEI_IDENTITY_DATABASE_URL": _DSN_IDENTITY,
        "ZHIWEI_OIDC_ISSUER": "https://idp.example.com",
        "ZHIWEI_OIDC_CLIENT_ID": "zhiwei-bff",
        "ZHIWEI_OIDC_CLIENT_SECRET": "ZW_TEST_CLIENT_SECRET_OPENAPI",
        "ZHIWEI_OIDC_REDIRECT_URI": "https://app.example.com/auth/callback",
        "ZHIWEI_IDENTITY_MASTER_KEY_FILE": str(keyring),
        "ZHIWEI_OPA_BASE_URL": "http://127.0.0.1:1/never-connected-opa",
        "ZHIWEI_OBJECT_STORE_ROOT": str(tmp_path / "objects"),
        "ZHIWEI_TEMPORAL_TARGET": "127.0.0.1:1",
    }


def _app(tmp_path: Path) -> FastAPI:
    return create_app(load_settings(_settings(tmp_path)))


def test_openapi_matches_snapshot(tmp_path: Path) -> None:
    schema = _app(tmp_path).openapi()
    # 快照文件以换行收尾（POSIX 文本惯例）；比较时对齐
    actual = json.dumps(schema, sort_keys=True, indent=1) + "\n"
    if not SNAPSHOT_PATH.exists():
        pytest.fail(
            "openapi snapshot missing; regenerate with the API-surface change "
            f"and attribute path diffs in the commit: {SNAPSHOT_PATH}"
        )
    expected = SNAPSHOT_PATH.read_text(encoding="utf-8")
    if actual != expected:
        actual_doc = json.loads(actual)
        expected_doc = json.loads(expected)
        actual_paths = set(actual_doc.get("paths", {}))
        expected_paths = set(expected_doc.get("paths", {}))
        added = sorted(actual_paths - expected_paths)
        removed = sorted(expected_paths - actual_paths)
        pytest.fail(
            "openapi snapshot drift — attribute every path change in the "
            f"commit, then regenerate {SNAPSHOT_PATH}:\n"
            f"added paths: {added}\nremoved paths: {removed}\n"
            "(non-path schema drift is also drift: inspect the full diff)"
        )


@pytest.mark.parametrize(
    "path",
    ["/api/v1/audit-events"],
)
def test_audit_events_surface_is_declared(tmp_path: Path, path: str) -> None:
    """F-R6-09 RED：audit 列表端点必须在生产 API 面上声明。"""
    paths = _app(tmp_path).openapi().get("paths", {})
    assert path in paths, sorted(paths)
