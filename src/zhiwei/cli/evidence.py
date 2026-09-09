"""`zhiwei verify evidence` CLI 命令。

specs/s6 §3 的分层验证生产命令：对 bundle/schema/version/source/snapshot/locator/
query/result/value/claim span/digest 逐层验证，稳定退出码 0 success；2 input/schema；
3 source/snapshot；4 replay/value；5 claim/span；6 digest/artifact；7 authorization/
private boundary。

bundle 文件即 EvidenceBundle 的 JSON 序列化（`EvidenceBundle.model_validate_json`
可直接解析，无第二套文件 schema）。result copy 的冻结锚点存放在
`metadata["result_copy_digests"]`（ref_id → sha256）——单文件 bundle 的 result_copy
digest 一致性以该锚点复核；跨 bundle 的 wire 级篡改检测属 eval 通道的
reference_bundles 职责（verifier Layer 6）。

退出码约定：0/2-7 是 spec 语义码；1 保留给 shell/用法错误；**70 是保留码**——
校验过程本身未分类异常时 fail closed，不把内部错误伪装成对 bundle 的判定。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import click
import typer

from zhiwei.evidence.bundles import EvidenceBundle
from zhiwei.evidence.verifier import (
    VerifyCheck,
    VerifyExitCode,
    VerifyResult,
    map_load_error,
    verify_bundle,
)

OUTPUT_FORMAT = Annotated[Literal["text", "json"], typer.Option("--format", help="输出格式")]

# 未分类内部错误保留码：非 0/1/2-7，语义是「工具本身失败」，不是任何 bundle 判定。
EXIT_UNEXPECTED = 70


def _failed_result(code: VerifyExitCode, detail: str) -> VerifyResult:
    result = VerifyResult()
    result.add(VerifyCheck("bundle_load", False, code, detail))
    return result


def _load_bundle(path: Path) -> EvidenceBundle:
    raw = path.read_bytes()
    return EvidenceBundle.model_validate_json(raw)


def _expected_copy_digests(bundle: EvidenceBundle) -> dict[str, str] | None:
    """从 metadata 提取 result copy 冻结锚点；存在但形态非法即 schema 违规。"""
    anchor = bundle.metadata.get("result_copy_digests")
    if anchor is None:
        return None
    if not isinstance(anchor, dict):
        raise ValueError("metadata.result_copy_digests must be an object")
    ref_ids = bundle.ref_ids()
    expected: dict[str, str] = {}
    for ref_id, digest in anchor.items():
        if not isinstance(ref_id, str) or not isinstance(digest, str):
            raise ValueError("metadata.result_copy_digests entries must be strings")
        if not digest.startswith("sha256:"):
            raise ValueError(f"result copy digest for {ref_id} must use sha256: prefix")
        if UUID(ref_id) not in ref_ids:
            raise ValueError(f"result copy digest anchored to unknown ref {ref_id}")
        expected[ref_id] = digest
    return expected


def _verify_file(path: Path) -> VerifyResult:
    try:
        bundle = _load_bundle(path)
        expected = _expected_copy_digests(bundle)
    except Exception as exc:
        return _failed_result(map_load_error(exc), f"{type(exc).__name__}: {exc}")
    return verify_bundle(bundle, expected_result_copy_digests=expected)


def _collect_targets(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        targets = sorted(path.glob("*.bundle"))
        if not targets:
            raise FileNotFoundError(f"no *.bundle files under directory: {path}")
        return targets
    raise FileNotFoundError(f"bundle path does not exist: {path}")


def verify_evidence(
    path: Annotated[Path, typer.Argument(help="bundle 文件或目录（逐文件验证 *.bundle）")],
    output_format: OUTPUT_FORMAT = "json",
) -> None:
    """对 Evidence bundle 分层验证（spec s6 §3 稳定退出码 0/2/3/4/5/6/7）。"""
    try:
        targets = _collect_targets(path)
    except (OSError, FileNotFoundError) as exc:
        # 路径不存在/目录为空都是输入问题 → input/schema（2）
        click.echo(f"验证失败: {exc}", err=True)
        raise typer.Exit(int(VerifyExitCode.INPUT_SCHEMA)) from None

    results: list[dict[str, Any]] = []
    for target in targets:
        try:
            result = _verify_file(target)
        except Exception as exc:
            click.echo(f"校验过程异常失败（未分类）: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(EXIT_UNEXPECTED) from None
        results.append(
            {
                "path": str(target),
                "ok": result.ok,
                "exit_code": int(result.exit_code),
                "checks": [c.as_dict() for c in result.checks],
            }
        )

    worst = max((entry["exit_code"] for entry in results), default=0)
    ok = all(entry["ok"] for entry in results)
    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "ok": ok,
                    "exit_code": worst,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        lines = [f"bundles: {sum(1 for e in results if e['ok'])}/{len(results)} passed"]
        for entry in results:
            mark = "PASS" if entry["ok"] else "FAIL"
            lines.append(f"  {entry['path']}: {mark} (exit_code={entry['exit_code']})")
            for check in entry["checks"]:
                flag = "ok" if check["ok"] else "XX"
                lines.append(f"    [{flag}] {check['check_id']}: {check['detail']}")
        click.echo("\n".join(lines))

    if not ok or worst != int(VerifyExitCode.SUCCESS):
        raise typer.Exit(worst)
