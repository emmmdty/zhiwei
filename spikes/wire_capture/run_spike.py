"""spike-01 主程序：跑完全部场景并写出证据文件。

    uv run python spikes/wire_capture/run_spike.py

退出码 0 = 全部断言通过。证据写入 `evidence/spike-01-wire-capture.json`。

不发真实网络请求：所有流量走 127.0.0.1 上的 loopback mock endpoint，或走 httpx.MockTransport
（完全在进程内）。不读 `.env`——api_key / base_url 全部显式传入，不走环境变量回退。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import openai
from capture import CaptureTransport, PinnedBody, PreSendRejected, WireCapture
from mock_endpoint import EndpointConfig, MockEndpoint
from openai import AsyncOpenAI

EVIDENCE_PATH = Path(__file__).resolve().parent / "evidence" / "spike-01-wire-capture.json"
FAKE_KEY = "spike-key-not-a-credential"
LARGE_BODY_BYTES = 8 * 1024 * 1024


@dataclass
class Scenario:
    name: str
    question: str
    checks: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def check(self, cid: str, ok: bool, detail: str, *, timing_dependent: bool = False) -> None:
        entry: dict[str, Any] = {"id": cid, "ok": bool(ok), "detail": detail}
        if timing_dependent:
            entry["timing_dependent"] = True
        self.checks.append(entry)

    @property
    def passed(self) -> bool:
        return all(c["ok"] for c in self.checks)


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _client(
    endpoint: MockEndpoint, transport: httpx.AsyncBaseTransport, *, max_retries: int
) -> AsyncOpenAI:
    # api_key/base_url 显式传入：openai SDK 只在参数为 None 时才回落到环境变量，这里堵死那条路。
    return AsyncOpenAI(
        api_key=FAKE_KEY,
        base_url=endpoint.base_url,
        max_retries=max_retries,
        http_client=httpx.AsyncClient(transport=transport, timeout=60.0),
    )


def _logical_request(content: str = "hello") -> dict[str, Any]:
    return {
        "model": "spike-model",
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.0,
    }


# --------------------------------------------------------------------------- 场景


async def s1_non_streaming(endpoint: MockEndpoint) -> Scenario:
    s = Scenario("S1-non-streaming", "非流式请求：transport 捕到的 body 是否等于服务端收到的 body？")
    endpoint.reset(EndpointConfig())
    transport = CaptureTransport(inner=httpx.AsyncHTTPTransport(), keep_bodies=True)
    kwargs = _logical_request()

    async with _client(endpoint, transport, max_retries=0) as client:
        resp = await client.chat.completions.create(**kwargs)  # type: ignore[arg-type]

    cap = transport.captures[0]
    rec = endpoint.receipts[0]

    s.check("capture_count", len(transport.captures) == 1, f"transport 捕获 {len(transport.captures)} 次")
    s.check("receipt_count", len(endpoint.receipts) == 1, f"服务端接收 {len(endpoint.receipts)} 次")
    s.check(
        "digest_match",
        cap.body_sha256 == rec.body_sha256,
        f"capture={cap.body_sha256} server={rec.body_sha256}",
    )
    s.check("length_match", cap.body_len == rec.body_len, f"{cap.body_len} vs {rec.body_len}")
    s.check(
        "content_length_consistent",
        cap.content_length_consistent,
        f"Content-Length={cap.content_length_header} 实际={cap.body_len}",
    )
    s.check("response_parsed", resp.choices[0].message.content == "ok", "SDK 正常解析响应")
    s.check(
        "authorization_redacted",
        cap.redacted_headers.get("authorization") == "<redacted>"
        and FAKE_KEY not in json.dumps(cap.redacted_headers),
        "凭据头已在 capture 记录中脱敏",
    )

    # SDK 在序列化阶段动了什么——这是 ADR-001 失真路径 #3 的直接量化。
    sent = json.loads(transport.bodies[0])
    caller_keys = set(kwargs)
    wire_keys = set(sent) if isinstance(sent, dict) else set()
    s.facts["caller_top_level_keys"] = sorted(caller_keys)
    s.facts["wire_top_level_keys"] = sorted(wire_keys)
    # key 顺序是字节级事实：调用层就算把同一个 dict 重新序列化一遍，也未必得到同一串 bytes。
    s.facts["caller_key_order"] = list(kwargs)
    s.facts["wire_key_order"] = list(sent) if isinstance(sent, dict) else []
    s.facts["keys_added_by_sdk"] = sorted(wire_keys - caller_keys)
    s.facts["keys_dropped_by_sdk"] = sorted(caller_keys - wire_keys)
    s.facts["wire_body_utf8"] = transport.bodies[0].decode()
    s.facts["wire_body_sha256"] = cap.body_sha256
    s.facts["wire_body_len"] = cap.body_len
    s.facts["sdk_injected_headers"] = sorted(
        h for h in cap.header_names if h.startswith("x-stainless") or h == "user-agent"
    )
    return s


async def s2_streaming(endpoint: MockEndpoint) -> Scenario:
    s = Scenario("S2-streaming", "stream=True：捕获点、digest 与增量下发是否与非流式一致？")
    endpoint.reset(EndpointConfig(stream_chunks=4, stream_chunk_delay_s=0.05))
    transport = CaptureTransport(inner=httpx.AsyncHTTPTransport(), keep_bodies=True)

    arrivals: list[float] = []
    deltas: list[str] = []
    t0 = time.perf_counter()
    async with _client(endpoint, transport, max_retries=0) as client:
        stream = await client.chat.completions.create(**_logical_request(), stream=True)  # type: ignore[arg-type]
        async for chunk in stream:
            arrivals.append(time.perf_counter() - t0)
            piece = chunk.choices[0].delta.content
            if piece:
                deltas.append(piece)

    cap = transport.captures[0]
    rec = endpoint.receipts[0]
    spread = (arrivals[-1] - arrivals[0]) if len(arrivals) > 1 else 0.0

    s.check("capture_count", len(transport.captures) == 1, f"捕获 {len(transport.captures)} 次")
    s.check("digest_match", cap.body_sha256 == rec.body_sha256, f"{cap.body_sha256} == {rec.body_sha256}")
    s.check(
        "stream_flag_in_captured_body",
        cap.stream_flag_in_body is True,
        f'捕获到的 body 里 "stream"={cap.stream_flag_in_body}（从 bytes 反解，非从调用参数取）',
    )
    s.check("server_saw_stream", rec.streamed, "服务端确认按 SSE 分块返回")
    s.check("deltas_received", deltas == ["d0", "d1", "d2", "d3"], f"收到 deltas={deltas}")
    s.check(
        "response_not_buffered",
        spread > 0.08,
        f"首末 delta 间隔 {spread:.3f}s > 0.08s，说明 capture transport 未缓冲响应",
        timing_dependent=True,
    )
    s.check(
        "same_capture_point_as_non_stream",
        cap.method == "POST" and cap.url.endswith("/chat/completions"),
        f"{cap.method} {cap.url}",
    )
    s.facts["delta_arrival_offsets_s"] = [round(a, 4) for a in arrivals]
    s.facts["wire_body_utf8"] = transport.bodies[0].decode()
    s.facts["wire_body_sha256"] = cap.body_sha256
    return s


async def s3_retries_enabled(endpoint: MockEndpoint) -> Scenario:
    s = Scenario(
        "S3a-sdk-retries-enabled",
        "max_retries=2 且服务端返回 429：一次逻辑调用对应几次实际发送？",
    )
    endpoint.reset(EndpointConfig(status_plan=deque([429, 429])))
    transport = CaptureTransport(inner=httpx.AsyncHTTPTransport())

    sdk_call_count = 0
    async with _client(endpoint, transport, max_retries=2) as client:
        sdk_call_count += 1
        resp = await client.chat.completions.create(**_logical_request())  # type: ignore[arg-type]

    caps = transport.captures
    recs = endpoint.receipts
    bodies_identical = len({c.body_sha256 for c in caps}) == 1
    retry_headers = [c.retry_count_header for c in caps]

    s.check("sdk_layer_sees_one_call", sdk_call_count == 1, "SDK 调用层只看到 1 次 create()")
    s.check("transport_sees_three", len(caps) == 3, f"transport 捕获 {len(caps)} 次")
    s.check("server_receives_three", len(recs) == 3, f"服务端接收 {len(recs)} 次")
    s.check(
        "per_attempt_digest_match",
        all(c.body_sha256 == r.body_sha256 for c, r in zip(caps, recs, strict=True)),
        "逐次 capture digest 与服务端 digest 一一对应",
    )
    s.check(
        "retry_count_header_increments",
        retry_headers == ["0", "1", "2"],
        f"x-stainless-retry-count={retry_headers}——每次重试是重新构造的 request",
    )
    s.check("succeeded_on_third", resp.choices[0].message.content == "ok", "第三次返回 200")
    s.facts["sdk_call_count"] = sdk_call_count
    s.facts["wire_send_count"] = len(recs)
    s.facts["bodies_identical_across_attempts"] = bodies_identical
    s.facts["body_digests"] = [c.body_sha256 for c in caps]
    s.facts["retry_count_headers"] = retry_headers
    s.facts["note"] = (
        "openai/_base_client.py 在重试循环内调用 self._build_request(options, retries_taken=n)，"
        "即每次重试重新构造并重新序列化 request；SDK 调用层 hook 只能看到 1 次逻辑调用。"
    )
    return s


async def s3b_retries_disabled(endpoint: MockEndpoint) -> Scenario:
    s = Scenario("S3b-max-retries-0", "max_retries=0：是否恰好一次发送、错误是否直接上抛？")
    endpoint.reset(EndpointConfig(status_plan=deque([429])))
    transport = CaptureTransport(inner=httpx.AsyncHTTPTransport())

    raised: str | None = None
    async with _client(endpoint, transport, max_retries=0) as client:
        try:
            await client.chat.completions.create(**_logical_request())  # type: ignore[arg-type]
        except openai.RateLimitError as exc:
            raised = type(exc).__name__

    s.check("exactly_one_capture", len(transport.captures) == 1, f"捕获 {len(transport.captures)} 次")
    s.check("exactly_one_receipt", len(endpoint.receipts) == 1, f"服务端接收 {len(endpoint.receipts)} 次")
    s.check("error_surfaced", raised == "RateLimitError", f"上抛 {raised}")
    s.check(
        "retry_header_zero",
        transport.captures[0].retry_count_header == "0",
        f"x-stainless-retry-count={transport.captures[0].retry_count_header}",
    )
    s.facts["conclusion"] = "max_retries=0 时「一次逻辑调用 = 一次 wire 发送」成立，可作为结构不变量。"
    return s


async def s4_large_body(endpoint: MockEndpoint) -> Scenario:
    s = Scenario("S4-large-body", f"~{LARGE_BODY_BYTES // (1024 * 1024)}MiB body：是否仍能完整捕获？")
    endpoint.reset(EndpointConfig())
    transport = CaptureTransport(inner=httpx.AsyncHTTPTransport())
    payload = "x" * LARGE_BODY_BYTES

    t0 = time.perf_counter()
    async with _client(endpoint, transport, max_retries=0) as client:
        await client.chat.completions.create(**_logical_request(payload))  # type: ignore[arg-type]
    elapsed = time.perf_counter() - t0

    cap = transport.captures[0]
    rec = endpoint.receipts[0]

    s.check("digest_match", cap.body_sha256 == rec.body_sha256, f"{cap.body_sha256} == {rec.body_sha256}")
    s.check("length_match", cap.body_len == rec.body_len, f"{cap.body_len} == {rec.body_len}")
    s.check("body_larger_than_payload", cap.body_len > LARGE_BODY_BYTES, f"body={cap.body_len}B")
    s.check(
        "content_length_used_not_chunked",
        cap.content_length_header == cap.body_len and rec.transfer_encoding is None,
        f"Content-Length={cap.content_length_header}，Transfer-Encoding={rec.transfer_encoding}",
    )
    s.facts["body_len"] = cap.body_len
    s.facts["elapsed_s"] = round(elapsed, 3)
    s.facts["note"] = (
        "httpx 对 json= 一律先编码为 bytes 再包 ByteStream，不因体积切换到 chunked，"
        "所以大 body 与小 body 走同一条捕获路径。"
    )
    return s


async def s5_fail_closed(endpoint: MockEndpoint) -> Scenario:
    s = Scenario("S5-fail-closed", "pre-send gate 拒绝时，请求是否真的没出去？")
    endpoint.reset(EndpointConfig())
    seen: list[WireCapture] = []

    def gate(capture: WireCapture, body: bytes) -> None:
        seen.append(capture)
        raise PreSendRejected("spike: digest 计算/门禁失败，拒绝发送")

    transport = CaptureTransport(inner=httpx.AsyncHTTPTransport(), gate=gate)
    raised: str | None = None
    async with _client(endpoint, transport, max_retries=0) as client:
        try:
            await client.chat.completions.create(**_logical_request())  # type: ignore[arg-type]
        except Exception as exc:  # SDK 把任意 transport 异常包装成 APIConnectionError
            raised = type(exc).__name__

    s.check("gate_invoked", len(seen) == 1, f"gate 被调用 {len(seen)} 次")
    s.check("nothing_reached_server", len(endpoint.receipts) == 0, f"服务端接收 {len(endpoint.receipts)} 次")
    s.check("no_success_capture_recorded", len(transport.captures) == 0, "captures 列表为空——不存在无门禁的发送记录")
    s.check("error_surfaced", raised is not None, f"上抛 {raised}")
    s.facts["exception_type_seen_by_caller"] = raised
    s.facts["note"] = "gate 抛出时 inner transport 从未被调用，'先发了再补记' 在结构上不可能。"
    return s


async def s6_capture_point_integrity(endpoint: MockEndpoint) -> Scenario:
    s = Scenario(
        "S6-capture-point-integrity",
        "digest 与实际 wire bytes 会在什么条件下分叉？",
    )

    # 等长篡改：Content-Length 不变，h11 不会提前拦下，篡改能真正走到 socket 上。
    # 这也更贴近真实的「静默改写」——长度对不上的粗糙篡改本来就活不过传输层。
    def tamper(body: bytes) -> bytes:
        out = body.replace(b'"hello"', b'"he11o"')
        assert len(out) == len(body) and out != body
        return out

    # T1: 捕获后改 request._content，不动 stream。
    endpoint.reset(EndpointConfig())
    transport = CaptureTransport(
        inner=httpx.AsyncHTTPTransport(),
        pin_body=False,
        _post_capture_hook=lambda req, body: setattr(req, "_content", tamper(body)),
    )
    async with _client(endpoint, transport, max_retries=0) as client:
        await client.chat.completions.create(**_logical_request())  # type: ignore[arg-type]
    t1_cap, t1_rec = transport.captures[0], endpoint.receipts[0]
    s.check(
        "T1_content_mutation_never_reaches_wire",
        t1_rec.body_sha256 == t1_cap.body_sha256,
        f"改写 request._content 后服务端仍收到原始 body（{t1_rec.body_sha256}）"
        f"——.content 不是 wire 真相，request.stream 才是",
    )

    # T2: 不 pin，捕获后换掉 stream（模拟捕获层下面还有一层）。
    endpoint.reset(EndpointConfig())
    transport = CaptureTransport(
        inner=httpx.AsyncHTTPTransport(),
        pin_body=False,
        _post_capture_hook=lambda req, body: setattr(req, "stream", PinnedBody(tamper(body))),
    )
    async with _client(endpoint, transport, max_retries=0) as client:
        await client.chat.completions.create(**_logical_request())  # type: ignore[arg-type]
    t2_cap, t2_rec = transport.captures[0], endpoint.receipts[0]
    s.check(
        "T2_stream_swap_below_capture_diverges",
        t2_cap.body_sha256 != t2_rec.body_sha256 and t2_cap.body_len == t2_rec.body_len,
        f"capture={t2_cap.body_sha256} 但服务端={t2_rec.body_sha256}，长度相同"
        f"——捕获层下面若还有一层，digest 会指向一份从未发送的 body，且 Content-Length 交叉校验也拦不住",
    )

    # T3: pin 之后同样的攻击。
    endpoint.reset(EndpointConfig())
    transport = CaptureTransport(
        inner=httpx.AsyncHTTPTransport(),
        pin_body=True,
        _post_capture_hook=lambda req, body: setattr(req, "stream", PinnedBody(tamper(body))),
    )
    async with _client(endpoint, transport, max_retries=0) as client:
        await client.chat.completions.create(**_logical_request())  # type: ignore[arg-type]
    t3_cap, t3_rec = transport.captures[0], endpoint.receipts[0]
    s.check(
        "T3_pinning_alone_does_not_defend_lower_layer",
        t3_cap.body_sha256 != t3_rec.body_sha256,
        "pin 不能阻止捕获层下方的替换——保证只能来自架构约束：capture 必须是最内层 transport",
    )
    s.facts["conclusion"] = (
        "pin 解决的是 request._content 与 request.stream 分叉、以及不可重放 stream 两个问题；"
        "「捕到的就是发出去的」还额外依赖一条架构不变量：CaptureTransport 之下不得再有任何 wrapper。"
    )
    s.facts["required_architecture_test"] = (
        "断言构造出的 httpx.AsyncClient，其 transport 链最内层是真实 HTTP transport，"
        "CaptureTransport 紧邻其上；且 Core 不允许任何模块自建 AsyncClient。"
    )
    return s


async def s7_mock_transport(endpoint: MockEndpoint) -> Scenario:
    """S3 的测试要能离线跑。验证同一捕获层在 httpx.MockTransport 下行为一致。"""
    s = Scenario("S7-mock-transport", "同一捕获层能否在无 socket 的 MockTransport 下工作？")
    seen_bodies: list[bytes] = []

    def responder(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(request.read())
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0,
                                         "model": "spike-model",
                                         "choices": [{"index": 0, "finish_reason": "stop",
                                                      "message": {"role": "assistant", "content": "ok"}}]})

    transport = CaptureTransport(inner=httpx.MockTransport(responder))
    async with AsyncOpenAI(
        api_key=FAKE_KEY,
        base_url="http://127.0.0.1:1/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=transport, timeout=10.0),
    ) as client:
        await client.chat.completions.create(**_logical_request())  # type: ignore[arg-type]

    cap = transport.captures[0]
    s.check("capture_count", len(transport.captures) == 1, f"捕获 {len(transport.captures)} 次")
    s.check(
        "digest_match_inner_view",
        cap.body_sha256 == _digest(seen_bodies[0]),
        "inner transport 读到的 bytes 与 capture digest 一致",
    )
    s.facts["note"] = "S3 的 wire tamper corpus 可以完全离线运行，不需要 socket。"
    return s


# --------------------------------------------------------------------------- 入口


async def _main() -> int:
    scenarios: list[Scenario] = []
    with MockEndpoint() as endpoint:
        for fn in (
            s1_non_streaming,
            s2_streaming,
            s3_retries_enabled,
            s3b_retries_disabled,
            s4_large_body,
            s5_fail_closed,
            s6_capture_point_integrity,
            s7_mock_transport,
        ):
            scenarios.append(await fn(endpoint))

    all_ok = all(s.passed for s in scenarios)
    evidence = {
        "spike": "spike-01 wire capture 保真性",
        "adr": "ADR-001",
        "verdict": "FEASIBLE" if all_ok else "FAILED",
        "environment": {
            "python": sys.version.split()[0],
            "httpx": httpx.__version__,
            "openai": openai.__version__,
            "network": "loopback 127.0.0.1 only + in-process httpx.MockTransport",
        },
        "scenarios": [
            {"name": s.name, "question": s.question, "passed": s.passed,
             "checks": s.checks, "facts": s.facts}
            for s in scenarios
        ],
    }
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for s in scenarios:
        print(f"\n{'PASS' if s.passed else 'FAIL'}  {s.name}  — {s.question}")
        for c in s.checks:
            mark = "  ok " if c["ok"] else "  XX "
            print(f"{mark}{c['id']}: {c['detail']}")
    print(f"\n证据写入 {EVIDENCE_PATH}")
    print("verdict:", evidence["verdict"])
    return 0 if all_ok else 1


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
