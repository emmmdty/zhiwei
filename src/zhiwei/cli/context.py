"""`zhiwei verify context` CLI command.

Verifies context manifests against wire captures and context state digests.
Supports both valid and tampered fixtures.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

import click
import typer

from zhiwei.context.manifests import ContextManifest, TransitionManifest
from zhiwei.evidence.context_verify import (
    VerificationResult,
    verify_manifest_integrity,
    verify_send_after_capture_mutation,
    verify_tamper_body,
    verify_tamper_inventory,
    verify_tamper_ir,
    verify_tamper_profile,
    verify_transition_integrity,
)
from zhiwei.models.presend import WireCapture, digest_bytes

app = typer.Typer(help="验证上下文清单与线绑定完整性", no_args_is_help=True, pretty_exceptions_enable=False)

OUTPUT_FORMAT = Annotated[Literal["text", "json"], typer.Option("--format", help="输出格式")]


def _build_fixture_manifest() -> ContextManifest:
    """Build a deterministic fixture manifest for verification."""
    body = b'{"model": "test", "messages": [{"role": "user", "content": "hi"}]}'
    return ContextManifest(
        manifest_id="fixture-manifest-001",
        body_sha256=digest_bytes(body),
        body_len=len(body),
        url="http://127.0.0.1:1/v1/chat/completions",
        method="POST",
        redacted_headers={"authorization": "<redacted>", "content-type": "application/json"},
        header_names=("authorization", "content-type", "host"),
        source_inventory_digest="sha256:" + "a" * 64,
        target_profile_digest="sha256:" + "b" * 64,
        ir_digest="sha256:" + "c" * 64,
        captured_at="2026-09-01T00:00:00+00:00",
        sequence_no=0,
    )


def _build_fixture_capture() -> WireCapture:
    """Build a deterministic fixture wire capture matching the manifest."""
    body = b'{"model": "test", "messages": [{"role": "user", "content": "hi"}]}'
    return WireCapture(
        seq=0,
        method="POST",
        url="http://127.0.0.1:1/v1/chat/completions",
        body_sha256=digest_bytes(body),
        body_len=len(body),
        content_length_header=len(body),
        header_names=("authorization", "content-type", "host"),
        redacted_headers={"authorization": "<redacted>", "content-type": "application/json"},
        captured_at="2026-09-01T00:00:00+00:00",
    )


def _build_fixture_transition() -> TransitionManifest:
    """Build a deterministic fixture transition manifest."""
    return TransitionManifest(
        manifest_id="fixture-transition-001",
        before_state_digest="sha256:" + "d" * 64,
        after_state_digest="sha256:" + "e" * 64,
        transition_type="context.created",
        wire_body_digest="sha256:" + "a" * 64,
        ir_digest="sha256:" + "c" * 64,
        items_added=1,
        items_removed=0,
        items_unchanged=0,
        triggered_by_manifest_id="fixture-manifest-001",
        occurred_at="2026-09-01T00:00:00+00:00",
    )


@app.command("context")
def verify_context(
    output_format: OUTPUT_FORMAT = "json",
    scenario: Annotated[
        Literal["valid", "tampered-ir", "tampered-body", "tampered-inventory",
                "tampered-profile", "send-after-capture", "transition"],
        typer.Option("--scenario", help="验证场景"),
    ] = "valid",
) -> None:
    """验证上下文清单完整性：valid 检查全部通过，tampered-* 检测篡改。"""
    try:
        result = _run_scenario(scenario)
    except Exception as exc:
        click.echo(f"验证失败: {exc}", err=True)
        raise typer.Exit(1) from None

    if output_format == "json":
        click.echo(json.dumps({
            "scenario": scenario,
            "ok": result.ok,
            "checks": result.checks,
            "manifest_id": result.manifest_id,
            "summary": result.summary,
        }, ensure_ascii=False, indent=2))
    else:
        lines = [f"scenario: {scenario}", f"result: {'PASS' if result.ok else 'FAIL'}"]
        for c in result.checks:
            mark = "ok" if c["ok"] else "XX"
            lines.append(f"  [{mark}] {c['id']}: {c['detail']}")
        click.echo("\n".join(lines))

    if not result.ok:
        raise typer.Exit(1)


def _run_scenario(scenario: str) -> VerificationResult:
    """Dispatch to the appropriate verification scenario."""
    body = b'{"model": "test", "messages": [{"role": "user", "content": "hi"}]}'
    manifest = _build_fixture_manifest()
    capture = _build_fixture_capture()

    if scenario == "valid":
        return verify_manifest_integrity(
            manifest, capture,
            body_bytes=body,
            inventory_digest=manifest.source_inventory_digest,
            profile_digest=manifest.target_profile_digest,
            ir_digest=manifest.ir_digest,
        )
    elif scenario == "tampered-ir":
        return verify_tamper_ir(manifest, tampered_ir_digest="sha256:" + "f" * 64)
    elif scenario == "tampered-body":
        return verify_tamper_body(manifest, tampered_body=b"tampered body content")
    elif scenario == "tampered-inventory":
        return verify_tamper_inventory(manifest, tampered_inventory_digest="sha256:" + "f" * 64)
    elif scenario == "tampered-profile":
        return verify_tamper_profile(manifest, tampered_profile_digest="sha256:" + "f" * 64)
    elif scenario == "send-after-capture":
        return verify_send_after_capture_mutation(manifest, mutated_body=b"mutated body")
    elif scenario == "transition":
        transition = _build_fixture_transition()
        return verify_transition_integrity(
            transition,
            before_digest=transition.before_state_digest,
            after_digest=transition.after_state_digest,
        )
    else:
        raise ValueError(f"unknown scenario: {scenario}")
