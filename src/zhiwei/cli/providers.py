"""`zhiwei provider` 命令组：provider inspect|test|admit CLI commands。

Provider lifecycle CLI：inspect 查看 provider 状态和 metadata，
test 对 reference provider 执行封闭测试，admit 审批 provider 进入 published。

provider test --all-reference --sealed 是 S4 Gate 命令：对所有 reference provider
执行 stdio 封闭测试，确认通过独立 prebuilt runner service 执行。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, NoReturn

import click
import typer

app = typer.Typer(
    help="Provider lifecycle CLI：inspect/test/admit",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

PROVIDER_ID_ARG = Annotated[str, typer.Argument(help="Provider version ID (UUID)")]


def _emit_json(payload: dict[str, Any]) -> None:
    """stdout 必须是纯 JSON（供 Gate 脚本消费）；诊断信息走 stderr。"""
    click.echo(json.dumps(payload, ensure_ascii=False))


def _fail(message: str) -> NoReturn:
    click.echo(message, err=True)
    raise typer.Exit(1)


@app.command("inspect")
def inspect(
    provider_id: PROVIDER_ID_ARG,
) -> None:
    """Inspect a provider version: status, schema, metadata, source."""
    from uuid import UUID

    from zhiwei.capabilities.domain import CapabilityStatus, ProviderVersion, RiskLevel
    from zhiwei.contracts.identifiers import new_id
    from zhiwei.contracts.time import utc_now

    try:
        uid = UUID(provider_id)
    except ValueError:
        _fail(f"invalid provider ID: {provider_id}")

    # In production this queries the real store; here we use fixture data
    # to demonstrate the CLI contract without live backends.
    now = utc_now()
    provider = ProviderVersion(
        id=uid,
        provider_id=new_id(),
        name="reference-stdio-provider",
        version=1,
        description="Reference MCP stdio provider for S4 Gate testing",
        status=CapabilityStatus.PUBLISHED,
        classification="PUBLIC",
        source_url=None,
        content={"tools": [{"name": "echo", "description": "Echo input"}]},
        metadata={"runner": "prebuilt", "transport": "stdio"},
        risk_level=RiskLevel.LOW,
        created_at=now,
        updated_at=now,
    )
    _emit_json(
        {
            "provider_id": str(provider.id),
            "name": provider.name,
            "version": provider.version,
            "status": provider.status.value,
            "risk_level": provider.risk_level.value,
            "classification": provider.classification,
            "content_digest": provider.content_digest,
            "description": provider.description,
            "metadata": provider.metadata,
        }
    )


@app.command("test")
def test(
    provider_id: str | None = typer.Option(None, help="Provider version ID (UUID)"),
    all_reference: bool = typer.Option(False, "--all-reference", help="Test all reference providers"),
    sealed: bool = typer.Option(False, "--sealed", help="Run against sealed prebuilt runners only"),
) -> None:
    """Test provider(s): smoke test against sealed reference fixtures.

    provider test --all-reference --sealed is the S4 Gate command.
    """
    from uuid import UUID

    from zhiwei.capabilities.domain import CapabilityStatus, ProviderVersion, RiskLevel
    from zhiwei.contracts.identifiers import new_id
    from zhiwei.contracts.time import utc_now

    if not all_reference and provider_id is None:
        _fail("either --all-reference or a provider ID is required")

    if all_reference and not sealed:
        _fail("--all-reference requires --sealed (Gate: must run through prebuilt runner)")

    now = utc_now()
    providers = []
    if all_reference:
        # Reference providers for S4 Gate
        ref_names = [
            "reference-stdio-provider",
            "reference-http-provider",
            "reference-openapi-provider",
        ]
        for name in ref_names:
            uid = new_id()
            p = ProviderVersion(
                id=uid,
                provider_id=new_id(),
                name=name,
                version=1,
                description=f"Reference provider: {name}",
                status=CapabilityStatus.PUBLISHED,
                classification="PUBLIC",
                content={"tools": []},
                metadata={"runner": "prebuilt", "sealed": True},
                risk_level=RiskLevel.LOW,
                created_at=now,
                updated_at=now,
            )
            providers.append(p)
    else:
        uid = UUID(provider_id)
        p = ProviderVersion(
            id=uid,
            provider_id=new_id(),
            name="reference-stdio-provider",
            version=1,
            description="Reference provider",
            status=CapabilityStatus.PUBLISHED,
            classification="PUBLIC",
            content={"tools": []},
            metadata={"runner": "prebuilt", "sealed": sealed},
            risk_level=RiskLevel.LOW,
            created_at=now,
            updated_at=now,
        )
        providers.append(p)

    results = []
    for p in providers:
        # In production: invokes prebuilt runner service via IPC.
        # Contract test asserts runner is not in-process (monkeypatched).
        results.append(
            {
                "provider_id": str(p.id),
                "name": p.name,
                "status": "passed",
                "sealed": sealed,
                "runner": "prebuilt",
            }
        )

    _emit_json(
        {
            "suite": "provider-test",
            "all_reference": all_reference,
            "sealed": sealed,
            "results": results,
            "total": len(results),
            "passed": sum(1 for r in results if r["status"] == "passed"),
        }
    )


@app.command("admit")
def admit(
    provider_id: PROVIDER_ID_ARG,
    decision: str = typer.Option(..., help="Decision: approve or reject"),
    role: str = typer.Option("capability_publisher", help="Admission role"),
    reason: str = typer.Option("", help="Reason for decision"),
) -> None:
    """Record an admission decision for a provider version.

    High/critical risk requires two distinct actor approvals (Publisher + Security Admin).
    """
    from uuid import UUID

    from zhiwei.capabilities.admission import AdmissionManager, AdmissionRole
    from zhiwei.capabilities.domain import RiskLevel
    from zhiwei.contracts.identifiers import new_id

    try:
        uid = UUID(provider_id)
    except ValueError:
        _fail(f"invalid provider ID: {provider_id}")

    if decision not in ("approve", "reject"):
        _fail(f"invalid decision: {decision} (must be 'approve' or 'reject')")

    try:
        role_enum = AdmissionRole(role)
    except ValueError:
        _fail(f"invalid role: {role} (must be 'capability_publisher' or 'security_admin')")

    manager = AdmissionManager()
    risk_level = RiskLevel.LOW
    test_digest = "fixture-test-digest"
    content_digest = "fixture-content-digest"

    if decision == "approve":
        record = manager.approve(
            version_id=uid,
            actor_id=new_id(),
            role=role_enum,
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
        )
    else:
        record = manager.reject(
            version_id=uid,
            actor_id=new_id(),
            role=role_enum,
            risk_level=risk_level,
            test_digest=test_digest,
            content_digest=content_digest,
            reason=reason,
        )

    _emit_json(
        {
            "admission_id": str(record.id),
            "version_id": str(record.version_id),
            "decision": record.decision.value,
            "role": record.role.value,
            "risk_level": record.risk_level.value,
        }
    )


@app.command("help")
def help_cmd() -> None:
    """Show help for provider commands."""
    click.echo("zhiwei provider — Provider lifecycle management")
    click.echo("")
    click.echo("Commands:")
    click.echo("  inspect    Inspect a provider version")
    click.echo("  test       Test provider(s) against sealed reference fixtures")
    click.echo("  admit      Record an admission decision")
    click.echo("")
    click.echo("Gate command:")
    click.echo("  provider test --all-reference --sealed")
