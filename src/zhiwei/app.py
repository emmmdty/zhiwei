"""S1-T2 RED skeleton：应用组合根。

契约（冻结）：
- auth app 缺 identity DSN / OIDC issuer+client+secret+redirect / master-key file /
  app DSN 任一项都在组合期拒绝（fail closed）；
- 组合真实 session actor 依赖与现有 T1 routers（organizations / workspaces / members）；
- GREEN 阶段接入 OIDCService / SecretBackend / SessionService；
- 不得用测试专用认证旁路挂载生产 routers。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from zhiwei.api.auth import create_auth_router, create_session_actor_dependency
from zhiwei.api.memberships import create_memberships_router
from zhiwei.api.organizations import create_organizations_router
from zhiwei.api.workspaces import create_workspaces_router
from zhiwei.config.settings import Settings
from zhiwei.persistence.database import create_database_engine, create_session_factory

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

    app_engine = create_database_engine(settings.database_url.get_secret_value())  # type: ignore[union-attr]
    identity_engine = create_database_engine(settings.identity_database_url.get_secret_value())  # type: ignore[union-attr]
    app_sessions = create_session_factory(app_engine)
    identity_sessions = create_session_factory(identity_engine)

    app = FastAPI(title="zhiwei", version="0.1.0")

    # GREEN：以真实 session actor 组合 T1 routers；RED 阶段为占位依赖。
    # T1 工厂的类型签名为 Callable[[], ActorContext]（FastAPI Depends 兼容任意 callable），
    # 组合期以 Any 承接，GREEN 统一收紧。
    session_actor: Any = create_session_actor_dependency(session_service=None)
    app.include_router(create_auth_router(session_service=None, oidc_service=None, session_actor=session_actor))
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
        await app_engine.dispose()
        await identity_engine.dispose()

    app.state.dispose_engines = dispose_engines
    app.state.session_service = None
    return app
