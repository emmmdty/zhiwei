"""S0-T1 RED：`zhiwei dev` 命令组的契约。

`dev doctor` 是开发者第一个会跑的命令，也是 S0 Gate 的一步。它必须回答"这套环境到底能不能
用"，且**在回答过程中不得碰任何网络**——一个会顺手 probe 一下 provider 的 doctor，会让
"不调用 live 模型"这条纪律在最不起眼的地方破掉。

这里断言的是命令行契约：退出码、输出结构、失败时的表现。不断言实现用了哪个库。
"""

from __future__ import annotations

import json
import socket
from typing import Any

import pytest
from typer.testing import CliRunner

from zhiwei.cli.main import app

runner = CliRunner()

TRACEBACK_MARKER = "Traceback (most recent call last)"

# CliRunner 的 env 是叠加在 os.environ 上的，不是替换。开发机上真实存在的 ZHIWEI_DATABASE_URL
# 或 OPENAI_API_KEY 会让断言随机变绿——所以每个用例都从一份显式清空的基线开始。
_MANAGED_VARS = (
    "ZHIWEI_PROFILE",
    "ZHIWEI_RELEASE_MODE",
    "ZHIWEI_DATABASE_URL",
    "ZHIWEI_OBJECT_STORE_ROOT",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
)


def _env(**overrides: str) -> dict[str, str | None]:
    env: dict[str, str | None] = dict.fromkeys(_MANAGED_VARS)
    env.update(overrides)
    return env


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """把 socket 连接变成硬错误，并记录所有尝试。

    比断言"输出里没有 provider 字样"强得多：任何库、任何间接调用发起的出网都会被抓住。
    """
    attempts: list[Any] = []

    def _refuse(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(address)
        raise AssertionError(f"doctor 不得发起网络连接，但尝试连接了 {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _refuse, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse, raising=True)
    return attempts


# --------------------------------------------------------------------------- --help


def test_root_help_lists_dev_group() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "dev" in result.output


def test_dev_help_lists_doctor() -> None:
    result = runner.invoke(app, ["dev", "--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output


def test_doctor_help_documents_format_option() -> None:
    result = runner.invoke(app, ["dev", "doctor", "--help"])
    assert result.exit_code == 0
    assert "--format" in result.output


# --------------------------------------------------------------------------- JSON 输出结构


def _doctor_json(**env: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["dev", "doctor", "--format", "json"], env=_env(**env))
    assert TRACEBACK_MARKER not in result.output, f"不得向用户抛裸 traceback:\n{result.output}"
    payload = json.loads(result.stdout)
    return result.exit_code, payload


def test_doctor_json_reports_the_four_required_fields(no_network: list[Any]) -> None:
    """spec §2：doctor 显示 DB、object store、schema revision、release mode。"""
    _, payload = _doctor_json(ZHIWEI_PROFILE="test")

    assert payload["profile"] == "test"
    assert payload["release_mode"] == "fixture_only"
    assert set(payload["checks"]) >= {"database", "object_store", "schema_revision"}
    for name, check in payload["checks"].items():
        assert "status" in check, f"check {name!r} 缺少 status"
    assert no_network == []


def test_doctor_json_states_live_calls_are_denied(no_network: list[Any]) -> None:
    """doctor 必须把"当前允不允许 live"直接印出来，不让人去猜。"""
    _, payload = _doctor_json(ZHIWEI_PROFILE="test")
    assert payload["live_model_calls_allowed"] is False


def test_doctor_never_probes_the_model_provider(no_network: list[Any]) -> None:
    """即使配置了 provider，doctor 也不得去连它。"""
    _, payload = _doctor_json(
        ZHIWEI_PROFILE="test",
        OPENAI_BASE_URL="http://127.0.0.1:9/v1",
        OPENAI_API_KEY="sk-should-never-be-used",
        OPENAI_MODEL="some-model",
    )
    assert no_network == []
    assert "sk-should-never-be-used" not in json.dumps(payload)


def test_doctor_output_never_contains_credentials() -> None:
    """doctor 配置了 DB 时会真实查询 revision（S0 Gate 契约），失败的连接与错误信息
    不得回显 DSN 或密码。此用例不挂 no_network：doctor 允许连配置的数据库，
    但绝不连接模型 provider（后者由 test_doctor_never_probes_the_model_provider 覆盖）。
    """
    secret = "sk-doctor-leak-check-771a"
    _, payload = _doctor_json(
        ZHIWEI_PROFILE="test",
        OPENAI_API_KEY=secret,
        ZHIWEI_DATABASE_URL=f"postgresql+asyncpg://u:{secret}@127.0.0.1:1/db",
    )
    assert secret not in json.dumps(payload, ensure_ascii=False)


def test_doctor_exit_code_is_nonzero_when_a_check_is_not_ok(no_network: list[Any]) -> None:
    """没配 DB 就是环境没就绪。doctor 报了问题却退出 0，Gate 就形同虚设。"""
    exit_code, payload = _doctor_json(ZHIWEI_PROFILE="test")
    assert payload["checks"]["database"]["status"] != "ok"
    assert exit_code != 0


def test_doctor_json_is_the_only_thing_on_stdout(no_network: list[Any]) -> None:
    """`--format json` 的 stdout 必须是可直接管进 jq 的纯 JSON。"""
    result = runner.invoke(app, ["dev", "doctor", "--format", "json"], env=_env(ZHIWEI_PROFILE="test"))
    json.loads(result.stdout)


# --------------------------------------------------------------------------- 非法输入


def test_doctor_rejects_unknown_format() -> None:
    result = runner.invoke(app, ["dev", "doctor", "--format", "yaml"], env=_env(ZHIWEI_PROFILE="test"))
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


def test_doctor_reports_invalid_profile_without_traceback() -> None:
    """配置错误要给人看得懂的一行，不是 pydantic 的栈。"""
    result = runner.invoke(app, ["dev", "doctor"], env=_env(ZHIWEI_PROFILE="prod"))
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output
    assert "ZHIWEI_PROFILE" in result.output


def test_doctor_refuses_live_mode_under_test_profile() -> None:
    result = runner.invoke(
        app, ["dev", "doctor"], env=_env(ZHIWEI_PROFILE="test", ZHIWEI_RELEASE_MODE="live")
    )
    assert result.exit_code != 0
    assert TRACEBACK_MARKER not in result.output


# --------------------------------------------------------------------------- 文本输出


def test_doctor_text_format_is_the_default(no_network: list[Any]) -> None:
    result = runner.invoke(app, ["dev", "doctor"], env=_env(ZHIWEI_PROFILE="test"))
    assert TRACEBACK_MARKER not in result.output
    assert "database" in result.output
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.stdout)
