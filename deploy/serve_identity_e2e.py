#!/usr/bin/env python3
"""S1 tenancy e2e 本地后端：从环境变量组装 create_app 并跑 uvicorn(127.0.0.1:8000)。

只做本地 e2e bring-up（N-4 可复现环境的一部分）；不读 .env，全部配置来自调用方
环境变量。必需变量与 create_app 组合期校验一致（fail closed）：

    ZHIWEI_PROFILE=test
    ZHIWEI_DATABASE_URL / ZHIWEI_IDENTITY_DATABASE_URL  （compose 测试库）
    ZHIWEI_OIDC_ISSUER=http://localhost:8080/realms/zhiwei
    ZHIWEI_OIDC_CLIENT_ID=zhiwei-bff
    ZHIWEI_OIDC_CLIENT_SECRET=（compose identity profile 默认值）
    ZHIWEI_OIDC_REDIRECT_URI=http://localhost:5173/auth/callback
    ZHIWEI_IDENTITY_MASTER_KEY_FILE=deploy/compose/secrets/zhiwei_identity_master_key
    ZHIWEI_OPA_BASE_URL=http://127.0.0.1:8181
    ZHIWEI_OBJECT_STORE_ROOT=（本地 scratch 目录）

用法：uv run python deploy/serve_identity_e2e.py（前台运行；Ctrl-C 退出）
"""

from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from zhiwei.app import create_app
from zhiwei.config.settings import load_settings


def build_app() -> FastAPI:
    settings = load_settings(dict(os.environ))
    return create_app(settings)


# 模块级 app：uvicorn.run 直用；create_app 组合期校验缺配置即抛（fail closed）。
app: FastAPI = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
