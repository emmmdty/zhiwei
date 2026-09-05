"""`zhiwei release` 命令组：strict release checker 与出处 attestation（S9 §5/§7）。

约定与 cli/db.py 一致：一行可读错误 + 非零退出码，不抛栈；DSN 凭据脱敏复用
db.py 的实现（凭据脱敏逻辑单一来源）。claim registry 经 maintenance DSN 系统
级读取——release 表面声明的是平台公开口径，checker 必须看到全部租户的 claim。
dry-run 只构建 draft，永不签名/写文件；--sign 要求显式 key file（operator 动作，
不接受环境默认密钥）。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Annotated, Any, NoReturn

import click
import typer
from sqlalchemy import select

from zhiwei.agents.claims import ClaimEvidence, ClaimRecord, ClaimScope, ClaimStatus
from zhiwei.cli.db import _describe_error
from zhiwei.config.settings import Settings, load_settings
from zhiwei.contracts.canonical import canonical_json
from zhiwei.contracts.time import utc_now
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import ClaimRegistryRow
from zhiwei.release.attestation import build_attestation_draft, sign_attestation, verify_attestation
from zhiwei.release.checker import scan_release_surface

app = typer.Typer(
    help="Release 声明检查与出处 attestation",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)

DEFAULT_SURFACE_PATHS = (Path("README.md"), Path("docs"))
# attest 覆盖面是确定性的：README + 顶层 docs + artifacts 全树。`artifacts/**`
# 在 py3.11 只匹配目录，必须再落一层 `/*` 才能覆盖全树文件。
DEFAULT_ATTEST_GLOBS = ("README.md", "docs/*.md", "artifacts/**/*")
ATTEST_GENERATOR = "zhiwei-release-check"


def _load_settings() -> Settings:
    try:
        return load_settings()
    except ValueError as exc:
        click.echo(f"配置错误: {exc}", err=True)
        raise typer.Exit(1) from None


def _fail(message: str) -> NoReturn:
    click.echo(message, err=True)
    raise typer.Exit(1)


def _emit_json(payload: dict[str, Any]) -> None:
    # stdout 必须是纯 JSON（供 Gate 脚本消费）；诊断信息走 stderr。
    click.echo(json.dumps(payload, ensure_ascii=False))


def _load_registry(raw_dsn: str) -> dict[str, ClaimRecord]:
    """经 maintenance DSN 系统级读取 claim registry 并投影为域记录。"""

    async def _read() -> dict[str, ClaimRecord]:
        sessions = create_session_factory(create_database_engine(raw_dsn))
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(ClaimRegistryRow).order_by(
                        ClaimRegistryRow.organization_id,
                        ClaimRegistryRow.workspace_id,
                        ClaimRegistryRow.claim_id,
                    )
                )
            ).all()
            return _registry_from_rows(rows)

    try:
        return asyncio.run(_read())
    except Exception as exc:
        _fail(f"无法读取 claim registry: {_describe_error(exc, raw_dsn)}")


def _registry_from_rows(rows: Sequence[ClaimRegistryRow]) -> dict[str, ClaimRecord]:
    registry: dict[str, ClaimRecord] = {}
    for row in rows:
        record = _record(row)
        existing = registry.get(record.claim_id)
        if existing is not None and existing != record:
            # 同一 claim id 在多个租户作用域下内容冲突：release 表面无法确定
            # 绑定谁，fail closed 而不是随机取一个。
            raise ValueError(
                f"claim id {record.claim_id!r} has conflicting records across tenant scopes"
            )
        registry[record.claim_id] = record
    return registry


def _record(row: ClaimRegistryRow) -> ClaimRecord:
    return ClaimRecord(
        claim_id=row.claim_id,
        statement=row.statement,
        scope=ClaimScope.model_validate(row.scope),
        status=ClaimStatus(row.status),
        evidence=(
            ClaimEvidence.model_validate(row.evidence) if row.evidence is not None else None
        ),
        bound_value=row.bound_value,
    )


def _read_surface(entries: Sequence[Path]) -> dict[str, str]:
    files: dict[str, str] = {}
    for entry in entries:
        path = Path(entry)
        try:
            if path.is_file():
                files[path.as_posix()] = path.read_text(encoding="utf-8")
            elif path.is_dir():
                for candidate in sorted(path.rglob("*.md")):
                    if candidate.is_file():
                        files[candidate.as_posix()] = candidate.read_text(encoding="utf-8")
            else:
                _fail(f"release 表面路径不存在: {path.as_posix()}")
        except (OSError, UnicodeDecodeError) as exc:
            _fail(f"无法读取 release 表面 {path.as_posix()}: {exc}")
    return files


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
        )
    except OSError:
        _fail("无法确定 commit（git 不可用）：请显式传 --commit")
    if completed.returncode != 0:
        _fail("无法确定 commit（git rev-parse 失败）：请显式传 --commit")
    return completed.stdout.strip()


def _iso_date_or_fail(value: str, option: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(f"{option} 不是 ISO-8601 日期（YYYY-MM-DD）: {value}")
    return value


@app.command("check")
def check(
    strict: Annotated[bool, typer.Option("--strict", help="存在任何 finding 即退出 1")] = False,
    paths: Annotated[
        list[Path] | None,
        typer.Option("--paths", help="release 表面文件或目录（默认 README.md 与 docs）"),
    ] = None,
    stale_after_days: Annotated[
        int, typer.Option("--stale-after-days", min=0, help="claim 口径日期过期窗口（天）")
    ] = 180,
    db_dsn: Annotated[
        str | None, typer.Option("--db-dsn", help="maintenance DSN（默认取 ZHIWEI_DATABASE_URL）")
    ] = None,
    now: Annotated[
        str | None, typer.Option("--now", help="过期判定基准日期 ISO-8601（默认今天）")
    ] = None,
) -> None:
    """扫描 release 表面声明块：无 artifact 支撑的数字 / fixture-live 混写 / 过期 claim。"""
    surface = tuple(paths) if paths else DEFAULT_SURFACE_PATHS
    files = _read_surface(surface)
    raw_dsn = db_dsn
    if raw_dsn is None:
        settings = _load_settings()
        if settings.database_url is None:
            _fail("claim registry 不可用：未提供 --db-dsn 且 ZHIWEI_DATABASE_URL 未配置")
        raw_dsn = settings.database_url.get_secret_value()
    registry = _load_registry(raw_dsn)
    today = _iso_date_or_fail(
        now if now is not None else date.today().isoformat(), "--now"
    )
    findings = scan_release_surface(
        files, registry, now=today, stale_after_days=stale_after_days
    )
    _emit_json(
        {
            "checked_files": len(files),
            "findings": [finding.model_dump(mode="json") for finding in findings],
        }
    )
    if strict and findings:
        raise typer.Exit(1)


@app.command("attest")
def attest(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--sign", help="dry-run 只构建 draft 并打印；--sign 需要密钥文件"),
    ] = True,
    key_file: Annotated[
        Path | None,
        typer.Option("--key-file", help="HMAC 签名密钥文件（operator 显式动作，不经环境注入）"),
    ] = None,
    output: Annotated[
        Path | None, typer.Option("--output", help="签名 attestation JSON 输出路径（仅 --sign）")
    ] = None,
    commit: Annotated[
        str | None, typer.Option("--commit", help="仓库 commit（默认 git rev-parse HEAD）")
    ] = None,
    generated_at: Annotated[
        str | None, typer.Option("--generated-at", help="ISO-8601 生成时间（默认当前 UTC 时间）")
    ] = None,
) -> None:
    """构建 release 表面出处 attestation；dry-run 永不签名/发布/写文件。"""
    if dry_run:
        if key_file is not None:
            _fail("--key-file 仅在 --sign 模式下有效（dry-run 不接触密钥材料）")
        if output is not None:
            _fail("--output 仅在 --sign 模式下有效（dry-run 不写任何文件）")

    draft = build_attestation_draft(
        Path("."),
        DEFAULT_ATTEST_GLOBS,
        commit=commit if commit is not None else _git_commit(),
        generated_at=generated_at if generated_at is not None else utc_now().isoformat(),
        generator=ATTEST_GENERATOR,
    )
    if not draft.content_digests:
        _fail("attestation 覆盖面为空：表面文件缺失（README.md / docs / artifacts）")

    if dry_run:
        _emit_json({"signed": draft.signed, **draft.canonical_mapping()})
        return

    if key_file is None:
        _fail("--sign 需要显式 --key-file（operator 动作，不使用默认或环境密钥）")
    if output is None:
        _fail("--sign 需要显式 --output 路径")
    if not key_file.is_file():
        _fail(f"签名密钥文件不存在: {key_file.as_posix()}")

    try:
        key = key_file.read_bytes()
        signed = sign_attestation(draft, key)
        # 写出前用同一密钥自验一次：保证 Gate 侧 verify 不会因实现漂移而失败。
        verify_attestation(signed, key)
        output.write_bytes(canonical_json(signed.canonical_mapping()))
    except OSError as exc:
        _fail(f"签名密钥或输出文件读写失败: {exc}")
    _emit_json(
        {
            "signed": signed.signed,
            "output": output.as_posix(),
            "content_digests": len(signed.content_digests),
            "signature": signed.signature,
        }
    )
