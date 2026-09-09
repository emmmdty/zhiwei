"""live-synthesis-v1 suite 与 executor 契约（S11 live 演示任务，RED）。

背景（operator 2026-09-09 裁决）：发布面补 live 模型效果声明。S5 §9 claim
boundary 明确 offline knowledge suite 只声明检索/ACL/freshness，答案合成质量
需要 live 模型——本 suite 是最小的诚实 live 面：

- 检索走生产 Retrieve handler（Knowledge Planner，与 knowledge suite 同款）；
- 合成经生产 egress 机器（ModelEgressAssembler 门禁链 + OpenAIChatTransport +
  AuditedEndpointResolver 首次留痕）调用真实模型，operator token 门禁；
- 判分全部确定性（行为级）：证据在场 + 答案含 ground truth + 引用标记在场；
  拒绝合成质量评分（那需要 judge，本 suite 不声称）；
- 无证据 → 短路 abstain（不调模型，生产 abstain 语义）；ACL 拒绝 → 行为标签
  比对（不调模型）。只有可答单位发生真实模型调用（成本最小化）。

离线契约测试经 inner=MockTransport 注入（client_factory 文档化的测试路径，
不构成第二套生产组装路径）；sinks 注入 fake（Canonical 实现在 CLI 组合根接线）。
环境来源是显式 mapping——测试与工具链不读 os.environ/.env（AGENTS.md）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2 as httpx
import pytest

from zhiwei.evals.domain import RegisteredUnit, SampleStatus
from zhiwei.evals.executors.live_synthesis import LiveSynthesisExecutor
from zhiwei.evals.live_synthesis_suites import (
    LIVE_SYNTHESIS_V1,
    resolve_live_synthesis_suite,
)

_ENV: dict[str, str] = {
    "OPENAI_BASE_URL": "http://model.test/v1",
    "OPENAI_MODEL": "test-live-model",
    "OPENAI_API_KEY": "test-key-not-real",
}


class FakeFirstUseSink:
    def __init__(self, log: list[str] | None = None) -> None:
        self.calls: list[Any] = []
        self.run_ids: list[Any] = []
        self._log = log

    async def record_first_use(self, declaration: Any, *, run_id: Any) -> bool:
        self.calls.append(declaration)
        self.run_ids.append(run_id)
        if self._log is not None:
            self._log.append("first_use")
        return True


class FakeManifestSink:
    def __init__(self, log: list[str] | None = None) -> None:
        self.manifests: list[Any] = []
        self.run_ids: list[Any] = []
        self._log = log

    async def record_wire_manifest(self, manifest: Any, *, run_id: Any) -> bool:
        self.manifests.append(manifest)
        self.run_ids.append(run_id)
        if self._log is not None:
            self._log.append("manifest")
        return True

    async def record_transition_manifest(self, manifest: Any, *, run_id: Any) -> bool:
        return True


class FakeRunProvisioner:
    """记录供给请求并返回独立 run id 的 fake。

    生产实现在 CLI 组合根：经生产命令路径（RunCommandService.submit_start_run）
    创建租户作用域内的真实 Run 行后返回其 id——canonical 落账（first use /
    wire manifest）要求 run_id 有真实 Run 行，否则 fail closed（RunNotFound）。
    """

    def __init__(self, log: list[str] | None = None) -> None:
        self.calls: list[str] = []
        self.run_ids: dict[str, Any] = {}
        self._log = log

    async def __call__(self, sample_id: str) -> Any:
        self.calls.append(sample_id)
        run_id = uuid4()
        self.run_ids[sample_id] = run_id
        if self._log is not None:
            self._log.append("provision")
        return run_id


def _mock_model_inner(responses: list[dict[str, Any]]):
    """最小 OpenAI-compat responder：按调用序返回固定 completion。"""
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        index = min(counter["n"], len(responses) - 1)
        counter["n"] += 1
        return httpx.Response(200, json=responses[index])

    return httpx.MockTransport(handler)


def _ok_completion(text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "model": "test-live-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _executor(
    *,
    token: str = "operator-token-1",
    inner: httpx.AsyncBaseTransport | None = None,
    first_use: FakeFirstUseSink | None = None,
    manifests: FakeManifestSink | None = None,
    run_provisioner: FakeRunProvisioner | None = None,
) -> LiveSynthesisExecutor:
    suite = resolve_live_synthesis_suite()
    return LiveSynthesisExecutor(
        suite,
        operator_token=token,
        endpoints_path=Path("config/providers/endpoints.yaml"),
        env_overrides=_ENV,
        first_use_sink=first_use or FakeFirstUseSink(),
        manifest_sink=manifests or FakeManifestSink(),
        run_provisioner=run_provisioner or FakeRunProvisioner(),
        inner=inner,
    )


def _unit(sample_id: str) -> RegisteredUnit:
    suite = resolve_live_synthesis_suite()
    registered = next(u for u in suite.registered_units if u.sample_id == sample_id)
    return registered


class TestSuiteContract:
    def test_suite_registry_resolves_with_deterministic_units(self) -> None:
        suite = resolve_live_synthesis_suite()
        assert suite.name == LIVE_SYNTHESIS_V1
        assert len(suite.registered_units) == 6
        # 4 可答（真实模型调用）+ abstain + acl（短路，不调模型）
        kinds = sorted(unit.sample_id for unit in suite.registered_units)
        assert len(set(kinds)) == 6
        again = resolve_live_synthesis_suite()
        assert [(u.sample_id, u.unit_id) for u in suite.registered_units] == [
            (u.sample_id, u.unit_id) for u in again.registered_units
        ]
        # corpus digest 是代码定义证据文档的内容寻址（密封 provenance 可回溯）
        assert suite.corpus_digest.startswith("sha256:")

    def test_answerable_units_derive_from_answerable_spec(self) -> None:
        suite = resolve_live_synthesis_suite()
        answerable = [u for u in suite.registered_units if u.sample_id.startswith("live-ans-")]
        assert len(answerable) == 4


class TestOperatorGate:
    @pytest.mark.asyncio
    async def test_empty_token_refused_before_any_egress(self) -> None:
        manifests = FakeManifestSink()
        executor = _executor(token="   ", manifests=manifests)
        outcome = await executor.execute(_unit("live-ans-001"))
        assert outcome.status is SampleStatus.FAILED
        assert outcome.result["error"] == "operator_token_required"
        assert manifests.manifests == [], "门禁拒绝路径不得产生任何模型调用"

    @pytest.mark.asyncio
    async def test_empty_token_fails_closed_for_all_units(self) -> None:
        executor = _executor(token="")
        for unit in resolve_live_synthesis_suite().registered_units:
            outcome = await executor.execute(unit)
            assert outcome.status is SampleStatus.FAILED


class TestAnswerableUnits:
    @pytest.mark.asyncio
    async def test_live_synthesis_passes_with_citation_and_ground_truth(self) -> None:
        suite = resolve_live_synthesis_suite()
        spec = suite.answerable_specs["live-ans-001"]
        cited = f"根据证据 [1]，{spec.ground_truth}。"
        manifests = FakeManifestSink()
        executor = _executor(
            inner=_mock_model_inner([_ok_completion(cited)]), manifests=manifests
        )
        outcome = await executor.execute(_unit("live-ans-001"))
        assert outcome.status is SampleStatus.COMPLETED, outcome.result
        assert outcome.result["verdict"] == "pass"
        assert outcome.result["mode"] == "live"
        assert outcome.result["model"] == "test-live-model"
        assert spec.ground_truth in outcome.result["answer_text"]
        assert outcome.result["citations"] >= 1
        # 真实模型调用发生：wire manifest 落账（capture → sink）
        assert len(manifests.manifests) == 1
        assert manifests.manifests[0].body_sha256.startswith("sha256:")

    @pytest.mark.asyncio
    async def test_missing_citation_fails_the_unit(self) -> None:
        suite = resolve_live_synthesis_suite()
        spec = suite.answerable_specs["live-ans-001"]
        executor = _executor(
            inner=_mock_model_inner([_ok_completion(f"答案是 {spec.ground_truth}。")])
        )
        outcome = await executor.execute(_unit("live-ans-001"))
        assert outcome.status is SampleStatus.FAILED
        assert "citation_missing" in outcome.result["failures"]

    @pytest.mark.asyncio
    async def test_answer_missing_ground_truth_fails(self) -> None:
        executor = _executor(
            inner=_mock_model_inner([_ok_completion("证据 [1] 表明配置正常。")])
        )
        outcome = await executor.execute(_unit("live-ans-001"))
        assert outcome.status is SampleStatus.FAILED
        assert "ground_truth_missing" in outcome.result["failures"]

    @pytest.mark.asyncio
    async def test_provider_error_is_error_terminal_not_fabricated_pass(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "boom"}})

        executor = _executor(inner=httpx.MockTransport(handler))
        outcome = await executor.execute(_unit("live-ans-001"))
        assert outcome.status is SampleStatus.ERROR
        assert outcome.result["error_kind"] == "provider_error"

    @pytest.mark.asyncio
    async def test_deterministic_result_payload_for_fixed_model_output(self) -> None:
        suite = resolve_live_synthesis_suite()
        spec = suite.answerable_specs["live-ans-002"]
        cited = f"[1] {spec.ground_truth}"
        first = await _executor(
            inner=_mock_model_inner([_ok_completion(cited)])
        ).execute(_unit("live-ans-002"))
        second = await _executor(
            inner=_mock_model_inner([_ok_completion(cited)])
        ).execute(_unit("live-ans-002"))
        assert json.dumps(first.result, sort_keys=True, ensure_ascii=False) == json.dumps(
            second.result, sort_keys=True, ensure_ascii=False
        )


class TestShortCircuitUnits:
    @pytest.mark.asyncio
    async def test_abstain_unit_never_calls_the_model(self) -> None:
        manifests = FakeManifestSink()
        executor = _executor(
            inner=_mock_model_inner([_ok_completion("不应该被调用")]),
            manifests=manifests,
        )
        outcome = await executor.execute(_unit("live-abstain-no-evidence"))
        assert outcome.status is SampleStatus.COMPLETED
        assert outcome.result["verdict"] == "pass"
        assert outcome.result["behavior"] == "abstain_no_evidence"
        assert manifests.manifests == []

    @pytest.mark.asyncio
    async def test_acl_refusal_unit_matches_behavior_label(self) -> None:
        manifests = FakeManifestSink()
        executor = _executor(
            inner=_mock_model_inner([_ok_completion("不应该被调用")]),
            manifests=manifests,
        )
        outcome = await executor.execute(_unit("live-acl-refusal"))
        assert outcome.status is SampleStatus.COMPLETED
        assert outcome.result["verdict"] == "pass"
        assert outcome.result["behavior"] == "acl_refusal"
        assert manifests.manifests == []

    @pytest.mark.asyncio
    async def test_abstain_unit_fails_if_model_would_be_called(self) -> None:
        # 防御性契约：短路单位若观察到 wire manifest，判 0 分（防执行序漂移）
        manifests = FakeManifestSink()
        executor = _executor(
            inner=_mock_model_inner([_ok_completion("x")]),
            manifests=manifests,
        )
        await executor.execute(_unit("live-abstain-no-evidence"))
        outcome = await executor.execute(_unit("live-abstain-no-evidence"))
        assert outcome.result["verdict"] == "pass"
        assert len(manifests.manifests) == 0


class TestRunProvisioning:
    """可答单位的 Run 供给契约（S11 followup-3）。

    根因（首次 live 触发，eval_run e9b19e43）：executor 用合成 uuid 调模型，
    canonical 落账（first use / wire manifest）经 CanonicalUnitOfWork 要求
    run_id 在租户作用域有真实 Run 行——没有 → RunNotFound → 可答单位全 ERROR。
    契约：模型调用前必须经供给方取得 run id（生产命令路径由组合根注入），
    首次留痕与 wire manifest 绑定供给返回的 id，而非 executor 自行合成。
    """

    @pytest.mark.asyncio
    async def test_answerable_unit_binds_provisioned_run_id_to_ledger_writes(
        self,
    ) -> None:
        suite = resolve_live_synthesis_suite()
        spec = suite.answerable_specs["live-ans-001"]
        log: list[str] = []
        provisioner = FakeRunProvisioner(log)
        first_use = FakeFirstUseSink(log)
        manifests = FakeManifestSink(log)
        executor = _executor(
            inner=_mock_model_inner(
                [_ok_completion(f"根据证据 [1]，{spec.ground_truth}。")]
            ),
            first_use=first_use,
            manifests=manifests,
            run_provisioner=provisioner,
        )
        outcome = await executor.execute(_unit("live-ans-001"))
        assert outcome.status is SampleStatus.COMPLETED, outcome.result
        # 恰好供给一次，供给对象是该单位的 sample
        assert provisioner.calls == ["live-ans-001"]
        # 首次留痕与 wire manifest 绑定的是供给返回的 run id
        assert first_use.run_ids == [provisioner.run_ids["live-ans-001"]]
        assert manifests.run_ids == [provisioner.run_ids["live-ans-001"]]
        # 供给先于任何落账（canonical 落账要求 runs 表已有真实行）
        assert log == ["provision", "first_use", "manifest"]

    @pytest.mark.asyncio
    async def test_short_circuit_units_never_provision_runs(self) -> None:
        provisioner = FakeRunProvisioner()
        executor = _executor(
            inner=_mock_model_inner([_ok_completion("不应该被调用")]),
            run_provisioner=provisioner,
        )
        await executor.execute(_unit("live-abstain-no-evidence"))
        await executor.execute(_unit("live-acl-refusal"))
        assert provisioner.calls == []

    @pytest.mark.asyncio
    async def test_provider_error_unit_still_binds_provisioned_run_id(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": {"message": "boom"}})

        provisioner = FakeRunProvisioner()
        first_use = FakeFirstUseSink()
        executor = _executor(
            inner=httpx.MockTransport(handler),
            first_use=first_use,
            run_provisioner=provisioner,
        )
        outcome = await executor.execute(_unit("live-ans-001"))
        assert outcome.status is SampleStatus.ERROR
        assert outcome.result["error_kind"] == "provider_error"
        # 供给发生在请求之前：错误终态下 run id 也已是真实供给值
        assert provisioner.calls == ["live-ans-001"]
        assert first_use.run_ids == [provisioner.run_ids["live-ans-001"]]
