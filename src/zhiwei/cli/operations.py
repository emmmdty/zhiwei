"""`zhiwei backup` / `zhiwei restore` 命令组（S11-T4，specs/s11 §4）。

约定：一行可读错误 + 非零退出码，不抛栈、不回显凭据。退出码（§5 码表）：
0 成功 / 1 校验失败或执行失败 / 2 用法错误。

备份目录本身按 secret 处置（keyring 恢复材料原文在内）；stdout 默认 JSON、
诊断走 stderr。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Literal

import click
import typer

from zhiwei.config.settings import DeploymentProfile

backup_app = typer.Typer(
    help="备份创建与校验（specs/s11 §4）", no_args_is_help=True, pretty_exceptions_enable=False
)
restore_app = typer.Typer(
    help="隔离恢复校验（specs/s11 §4）", no_args_is_help=True, pretty_exceptions_enable=False
)
ops_app = typer.Typer(
    help="故障注入与固定负载（specs/s11 §5）", no_args_is_help=True, pretty_exceptions_enable=False
)

OUTPUT_FORMAT = Annotated[Literal["text", "json"], typer.Option("--format", help="输出格式")]


def _default_object_store() -> Path | None:
    from zhiwei.config.settings import load_settings

    try:
        return load_settings().object_store_root
    except ValueError:
        return None


def _default_keyring() -> Path | None:
    from zhiwei.config.settings import load_settings

    try:
        return load_settings().identity_master_key_file
    except ValueError:
        return None


def _emit(payload: dict, output_format: str) -> None:
    if output_format == "json":
        click.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for key, value in payload.items():
            click.echo(f"{key}: {value}")


@backup_app.command("create")
def backup_create(
    output: Annotated[Path, typer.Option("--output", help="备份输出目录")],
    profile: Annotated[str, typer.Option("--profile", help="部署档位")] = "local_product",
    output_format: OUTPUT_FORMAT = "text",
) -> None:
    """创建 backup manifest + 事实源组件备份。"""
    if profile not in (DeploymentProfile.LOCAL_PRODUCT.value,
                       DeploymentProfile.PRODUCTION_REFERENCE.value):
        click.echo(f"未知 profile: {profile}（local_product / production_reference）", err=True)
        raise typer.Exit(2)
    from zhiwei.operations.backup import create_backup

    try:
        manifest = create_backup(
            output_dir=output,
            profile=profile,
            object_store_root=_default_object_store(),
            keyring_file=_default_keyring(),
        )
    except RuntimeError as exc:
        click.echo(f"backup create 失败: {exc}", err=True)
        raise typer.Exit(1) from None
    _emit({"status": "created", "output": str(output), "manifest_digest": manifest.manifest_digest},
          output_format)


@backup_app.command("verify")
def backup_verify(
    backup_dir: Annotated[Path, typer.Argument(help="备份目录")],
    output_format: OUTPUT_FORMAT = "text",
) -> None:
    """逐组件重算 digest，校验 manifest（fail closed）。"""
    from zhiwei.operations.backup import verify_backup

    try:
        manifest = verify_backup(backup_dir)
    except (RuntimeError, ValueError) as exc:
        click.echo(f"backup verify 失败: {exc}", err=True)
        raise typer.Exit(1) from None
    _emit({"status": "verified", "manifest_digest": manifest.manifest_digest}, output_format)


@restore_app.command("verify")
def restore_verify(
    backup_dir: Annotated[Path, typer.Argument(help="备份目录")],
    isolated: Annotated[bool, typer.Option("--isolated", help="强制隔离恢复（spec §4 硬要求）")] = False,
    output_format: OUTPUT_FORMAT = "text",
) -> None:
    """隔离恢复 + 八项校验（docs/operations/backup-restore.md §3）。"""
    if not isolated:
        click.echo("restore verify 只支持隔离恢复：显式传 --isolated（spec §4）", err=True)
        raise typer.Exit(2)
    from zhiwei.operations.restore import restore_verify

    try:
        report = restore_verify(backup_dir, isolated=True, opensearch_endpoint=_opensearch_endpoint())
    except (RuntimeError, ValueError) as exc:
        click.echo(f"restore verify 失败: {exc}", err=True)
        raise typer.Exit(1) from None
    _emit({"status": "verified", **report.model_dump()}, output_format)


def _opensearch_endpoint() -> str | None:
    """local-product 参考搜索面；端口不可达时返回 None（restore verify 对 None
    硬失败——search rebuild 是 §3.5 必做校验，不得静默跳过）。"""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 9201), timeout=1):
            return "http://127.0.0.1:9201"
    except OSError:
        return None


def _fault_profile_ok(profile: str) -> bool:
    return profile in ("local-product", "production-reference")


@ops_app.command("fault-run")
def fault_run(
    profile: Annotated[str, typer.Option("--profile", help="部署档位")] = "local-product",
    fixture: Annotated[bool, typer.Option("--fixture", help="只跑 fixture backend（确定性、无 docker）")] = False,
    scenario: Annotated[str | None, typer.Option("--scenario", help="只跑指定场景")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="列出将执行的场景，不注入故障")] = False,
    sealed: Annotated[bool, typer.Option("--sealed", help="写 seal 产物（raw events/digests/恢复时间）")] = False,
    seal_dir: Annotated[Path | None, typer.Option("--seal-dir", help="seal 输出目录")] = None,
    compose_file: Annotated[str | None, typer.Option("--compose-file", help="compose 文件路径覆盖")] = None,
    output_format: OUTPUT_FORMAT = "text",
) -> None:
    """故障场景执行器；退出码语义见 docs/API.md §12.1（0/1/2/3）。"""
    from zhiwei.operations.faults import SCENARIO_REGISTRY, run_scenario

    if not _fault_profile_ok(profile):
        click.echo(f"未知 profile: {profile}（local-product / production-reference）", err=True)
        raise typer.Exit(2)

    backend = "fixture" if fixture else "compose"
    selected = list(SCENARIO_REGISTRY)
    if scenario is not None:
        if scenario not in SCENARIO_REGISTRY:
            click.echo(f"未知 scenario: {scenario}", err=True)
            raise typer.Exit(2)
        selected = [scenario]
    matching = [sid for sid in selected if SCENARIO_REGISTRY[sid].backend == backend]
    if dry_run:
        _emit({"status": "dry-run", "backend": backend,
               "scenarios": matching}, output_format)
        return

    seal_path = seal_dir if sealed else None
    if backend == "compose" and compose_file is not None:
        # compose 文件覆盖用于环境不可用判定（测试注入）
        import pathlib as _pathlib

        fake = _pathlib.Path(compose_file)
        if not fake.is_file():
            click.echo("compose 栈未就绪（环境不可用）", err=True)
            raise typer.Exit(3)

    failures: list[str] = []
    results: list[dict] = []
    for sid in matching:
        try:
            result = run_scenario(sid, backend=backend, seal_dir=seal_path)
        except KeyError as exc:
            click.echo(f"fault-run 用法错误: {exc}", err=True)
            raise typer.Exit(2) from None
        except RuntimeError as exc:
            click.echo(f"环境不可用: {exc}", err=True)
            raise typer.Exit(3) from None
        results.append(result.model_dump())
        if not result.passed:
            failures.append(f"{sid}: {result.failure_reason or 'failed'}")
    if failures:
        click.echo("\n".join(failures), err=True)
        raise typer.Exit(1)
    _emit({"status": "passed", "backend": backend,
           "sealed": bool(sealed), "results": results}, output_format)


def _load_profile_ok(profile: str) -> bool:
    return profile in ("local_product", "production_reference")


@ops_app.command("load-run")
def load_run(
    workload: Annotated[list[str] | None, typer.Option("--workload", help="workload（可重复）")] = None,
    profile: Annotated[str, typer.Option("--profile", help="部署档位")] = "local_product",
    concurrency: Annotated[int, typer.Option("--concurrency", help="并发档位")] = 2,
    max_runs: Annotated[int, typer.Option("--max-runs", help="每 workload 运行次数上界")] = 5,
    ramp: Annotated[str | None, typer.Option("--ramp", help="逗号分隔并发爬坡档位")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="列出将执行的 workload，不执行")] = False,
    sealed: Annotated[bool, typer.Option("--sealed", help="写 seal 产物")] = False,
    seal_dir: Annotated[Path | None, typer.Option("--seal-dir", help="seal 输出目录")] = None,
    output_format: OUTPUT_FORMAT = "text",
) -> None:
    """固定负载 runner；退出码语义见 docs/API.md §12.1（0/1/2/3）。"""
    from zhiwei.operations.load import LOAD_WORKLOADS, run_load

    if not _load_profile_ok(profile):
        click.echo(f"未知 profile: {profile}（local_product / production_reference）", err=True)
        raise typer.Exit(2)
    selected = list(workload) if workload else ["ask"]
    unknown = set(selected) - set(LOAD_WORKLOADS)
    if unknown:
        click.echo(f"未知 workload: {sorted(unknown)}", err=True)
        raise typer.Exit(2)
    if dry_run:
        _emit({"status": "dry-run", "profile": profile,
               "workloads": sorted(LOAD_WORKLOADS), "concurrency": concurrency,
               "max_runs": max_runs}, output_format)
        return
    levels = [int(x) for x in ramp.split(",")] if ramp else None
    try:
        report = run_load(
            workloads=selected, concurrency=concurrency, max_runs=max_runs,
            profile=profile, seal_dir=seal_dir if sealed else None, ramp=levels,
        )
    except KeyError as exc:
        click.echo(f"load-run 用法错误: {exc}", err=True)
        raise typer.Exit(2) from None
    except OSError as exc:
        click.echo(f"环境不可用: {exc}", err=True)
        raise typer.Exit(3) from None
    if report.status != "passed":
        raise typer.Exit(1)
    _emit({"status": "passed", "p50_ms": report.p50_ms, "p95_ms": report.p95_ms,
           "errors": report.errors, "sealed": bool(sealed)}, output_format)


def _upgrade_profile_ok(profile: str) -> bool:
    return profile in ("local_product", "production_reference")


@ops_app.command("upgrade-run")
def upgrade_run_cmd(
    previous_revision: Annotated[str, typer.Option("--from", help="当前（升级前）revision 全名")],
    target_revision: Annotated[str, typer.Option("--to", help="目标 revision 全名")],
    worker_build_id: Annotated[str | None, typer.Option("--build-id", help="worker build id（缺省取环境）")] = None,
    temporal_target: Annotated[str | None, typer.Option("--temporal-target", help="preflight 探活的 Temporal 地址")] = None,
    claims_snapshot_digest: Annotated[str | None, typer.Option("--claims-digest", help="升级前 claims 快照 digest（§2.7，缺省不校验）")] = None,
    checkpoint: Annotated[bool, typer.Option("--checkpoint", help="显式确认 destructive contract 阶段")] = False,
    contract_phase: Annotated[bool, typer.Option("--contract", help="执行 contract 阶段（需 --checkpoint）")] = False,
    output_format: OUTPUT_FORMAT = "text",
) -> None:
    """三段式升级编排（docs/operations/upgrade.md §2.6）；退出码 0/1/2/3 同 §12.1。"""
    from zhiwei.operations.upgrade import UpgradeManifest, upgrade_run

    if not _upgrade_profile_ok("local_product"):
        raise typer.Exit(2)
    manifest = UpgradeManifest(
        previous_revision=previous_revision,
        target_revision=target_revision,
        worker_build_id=worker_build_id or os.environ.get("ZHIWEI_WORKER_BUILD_ID") or "dev-local",
        requires_checkpoint=True,
        opensearch_rebuild=False,
        temporal_target=temporal_target,
        claims_snapshot_digest=claims_snapshot_digest,
    )
    try:
        result = upgrade_run(
            manifest,
            dsn=_require_db_dsn(),
            checkpoint=checkpoint,
            contract_phase=contract_phase,
            worker_build_id=manifest.worker_build_id,
        )
    except RuntimeError as exc:
        click.echo(f"upgrade-run 失败: {exc}", err=True)
        raise typer.Exit(1) from None
    except PermissionError as exc:
        click.echo(f"upgrade-run 拒绝: {exc}", err=True)
        raise typer.Exit(2) from None
    _emit({"status": result.stopped_at, "revisions_applied": result.revisions_applied},
          output_format)


def _require_db_dsn() -> str:
    dsn = os.environ.get("ZHIWEI_DATABASE_URL")
    if not dsn:
        click.echo("ZHIWEI_DATABASE_URL 未配置", err=True)
        raise typer.Exit(3)
    return dsn
