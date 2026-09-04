"""S3-T7: `zhiwei models` 命令组：模型 profiles 的 fixture attestation 与诊断。

fixture 模式下执行 offline schema 验证；live 模式需要 operator 显式 preflight。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

import click
import typer

from zhiwei.models.attestations import AttestationRegistry
from zhiwei.models.probes import run_fixture_attestations
from zhiwei.models.profiles import load_model_profiles

app = typer.Typer(help="模型 profiles 与 fixture attestation", no_args_is_help=True, pretty_exceptions_enable=False)

OUTPUT_FORMAT = Annotated[Literal["text", "json"], typer.Option("--format", help="输出格式")]

# Default profiles path — relative to project root, resolved at runtime.
_DEFAULT_PROFILES_PATH = Path("config/models/opencode-go-profiles.yaml")


def _resolve_profiles_path() -> Path:
    """Resolve the model profiles path relative to project root."""
    # Walk up from CWD to find config/models/opencode-go-profiles.yaml
    candidate = Path.cwd()
    for _ in range(10):
        path = candidate / "config" / "models" / "opencode-go-profiles.yaml"
        if path.is_file():
            return path
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return _DEFAULT_PROFILES_PATH


def _format_text_result(
    attestations: list,
    total_profiles: int,
) -> str:
    """Format attestation results as human-readable text."""
    lines: list[str] = []
    lines.append(f"fixture attestation: {len(attestations)}/{total_profiles} profiles qualified")
    for att in attestations:
        status_icon = "ok" if att.status.value == "valid" else att.status.value
        lines.append(
            f"  {att.model_name:<25s} {status_icon:<6s} "
            f"level={att.qualification_level} endpoint={att.endpoint_id}"
        )
    return "\n".join(lines)


@app.command("attest")
def attest(
    output_format: OUTPUT_FORMAT = "text",
    live: bool = typer.Option(False, "--live", help="执行 live preflight（需 operator 显式触发）"),
) -> None:
    """对所有 fixture model profiles 执行 offline schema 验证 attestation。

    fixture 模式（默认）：不做任何网络调用，仅验证 profile 与已知 fixture response schema 的结构一致性。
    live 模式（--live）：拒绝执行，除非由 operator 显式触发（当前版本不支持 live attestation）。
    """
    if live:
        click.echo("错误: live attestation 需要 operator 显式 preflight，当前版本不支持", err=True)
        raise typer.Exit(1)

    profiles_path = _resolve_profiles_path()
    if not profiles_path.is_file():
        click.echo(f"错误: 找不到 model profiles 配置: {profiles_path}", err=True)
        raise typer.Exit(1)

    try:
        profiles = load_model_profiles(profiles_path)
    except Exception as exc:
        click.echo(f"错误: 加载 model profiles 失败: {exc}", err=True)
        raise typer.Exit(1) from None

    registry = AttestationRegistry()
    attestations = run_fixture_attestations(profiles, registry)

    if output_format == "json":
        payload = {
            "fixture_attestations": [
                {
                    "id": att.id,
                    "model_name": att.model_name,
                    "endpoint_id": att.endpoint_id,
                    "qualification_level": att.qualification_level,
                    "status": att.status.value,
                    "probed_at": att.probed_at.isoformat(),
                    "valid_until": att.valid_until.isoformat(),
                    "capabilities_count": len(att.probed_capabilities),
                }
                for att in attestations
            ],
            "total_profiles": len(profiles),
            "qualified_count": len(attestations),
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        click.echo(_format_text_result(attestations, len(profiles)))

    if len(attestations) < len(profiles):
        raise typer.Exit(1)


@app.command("list")
def list_profiles(
    output_format: OUTPUT_FORMAT = "text",
) -> None:
    """列出所有已加载的 model profiles 及其 verification level。"""
    profiles_path = _resolve_profiles_path()
    if not profiles_path.is_file():
        click.echo(f"错误: 找不到 model profiles 配置: {profiles_path}", err=True)
        raise typer.Exit(1)

    try:
        profiles = load_model_profiles(profiles_path)
    except Exception as exc:
        click.echo(f"错误: 加载 model profiles 失败: {exc}", err=True)
        raise typer.Exit(1) from None

    if output_format == "json":
        payload = {
            "profiles": [
                {
                    "id": p.id,
                    "model_name": p.model_name,
                    "endpoint_id": p.endpoint_id,
                    "wire_protocol": p.wire_protocol.value,
                    "verification_level": p.verification_level,
                    "context_window": p.context_window,
                    "max_output": p.max_output,
                }
                for p in profiles.values()
            ],
            "total": len(profiles),
        }
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for _pid, p in profiles.items():
            click.echo(
                f"  {p.model_name:<25s} protocol={p.wire_protocol.value:<20s} "
                f"level={p.verification_level} ctx={p.context_window}"
            )
