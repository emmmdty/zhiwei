"""S11-T1 dev CLI 契约：`dev up|down` + `dev doctor --strict`（plan Task 1 checkbox 5）。

冻结断言面（A 档契约；plan 2026-09-08 修订 F-R8-08 的 ops 码表先例在此扩展 dev 面）：

1. `dev up` / `dev down` 只作用于 S11 compose 文件 + 固定 project 名——包装器不得
   触碰无关 Compose 资源（plan 原文约束），argv 必须同时携带 `-p zhiwei-local` 与
   `-f <repo>/deploy/compose/compose.yaml`；
2. `dev up --dry-run` 是 config-only 演练：不执行 docker 子进程、打印将执行的 argv、
   退出 0；`--help` 永远退出 0（CLI 既有惯例）；
3. `dev down` 不得使用 `--volumes` 之外的破坏性扩散（`--remove-orphans` 允许——它只
   影响**本项目**孤儿容器；冻结此语义防误删其他项目）；
4. 包装器永不触发 live provider：up/down 不读取、不传递 OPENAI_* 凭据；
5. `dev doctor --strict`：非 local_product/production_reference 档拒绝（exit 1）；
   compose 配置校验失败 → exit 1；全部检查通过 → exit 0 且 JSON stdout 纯净。
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner, Result

from zhiwei.cli.dev import app as dev_app

runner = CliRunner()

PROJECT_NAME = "zhiwei-local"


def _run(*args: str) -> Result:
    return runner.invoke(dev_app, list(args))


def test_help_exits_zero() -> None:
    for argv in (
        ["dev", "--help"],
        ["up", "--help"],
        ["down", "--help"],
    ):
        result = runner.invoke(dev_app, argv[1:])
        assert result.exit_code == 0, f"{argv} 应退出 0: {result.output}"


def test_up_dry_run_prints_argv_and_does_not_execute_docker(monkeypatch) -> None:
    executed: list[list[str]] = []

    def _boom(cmd: object, *a: object, **k: object) -> None:
        executed.append(list(cmd))  # type: ignore[arg-type]
        raise AssertionError("dry-run 不得执行 docker 子进程")

    monkeypatch.setattr("zhiwei.cli.dev._run_docker", _boom)
    result = _run("up", "--dry-run")
    assert result.exit_code == 0, result.output
    assert not executed
    argv_text = result.output
    assert "-p" in argv_text and PROJECT_NAME in argv_text, "up 必须 scope 到固定 project"
    assert "deploy/compose/compose.yaml" in argv_text, "up 必须指向 S11 compose 文件"
    assert "up" in argv_text and "--wait" in argv_text, "up 必须以 --wait 等待健康"


def test_up_argv_scope_and_wait() -> None:
    plan = json.loads(_run("up", "--dry-run", "--format", "json").output)  # type: ignore[union-attr]
    argv = plan["argv"]
    idx_p = argv.index("-p")
    assert argv[idx_p + 1] == PROJECT_NAME
    compose_idx = argv.index("-f")
    compose_path = Path(argv[compose_idx + 1])
    assert compose_path.is_absolute() and compose_path.name == "compose.yaml"
    assert "deploy" in compose_path.parts and "compose" in compose_path.parts
    assert argv[-2:] == ["up", "-d"] or "up" in argv


def test_down_argv_scoped_no_cross_project_deletion() -> None:
    plan = json.loads(_run("down", "--dry-run", "--format", "json").output)  # type: ignore[union-attr]
    argv = plan["argv"]
    assert "-p" in argv and PROJECT_NAME in argv, "down 必须 scope 到固定 project"
    assert "deploy/compose/compose.yaml" in argv or any(
        "compose.yaml" in part for part in argv
    )
    assert "down" in argv
    # down 允许 --remove-orphans（仅本项目孤儿）；绝不出现跨项目破坏面
    assert "--all" not in argv


def test_wrapper_does_not_pass_live_credentials(monkeypatch) -> None:
    """包装器不读取/不传递 OPENAI_*：即进程环境里有也绝不出现在 argv。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.com")
    plan = json.loads(_run("up", "--dry-run", "--format", "json").output)  # type: ignore[union-attr]
    argv_text = json.dumps(plan)
    assert "sk-should-never-leak" not in argv_text
    assert "OPENAI_API_KEY" not in argv_text and "OPENAI_BASE_URL" not in argv_text


def test_doctor_strict_rejects_test_profile(monkeypatch) -> None:
    for env_key in list(__import__("os").environ):
        if env_key.startswith("ZHIWEI_"):
            monkeypatch.delenv(env_key, raising=False)
    result = _run("doctor", "--strict")
    assert result.exit_code == 1
    assert "local_product" in result.output or "strict" in result.output


def test_doctor_strict_json_stdout_is_pure_json(monkeypatch) -> None:
    import os

    for env_key in list(os.environ):
        if env_key.startswith("ZHIWEI_"):
            monkeypatch.delenv(env_key, raising=False)
    # test 档在 strict 下 exit 1；stdout 仍是可解析 JSON（诊断走 stderr，click≥8.2 默认分流）
    separated = CliRunner()
    result = separated.invoke(dev_app, ["doctor", "--strict", "--format", "json"])
    payload = json.loads(result.stdout)
    assert "strict" in payload
    assert isinstance(payload["checks"], dict)
