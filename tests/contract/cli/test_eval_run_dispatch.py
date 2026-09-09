"""`eval run` 的 suite→flow dispatch 完整性契约（S11 followup-3）。

背景：live 演示任务 GREEN 提交把 `_live_synthesis_flow` 以零缩进插进 `run()`
函数体中间，dispatch 尾部（knowledge/factqa/ask/memory/security/change-brief/
risk/legacy）随之变成死代码——run() 对这些注册 suite 静默不作为，且
`_KNOWN_SUITES` 丢掉 S6–S10 union 后这些 suite 还会被「未知 suite」拒绝。
本契约把「每个注册 suite 都必须路由到自己的执行 flow」变成可观测断言，
防止函数体中段插入再次截断 dispatch 链。

settings/runtime 依赖经 sentinel 桩替换（test_eval_memory_cli.py 同款口径）：
dispatch 测试不需要 DB，flow 本身的行为由各自的 integration/契约测试覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import zhiwei.cli.evals as evals_cli
from zhiwei.evals.domain import EvalMode


class _DispatchRecorder:
    def __init__(self) -> None:
        self.flows: list[str] = []


def _install_stub(
    monkeypatch: pytest.MonkeyPatch, recorder: _DispatchRecorder, flow_attr: str
) -> None:
    monkeypatch.setattr(
        evals_cli,
        "_settings_runtime",
        lambda: (None, "unused-dsn", Path("/tmp"), object(), object()),
    )
    if flow_attr == "_run_flow":

        def _fake_run_flow(sessions: Any, flow: Any, context: Any) -> dict[str, Any]:
            recorder.flows.append(flow_attr)
            return {"ok": True}

        monkeypatch.setattr(evals_cli, "_run_flow", _fake_run_flow)
        return

    async def _fake_flow(*args: Any, **kwargs: Any) -> dict[str, Any]:
        recorder.flows.append(flow_attr)
        return {"ok": True}

    monkeypatch.setattr(evals_cli, flow_attr, _fake_flow)


@pytest.mark.parametrize(
    ("suite_name", "mode", "operator_token", "flow_attr"),
    [
        pytest.param(
            "knowledge-doc-v1", EvalMode.OFFLINE, None,
            "_knowledge_suite_flow", id="knowledge",
        ),
        pytest.param(
            "factqa-v1", EvalMode.FIXTURE, None,
            "_factqa_suite_flow", id="factqa",
        ),
        pytest.param(
            "ask-v1", EvalMode.OFFLINE, None,
            "_ask_suite_flow", id="ask",
        ),
        pytest.param(
            "enterprise-memory-v1", EvalMode.OFFLINE, None,
            "_memory_suite_flow", id="memory",
        ),
        pytest.param(
            "security-v1", EvalMode.OFFLINE, None,
            "_security_suite_flow", id="security",
        ),
        pytest.param(
            "change-brief-v1", EvalMode.OFFLINE, None,
            "_change_brief_suite_flow", id="change-brief",
        ),
        pytest.param(
            "numeric-risk-v1", EvalMode.OFFLINE, None,
            "_risk_suite_flow", id="risk",
        ),
        pytest.param(
            "discover-blind-v1", EvalMode.OFFLINE, None,
            "_risk_suite_flow", id="discover-blind",
        ),
        pytest.param(
            "live-synthesis-v1", EvalMode.LIVE, "operator-token-1",
            "_live_synthesis_flow", id="live-synthesis",
        ),
        pytest.param(
            "legacy-assets", EvalMode.OFFLINE, None,
            "_run_flow", id="legacy-fallback",
        ),
    ],
)
def test_run_dispatches_every_registered_suite_to_its_flow(
    monkeypatch: pytest.MonkeyPatch,
    suite_name: str,
    mode: EvalMode,
    operator_token: str | None,
    flow_attr: str,
) -> None:
    recorder = _DispatchRecorder()
    _install_stub(monkeypatch, recorder, flow_attr)
    evals_cli.run(
        suite=suite_name,
        executor="legacy",
        mode=mode,
        seal=False,
        operator_token=operator_token,
    )
    assert recorder.flows == [flow_attr], (
        f"suite {suite_name} 必须恰好路由到 {flow_attr}（静默不作为或错路由都算回归）"
    )
