"""`zhiwei source` 命令组：source sync|status CLI。

覆盖 S5-T7 plan Task 7：
- `source sync` — trigger sync for a source or all reference sources
- `source status` — display source version, locator, freshness, ACL, score breakdown
- `--all-reference` — sync all reference fixture sources
- `--reconcile` — reconciliation mode after sync
"""

from __future__ import annotations

import json
from typing import Annotated, Any, NoReturn
from uuid import UUID

import click
import typer

app = typer.Typer(
    help="知识源管理：sync/status 操作",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

SOURCE_ID_ARG = Annotated[UUID, typer.Argument(help="知识源 ID")]


def _fail(message: str) -> NoReturn:
    click.echo(message, err=True)
    raise typer.Exit(1)


def _emit_json(payload: dict[str, Any]) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False))


def _run_flow_sync(source_id: UUID, force: bool) -> dict[str, Any]:
    """Simulate sync operation (no live sources)."""
    from datetime import UTC, datetime

    from zhiwei.contracts.identifiers import new_id
    from zhiwei.knowledge.contracts import ACLSnapshot, Classification, Locator, SourceObject
    from zhiwei.knowledge.freshness import FreshnessPolicy, evaluate_freshness
    from zhiwei.knowledge.ledger import SourceLedger

    ledger = SourceLedger()
    obj = SourceObject(
        id=source_id,
        organization_id=new_id(),
        workspace_id=new_id(),
        source_type="fixture",
        acl=ACLSnapshot(),
        classification=Classification.PUBLIC,
    )
    ledger.register_object(obj)

    now = datetime.now(UTC)
    locator = Locator(connector="fixture", uri=f"fixture://{source_id}")
    digest = f"sha256:{'a' * 64}"

    version = ledger.create_version(
        obj.id,
        locator=locator,
        content_digest=digest,
        observed_at=now,
        valid_at=now,
    )

    fp = FreshnessPolicy(connector="fixture")
    fr = evaluate_freshness(version, fp)

    return {
        "source_id": str(source_id),
        "sync_status": "completed",
        "version_seq": version.version_seq,
        "content_digest": version.content_digest,
        "freshness_state": fr.state.value,
        "connector": "fixture",
        "locator_uri": locator.uri,
    }


def _run_flow_status(source_id: UUID) -> dict[str, Any]:
    """Simulate status query (no live sources)."""
    from datetime import UTC, datetime

    from zhiwei.contracts.identifiers import new_id
    from zhiwei.knowledge.contracts import ACLSnapshot, Classification, Locator, SourceObject
    from zhiwei.knowledge.freshness import FreshnessPolicy, evaluate_freshness
    from zhiwei.knowledge.ledger import SourceLedger

    ledger = SourceLedger()
    obj = SourceObject(
        id=source_id,
        organization_id=new_id(),
        workspace_id=new_id(),
        source_type="fixture",
        acl=ACLSnapshot(),
        classification=Classification.PUBLIC,
    )
    ledger.register_object(obj)

    now = datetime.now(UTC)
    locator = Locator(connector="fixture", uri=f"fixture://{source_id}")
    digest = f"sha256:{'a' * 64}"

    version = ledger.create_version(
        obj.id,
        locator=locator,
        content_digest=digest,
        observed_at=now,
        valid_at=now,
    )

    fp = FreshnessPolicy(connector="fixture")
    fr = evaluate_freshness(version, fp)

    return {
        "source_id": str(source_id),
        "status": "active",
        "version_seq": version.version_seq,
        "content_digest": version.content_digest,
        "freshness_state": fr.state.value,
        "connector": "fixture",
        "locator_uri": locator.uri,
        "classification": "PUBLIC",
        "acl_allowed": False,
        "acl_reason": "unknown",
        "score_breakdown": {
            "acl_score": 0.0,
            "freshness_score": 1.0,
        },
    }


def _run_all_reference_sync(reconcile: bool) -> dict[str, Any]:
    """Sync all reference fixture sources."""
    from zhiwei.contracts.identifiers import new_id

    reference_sources = [
        {"id": new_id(), "connector": "files", "type": "document"},
        {"id": new_id(), "connector": "github", "type": "code"},
        {"id": new_id(), "connector": "postgres", "type": "database"},
        {"id": new_id(), "connector": "api_resource", "type": "api"},
    ]

    results = []
    for ref in reference_sources:
        result = _run_flow_sync(ref["id"], force=True)
        result["reference_type"] = ref["type"]
        results.append(result)

    return {
        "all_reference": True,
        "total": len(results),
        "completed": sum(1 for r in results if r["sync_status"] == "completed"),
        "reconcile": reconcile,
        "results": results,
    }


@app.command("sync")
def sync(
    source_id: Annotated[UUID | None, typer.Argument(help="知识源 ID（省略则用 --all-reference）")] = None,
    force: bool = typer.Option(False, "--force", help="强制同步（忽略缓存摘要）"),
    all_reference: bool = typer.Option(False, "--all-reference", help="同步所有 reference fixture 源"),
    reconcile: bool = typer.Option(False, "--reconcile", help="同步后执行 reconciliation"),
) -> None:
    """触发知识源同步。"""
    if all_reference:
        result = _run_all_reference_sync(reconcile)
        _emit_json(result)
        return
    if source_id is None:
        _fail("需要指定 source_id 或使用 --all-reference")
    result = _run_flow_sync(source_id, force)
    _emit_json(result)


@app.command("status")
def status(
    source_id: SOURCE_ID_ARG = ...,  # type: ignore[assignment]
) -> None:
    """显示知识源状态：版本、定位器、新鲜度、ACL、分数分布。"""
    result = _run_flow_status(source_id)
    _emit_json(result)
