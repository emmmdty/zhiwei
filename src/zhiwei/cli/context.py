"""`zhiwei verify context` CLI command.

Verifies context manifests against wire captures and context state digests.
Supports both valid and tampered fixtures.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

import click
import typer

from zhiwei.cli.evidence import verify_evidence
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

# S6：`verify evidence` 与 `verify context` 同属 verify 命令组（specs/s6 §7 Gate 命令面）。
app.command("evidence", help="对 Evidence bundle 分层验证（稳定退出码 0/2-7，spec s6 §3）")(verify_evidence)

OUTPUT_FORMAT = Annotated[Literal["text", "json"], typer.Option("--format", help="输出格式")]

# spec s3 §6 Gate 的「全部验证场景」：valid + 五类篡改 + transition。
# --all 按此顺序逐个执行；顺序稳定使 text 汇总与 Gate 产物可逐字节比对。
ALL_SCENARIOS: tuple[str, ...] = (
    "valid",
    "tampered-ir",
    "tampered-body",
    "tampered-inventory",
    "tampered-profile",
    "send-after-capture",
    "transition",
)


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
    ctx: typer.Context,
    output_format: OUTPUT_FORMAT = "json",
    scenario: Annotated[
        Literal["valid", "tampered-ir", "tampered-body", "tampered-inventory",
                "tampered-profile", "send-after-capture", "transition"],
        typer.Option("--scenario", help="验证场景"),
    ] = "valid",
    run_all: Annotated[
        bool,
        typer.Option("--all", help="依次执行全部验证场景并汇总：全部 PASS 才 exit 0"),
    ] = False,
) -> None:
    """验证上下文清单完整性：valid 检查全部通过，tampered-* 检测篡改。"""
    if run_all and _scenario_explicitly_given(ctx):
        # fail closed：--all 是聚合语义，与显式限定单一场景并存属于歧义输入，
        # 静默取其一会让「到底验证了什么」无法从命令行回答。
        click.echo("--all 与显式 --scenario 互斥，请二选一", err=True)
        raise typer.Exit(1)

    if run_all:
        _run_all_scenarios(output_format)
        return

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


def _scenario_explicitly_given(ctx: typer.Context) -> bool:
    """区分「用户显式传了 --scenario」与「参数默认值」：默认 valid 与 --all 并存是合法的。

    按 enum name 比较而非身份比较：typer 内嵌了自己的 click fork，其 ParameterSource
    与 click 包的同名枚举不是同一类型，身份比较永远为 False。
    """
    source = ctx.get_parameter_source("scenario")
    return source is not None and source.name == "COMMANDLINE"


def _failed_scenario_result(scenario: str, exc: Exception) -> VerificationResult:
    """把场景执行异常折叠成 FAIL 结果，保证 --all 聚合时 7 个场景都有汇总条目。"""
    return VerificationResult(
        ok=False,
        checks=[{"id": "scenario_execution", "ok": False, "detail": str(exc)}],
        manifest_id=None,
    )


def _run_all_scenarios(output_format: str) -> None:
    """逐场景执行并汇总；单场景异常不中断聚合，否则失败面无法完整呈现。"""
    results: list[tuple[str, VerificationResult]] = []
    for name in ALL_SCENARIOS:
        try:
            results.append((name, _run_scenario(name)))
        except Exception as exc:
            results.append((name, _failed_scenario_result(name, exc)))

    all_ok = all(result.ok for _, result in results)
    if output_format == "json":
        click.echo(json.dumps({
            "ok": all_ok,
            "scenarios": [
                {
                    "scenario": name,
                    "ok": result.ok,
                    "checks": len(result.checks),
                    "checks_failed": sum(1 for c in result.checks if not c["ok"]),
                }
                for name, result in results
            ],
        }, ensure_ascii=False, indent=2))
    else:
        passed = sum(1 for _, result in results if result.ok)
        lines = [f"scenarios: {passed}/{len(results)} passed"]
        for name, result in results:
            lines.append(f"  {name}: {'PASS' if result.ok else 'FAIL'}")
        click.echo("\n".join(lines))

    if not all_ok:
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
