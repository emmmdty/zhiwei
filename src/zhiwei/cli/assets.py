"""`zhiwei assets` 命令组：冻结基准资产的 lock 校验。

`assets lock` 默认等价 `--check`：只读比较，漂移时非零退出且不写 lock；只有显式 `--write`
才更新 `CHECKSUMS.sha256`。二者互斥。扫描范围沿用 Makefile 的
`evals/{novels,questions,risk,knowledge}`，排除 lock 自身。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import click
import typer

app = typer.Typer(
    help="冻结基准资产与校验和 lock", no_args_is_help=True, pretty_exceptions_enable=False
)

_ART_DIRS = ("novels", "questions", "risk", "knowledge", "change-brief")


def _scan_artifacts(evals_dir: Path) -> dict[str, str]:
    """返回 {相对 posix 路径: sha256 十六进制}；目录稳定排序保证输出可复算。

    路径以仓库根为基准（与 `make checksums` 的 `find evals/...` 输出一致），
    这样 lock 内容与冻结资产生成规则逐字可比。
    """
    artifacts: dict[str, str] = {}
    for directory in _ART_DIRS:
        base = evals_dir / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(evals_dir.parent).as_posix()
            artifacts[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return artifacts


def _read_lock(checksum_path: Path) -> dict[str, str]:
    """解析 CHECKSUMS.sha256；空行忽略，非标准行直接视为漂移来源。"""
    lines = checksum_path.read_text(encoding="utf-8").splitlines()
    entries: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        digest_hex, path = line.split(maxsplit=1)
        entries[path] = digest_hex
    return entries


def _write_lock(checksum_path: Path, artifacts: dict[str, str]) -> None:
    lines = [f"{artifacts[path]}  {path}" for path in sorted(artifacts)]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.command("lock")
def lock(
    check: bool = typer.Option(False, "--check", help="只读校验 lock，不写任何文件"),
    write: bool = typer.Option(False, "--write", help="显式重写 CHECKSUMS.sha256"),
) -> None:
    """校验或重写冻结资产的 CHECKSUMS.sha256。默认等价 --check。"""
    if check and write:
        click.echo("--check 与 --write 互斥：一次只允许一种模式", err=True)
        raise typer.Exit(2)

    evals_dir = Path.cwd() / "evals"
    checksum_path = evals_dir / "CHECKSUMS.sha256"
    if not evals_dir.is_dir():
        click.echo(f"assets lock: 当前目录下没有 evals/ 冻结资产目录: {evals_dir}", err=True)
        raise typer.Exit(1)

    if write:
        artifacts = _scan_artifacts(evals_dir)
        _write_lock(checksum_path, artifacts)
        click.echo(f"write: 已重写 CHECKSUMS.sha256（{len(artifacts)} 个资产）")
        return

    artifacts = _scan_artifacts(evals_dir)
    if not checksum_path.is_file():
        click.echo("check: 缺少 CHECKSUMS.sha256，请先用 --write 生成", err=True)
        raise typer.Exit(1)
    locked = _read_lock(checksum_path)

    drift: list[str] = []
    for path in sorted(set(artifacts) | set(locked)):
        actual = artifacts.get(path)
        expected = locked.get(path)
        if actual != expected:
            if actual is None:
                drift.append(f"{path}（lock 中存在但资产已缺失）")
            elif expected is None:
                drift.append(f"{path}（资产未登记进 lock）")
            else:
                drift.append(f"{path}（digest 不一致）")

    if drift:
        click.echo("check: 资产与 CHECKSUMS.sha256 漂移，且未写入任何文件:")
        for entry in drift:
            click.echo(f"  - {entry}")
        raise typer.Exit(1)
    click.echo(f"check: {len(artifacts)} 个资产与 CHECKSUMS.sha256 一致")
