"""OpenAPI authentication scheme handling.

Supports API Key (header/query/cookie), HTTP Bearer, and OAuth2 flows.
Extracts security requirements from OpenAPI securitySchemes and validates
that the required credentials are available.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuthSchemeType(StrEnum):
    API_KEY = "apiKey"
    HTTP = "http"
    OAUTH2 = "oauth2"
    OPEN_ID_CONNECT = "openIdConnect"


class ApiKeyLocation(StrEnum):
    HEADER = "header"
    QUERY = "query"
    COOKIE = "cookie"


class OAuthFlowType(StrEnum):
    AUTHORIZATION_CODE = "authorizationCode"
    CLIENT_CREDENTIALS = "clientCredentials"
    IMPLICIT = "implicit"
    PASSWORD = "password"


class AuthScheme(BaseModel):
    """Parsed authentication scheme from OpenAPI securitySchemes."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    scheme_type: AuthSchemeType
    description: str = ""

    api_key_location: ApiKeyLocation | None = None
    api_key_name: str = ""

    http_scheme: str = ""
    bearer_format: str = ""

    oauth_flows: dict[str, dict[str, Any]] = Field(default_factory=dict)
    open_id_connect_url: str = ""


class SecurityRequirement(BaseModel):
    """A parsed security requirement (one option from the security array)."""

    model_config = ConfigDict(frozen=True)

    scheme_names: tuple[str, ...] = ()
    scopes: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class AuthExtractionResult(BaseModel):
    """Result of extracting auth info from an OpenAPI spec."""

    model_config = ConfigDict(frozen=True)

    schemes: dict[str, AuthScheme] = Field(default_factory=dict)
    global_requirements: tuple[SecurityRequirement, ...] = ()
    operation_requirements: dict[str, tuple[SecurityRequirement, ...]] = Field(
        default_factory=dict
    )


class OpenAPIAuthExtractor:
    """Extracts and validates authentication schemes from OpenAPI specs."""

    def extract(
        self,
        security_schemes: dict[str, dict[str, Any]],
        *,
        global_security: list[dict[str, list[str]]] | None = None,
        operation_security: dict[str, list[dict[str, list[str]]]] | None = None,
    ) -> AuthExtractionResult:
        """Extract auth schemes and requirements from an OpenAPI spec."""
        schemes: dict[str, AuthScheme] = {}
        for name, scheme_data in security_schemes.items():
            scheme = self._parse_scheme(name, scheme_data)
            if scheme is not None:
                schemes[name] = scheme

        global_reqs = tuple(
            self._parse_requirement(r) for r in (global_security or [])
        )
        op_reqs: dict[str, tuple[SecurityRequirement, ...]] = {}
        for op_id, reqs in (operation_security or {}).items():
            op_reqs[op_id] = tuple(self._parse_requirement(r) for r in reqs)

        return AuthExtractionResult(
            schemes=schemes,
            global_requirements=global_reqs,
            operation_requirements=op_reqs,
        )

    def _parse_scheme(self, name: str, data: dict[str, Any]) -> AuthScheme | None:
        scheme_type = data.get("type", "")
        try:
            st = AuthSchemeType(scheme_type)
        except ValueError:
            return None

        if st == AuthSchemeType.API_KEY:
            return AuthScheme(
                name=name,
                scheme_type=st,
                description=data.get("description", ""),
                api_key_location=ApiKeyLocation(data.get("in", "header")),
                api_key_name=data.get("name", ""),
            )
        if st == AuthSchemeType.HTTP:
            return AuthScheme(
                name=name,
                scheme_type=st,
                description=data.get("description", ""),
                http_scheme=data.get("scheme", ""),
                bearer_format=data.get("bearerFormat", ""),
            )
        if st == AuthSchemeType.OAUTH2:
            return AuthScheme(
                name=name,
                scheme_type=st,
                description=data.get("description", ""),
                oauth_flows=data.get("flows", {}),
            )
        if st == AuthSchemeType.OPEN_ID_CONNECT:
            return AuthScheme(
                name=name,
                scheme_type=st,
                description=data.get("description", ""),
                open_id_connect_url=data.get("openIdConnectUrl", ""),
            )
        return None

    def _parse_requirement(self, req: dict[str, list[str]]) -> SecurityRequirement:
        return SecurityRequirement(
            scheme_names=tuple(req.keys()),
            scopes={k: tuple(v) for k, v in req.items()},
        )

    def validate_scheme_available(
        self,
        scheme: AuthScheme,
        *,
        available_credentials: dict[str, str],
    ) -> bool:
        """Check if the required credential for a scheme is available."""
        if scheme.scheme_type == AuthSchemeType.API_KEY:
            return scheme.api_key_name in available_credentials
        if scheme.scheme_type == AuthSchemeType.HTTP:
            key = f"auth_{scheme.http_scheme}"
            return key in available_credentials
        if scheme.scheme_type == AuthSchemeType.OAUTH2:
            return f"oauth2_{scheme.name}" in available_credentials
        if scheme.scheme_type == AuthSchemeType.OPEN_ID_CONNECT:
            return f"oidc_{scheme.name}" in available_credentials
        return False
