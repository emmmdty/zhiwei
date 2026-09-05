"""真实栈 transport 兼容契约：authlib（httpx_client 集成）× httpx2 transport。

背景（s1-t6 §5-1 在 httpx2 迁移后的真实形态）：`OIDCService._oauth_client` 把
httpx2 transport 注入 authlib 的 `AsyncOAuth2Client`。生产进程里 authlib 走
真实 httpx（httpx2.alias_httpx 只在测试进程生效），其 Request 携带同步
ByteStream；httpx2 默认异步 transport 断言
`isinstance(request.stream, AsyncByteStream)`（httpx2/_transports/default.py:372）
——真实 Keycloak 下 token exchange 一律 AssertionError（2026-09-05 真实栈探针
实证）。MockTransport 不做该断言，掩盖缺陷。

本契约的执行环境必须与生产同构：authlib 用真实 httpx、transport 用 httpx2。
pytest 进程内 conftest 已做 httpx→httpx2 别名（模块混用不复现），故场景在
无别名的子进程中运行：authlib（真实 httpx）→ 严格 transport（执行真实栈
stream 断言）。exchange 必须成功，否则真实 IdP 下 token/refresh/revoke 全废。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ISSUER = "https://idp.strict.example"
CLIENT_ID = "zhiwei-bff"
CLIENT_SECRET = "ZW_TEST_CLIENT_SECRET_STRICT"
REDIRECT_URI = "https://app.example/auth/callback"
SUBJECT = "strict-oidc-user"
CODE = "strict-transport-code-1"

_SUBPROCESS_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import base64
    import json
    import time
    from urllib.parse import parse_qs, urlparse

    import httpx2 as httpx
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from zhiwei.identity.oidc import OIDCService

    ISSUER = {ISSUER!r}
    CLIENT_ID = {CLIENT_ID!r}
    CLIENT_SECRET = {CLIENT_SECRET!r}
    REDIRECT_URI = {REDIRECT_URI!r}
    SUBJECT = {SUBJECT!r}
    CODE = {CODE!r}

    def _b64url(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = key.public_key().public_numbers()
    jwk = {{
        "kty": "RSA", "use": "sig", "kid": "test-kid", "alg": "RS256",
        "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }}
    NONCE = {{}}

    class StrictAsyncTransport(httpx.AsyncBaseTransport):
        # 与 httpx2/_transports/default.py:372 同款断言：真实异步 transport
        # 拒绝非 AsyncByteStream 的请求流。token endpoint 承载请求体，必须断言。
        async def handle_async_request(self, request):
            if request.url.path == "/token":
                assert isinstance(request.stream, httpx.AsyncByteStream), (
                    "authlib 发出的 token 请求携带非 httpx2 异步流：真实 transport "
                    "抛 AssertionError（生产混用复现）"
                )
            return handler(request)

    def handler(request):
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={{
                "issuer": ISSUER,
                "authorization_endpoint": f"{{ISSUER}}/authorize",
                "token_endpoint": f"{{ISSUER}}/token",
                "jwks_uri": f"{{ISSUER}}/jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
            }})
        if request.url.path == "/jwks":
            return httpx.Response(200, json={{"keys": [jwk]}})
        if request.url.path == "/token":
            body = parse_qs(request.read().decode("utf-8"))
            assert body.get("code", [""])[0] == CODE
            now = int(time.time())
            header = _b64url(b'{{"alg":"RS256","typ":"JWT","kid":"test-kid"}}')
            claims = {{
                "iss": ISSUER, "aud": [CLIENT_ID], "azp": CLIENT_ID,
                "sub": SUBJECT, "exp": now + 3600, "iat": now - 5,
                "nonce": NONCE["value"],
            }}
            payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
            signing_input = f"{{header}}.{{payload}}".encode()
            signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
            return httpx.Response(200, json={{
                "access_token": "strict-access",
                "refresh_token": "strict-refresh",
                "id_token": f"{{header}}.{{payload}}.{{_b64url(signature)}}",
                "token_type": "Bearer",
                "expires_in": 3600,
            }})
        return httpx.Response(404)

    async def main():
        service = OIDCService(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            redirect_uri=REDIRECT_URI,
            http_client=httpx.AsyncClient(transport=StrictAsyncTransport(), timeout=5.0),
        )
        try:
            auth_url, attempt = await service.create_login_attempt()
            NONCE["value"] = parse_qs(urlparse(auth_url).query)["nonce"][0]
            tokens, token_claims = await service.exchange_code(
                code=CODE, attempt=attempt
            )
            assert token_claims["sub"] == SUBJECT
            assert tokens["access_token"] == "strict-access"
        finally:
            await service.aclose()
        print("EXCHANGE_OK")

    asyncio.run(main())
    """
).format(
    ISSUER=ISSUER,
    CLIENT_ID=CLIENT_ID,
    CLIENT_SECRET=CLIENT_SECRET,
    REDIRECT_URI=REDIRECT_URI,
    SUBJECT=SUBJECT,
    CODE=CODE,
)


def test_exchange_survives_authlib_real_httpx_with_httpx2_transport(tmp_path: Path) -> None:
    """生产同构场景（无 httpx 别名）：exchange 必须通过真实 transport 的流断言。"""
    script = tmp_path / "strict_transport_scenario.py"
    script.write_text(_SUBPROCESS_SCRIPT, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert proc.returncode == 0, (
        "生产同构场景（authlib 真实 httpx × httpx2 transport）下 exchange 失败：\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    assert "EXCHANGE_OK" in proc.stdout
