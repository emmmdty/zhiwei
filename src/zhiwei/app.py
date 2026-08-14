"""应用组合根（S1-T2）。

契约（冻结）：
- auth app 缺 identity DSN / OIDC issuer+client+secret+redirect / master-key file /
  app DSN 任一项都在组合期拒绝（fail closed）；
- 组合真实 session actor 依赖（cookie → principal + CSRF/Origin + membership）与
  现有 T1 routers（organizations / workspaces / members）；
- master key 只从显式挂载文件加载（load_keyring），不存在 → 组合期失败；
- 不得用测试专用认证旁路挂载生产 routers；OIDC transport 是唯一外部 binding 替换点
  （MockTransport 只出现在测试）。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request

from zhiwei.api.auth import (
    SESSION_COOKIE,
    _cookie_flags,
    create_auth_router,
    create_session_actor_dependency,
)
from zhiwei.api.memberships import create_memberships_router
from zhiwei.api.organizations import create_organizations_router
from zhiwei.api.workspaces import create_workspaces_router
from zhiwei.config.settings import Settings
from zhiwei.identity.oidc import OIDCService
from zhiwei.identity.sessions import (
    AuthSessionStore,
    LocalSessionRefreshUnitOfWork,
    SessionService,
)
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.secrets.local import LocalSecretBackend, load_keyring

_REQUIRED = (
    ("ZHIWEI_DATABASE_URL", "database_url"),
    ("ZHIWEI_IDENTITY_DATABASE_URL", "identity_database_url"),
    ("ZHIWEI_OIDC_ISSUER", "oidc_issuer"),
    ("ZHIWEI_OIDC_CLIENT_ID", "oidc_client_id"),
    ("ZHIWEI_OIDC_CLIENT_SECRET", "oidc_client_secret"),
    ("ZHIWEI_OIDC_REDIRECT_URI", "oidc_redirect_uri"),
    ("ZHIWEI_IDENTITY_MASTER_KEY_FILE", "identity_master_key_file"),
)


def create_app(settings: Settings, *, oidc_http_client: Any | None = None) -> FastAPI:
    missing = [env for env, attr in _REQUIRED if getattr(settings, attr) is None]
    if missing:
        raise ValueError("auth app 组合期拒绝：缺少 " + ", ".join(missing))
    database_url = settings.database_url
    identity_database_url = settings.identity_database_url
    oidc_issuer = settings.oidc_issuer
    oidc_client_id = settings.oidc_client_id
    oidc_client_secret = settings.oidc_client_secret
    oidc_redirect_uri = settings.oidc_redirect_uri
    master_key_file = settings.identity_master_key_file
    if (
        database_url is None
        or identity_database_url is None
        or oidc_issuer is None
        or oidc_client_id is None
        or oidc_client_secret is None
        or oidc_redirect_uri is None
        or master_key_file is None
    ):
        raise ValueError("auth app 组合期拒绝：缺少必需配置")
    assert database_url is not None and identity_database_url is not None
    assert oidc_issuer is not None and oidc_client_id is not None
    assert oidc_client_secret is not None and oidc_redirect_uri is not None
    assert master_key_file is not None

    app_engine = create_database_engine(database_url.get_secret_value())
    identity_engine = create_database_engine(identity_database_url.get_secret_value())
    app_sessions = create_session_factory(app_engine)
    identity_sessions = create_session_factory(identity_engine)

    # master key 只从显式挂载文件加载；文件缺失/损坏在组合期失败（fail closed）
    keyring = load_keyring(master_key_file)
    secret_backend = LocalSecretBackend(identity_sessions, keyring)
    session_store = AuthSessionStore(identity_sessions)
    # 类型化 UoW adapter：envelope 改写与 session 完成同事务（验收阻断 3/4，
    # SecretBackend port 不含数据库/session 参数）
    refresh_uow = LocalSessionRefreshUnitOfWork(
        session_factory=identity_sessions,
        secret_backend=secret_backend,
        session_store=session_store,
    )
    oidc_service = OIDCService(
        issuer=oidc_issuer,
        client_id=oidc_client_id,
        client_secret=oidc_client_secret.get_secret_value(),
        redirect_uri=oidc_redirect_uri,
        http_client=oidc_http_client,
    )
    session_service = SessionService(
        session_store=session_store,
        secret_backend=secret_backend,
        refresh_uow=refresh_uow,
        oidc_service=oidc_service,
        identity_session_factory=identity_sessions,
    )

    app = FastAPI(title="zhiwei", version="0.1.0")

    @app.middleware("http")
    async def clear_session_cookie(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if getattr(request.state, "clear_session_cookie", False):
            response.delete_cookie(SESSION_COOKIE, **_cookie_flags())
        return response

    # T1 工厂的类型签名为 Callable[[], ActorContext]（FastAPI Depends 兼容任意
    # callable）；真实依赖是 async callable，组合期以 Any 承接。
    session_actor: Any = create_session_actor_dependency(session_service)
    app.include_router(
        create_auth_router(
            session_service=session_service,
            oidc_service=oidc_service,
            session_actor=session_actor,
        )
    )
    app.include_router(
        create_organizations_router(
            actor_dependency=session_actor,
            sessions=app_sessions,
            identity_sessions=identity_sessions,
        )
    )
    app.include_router(create_workspaces_router(actor_dependency=session_actor, sessions=app_sessions))
    app.include_router(create_memberships_router(actor_dependency=session_actor, sessions=app_sessions))

    async def dispose_engines() -> None:
        await oidc_service.aclose()
        await app_engine.dispose()
        await identity_engine.dispose()

    app.state.dispose_engines = dispose_engines
    app.state.session_service = session_service
    return app
