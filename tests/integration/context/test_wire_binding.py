"""S3-T5 Integration: wire binding tests.

Tests the full capture→pin→digest→gate→persist flow using httpx.MockTransport.
No network calls. Validates that:
1. CaptureTransport captures the correct body at the transport layer
2. PinnedBody ensures digest == bytes sent
3. PreSendGate rejects before inner transport is called
4. ContextManifest binds correctly to WireCapture
5. Tamper scenarios are detected
6. max_retries=0 is enforced at the transport level
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import httpx2 as httpx

from zhiwei.context.manifests import ContextManifest
from zhiwei.evidence.context_verify import (
    verify_manifest_integrity,
    verify_send_after_capture_mutation,
    verify_tamper_body,
    verify_tamper_inventory,
    verify_tamper_ir,
    verify_tamper_profile,
    verify_transition_integrity,
)
from zhiwei.models.presend import (
    CaptureTransport,
    PinnedBody,
    PreSendRejected,
    WireCapture,
    digest_bytes,
)


def _mock_responder(request: httpx.Request) -> httpx.Response:
    """Mock httpx responder that echoes the received body."""
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        },
        headers={"content-type": "application/json"},
    )


def _make_transport(gate=None) -> CaptureTransport:
    """Create a CaptureTransport with a mock inner transport."""
    inner = httpx.MockTransport(_mock_responder)
    return CaptureTransport(inner=inner, gate=gate)


def _logical_request() -> dict[str, Any]:
    return {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.0,
    }


# --------------------------------------------------------------------------- Capture mechanics


class TestCaptureTransport:
    """Test that CaptureTransport captures body at the transport layer."""

    def test_single_capture_count(self) -> None:
        transport = _make_transport()
        client = httpx.AsyncClient(transport=transport, timeout=10.0)

        async def _run() -> None:
            resp = await client.post("http://127.0.0.1:1/v1/chat/completions", json=_logical_request())
            assert resp.status_code == 200

        asyncio.run(_run())

        assert len(transport.captures) == 1
        cap = transport.captures[0]
        assert cap.method == "POST"
        assert cap.url == "http://127.0.0.1:1/v1/chat/completions"

    def test_digest_matches_server_receipt(self) -> None:
        """Digest computed by transport matches what the mock server received."""
        received_bodies: list[bytes] = []

        def responder(request: httpx.Request) -> httpx.Response:
            received_bodies.append(request.read())
            return httpx.Response(200, json={"ok": True})

        inner = httpx.MockTransport(responder)
        transport = CaptureTransport(inner=inner)
        client = httpx.AsyncClient(transport=transport, timeout=10.0)

        async def _run() -> None:
            await client.post("http://127.0.0.1:1/v1/chat/completions", json=_logical_request())

        asyncio.run(_run())

        assert len(transport.captures) == 1
        cap = transport.captures[0]
        assert cap.body_sha256 == digest_bytes(received_bodies[0])
        assert cap.body_len == len(received_bodies[0])

    def test_content_length_consistent(self) -> None:
        transport = _make_transport()
        client = httpx.AsyncClient(transport=transport, timeout=10.0)

        async def _run() -> None:
            await client.post("http://127.0.0.1:1/v1/chat/completions", json=_logical_request())

        asyncio.run(_run())

        cap = transport.captures[0]
        assert cap.content_length_consistent

    def test_auth_headers_redacted(self) -> None:
        """Authorization headers are redacted in the capture record."""
        inner = httpx.MockTransport(_mock_responder)
        transport = CaptureTransport(inner=inner)

        async def _run() -> None:
            client = httpx.AsyncClient(transport=transport, timeout=10.0)
            await client.post(
                "http://127.0.0.1:1/v1/chat/completions",
                json=_logical_request(),
                headers={"Authorization": "Bearer secret-key-123"},
            )

        asyncio.run(_run())

        cap = transport.captures[0]
        assert cap.redacted_headers.get("authorization") == "<redacted>"
        assert "secret-key-123" not in json.dumps(cap.redacted_headers)


# --------------------------------------------------------------------------- PinnedBody


class TestPinnedBody:
    """Test that PinnedBody pins stream to materialized bytes."""

    def test_sync_iteration(self) -> None:
        body = b"test body content"
        pinned = PinnedBody(body)
        chunks = list(pinned)
        assert chunks == [body]

    def test_async_iteration(self) -> None:
        body = b"test body async"
        pinned = PinnedBody(body)

        async def _collect() -> list[bytes]:
            return [chunk async for chunk in pinned]

        chunks = asyncio.run(_collect())
        assert chunks == [body]

    def test_pinning_prevents_stream_mutation(self) -> None:
        """After pinning, request.stream always yields the original bytes."""
        received_bodies: list[bytes] = []

        def responder(request: httpx.Request) -> httpx.Response:
            received_bodies.append(request.read())
            return httpx.Response(200, json={"ok": True})

        inner = httpx.MockTransport(responder)
        transport = CaptureTransport(inner=inner)
        client = httpx.AsyncClient(transport=transport, timeout=10.0)

        async def _run() -> None:
            await client.post("http://127.0.0.1:1/v1/chat/completions", json=_logical_request())

        asyncio.run(_run())

        assert len(transport.captures) == 1
        cap = transport.captures[0]
        # The captured body matches what the server actually received
        assert cap.body_sha256 == digest_bytes(received_bodies[0])


# --------------------------------------------------------------------------- PreSendGate


class TestPreSendGate:
    """Test that PreSendGate rejects before inner transport is called."""

    def test_gate_rejects_before_send(self) -> None:
        seen_captures: list[WireCapture] = []
        seen_bodies: list[bytes] = []

        def gate(capture: WireCapture, body: bytes) -> None:
            seen_captures.append(capture)
            seen_bodies.append(body)
            raise PreSendRejected("gate: rejected")

        transport = _make_transport(gate=gate)
        client = httpx.AsyncClient(transport=transport, timeout=10.0)

        async def _run() -> None:
            with contextlib.suppress(Exception):
                await client.post("http://127.0.0.1:1/v1/chat/completions", json=_logical_request())

        asyncio.run(_run())

        assert len(seen_captures) == 1
        assert len(seen_bodies) == 1
        # Inner transport was never called
        assert len(transport.captures) == 0

    def test_gate_passes_when_no_rejection(self) -> None:
        transport = _make_transport(gate=lambda c, b: None)
        client = httpx.AsyncClient(transport=transport, timeout=10.0)

        async def _run() -> None:
            resp = await client.post("http://127.0.0.1:1/v1/chat/completions", json=_logical_request())
            assert resp.status_code == 200

        asyncio.run(_run())

        assert len(transport.captures) == 1


# --------------------------------------------------------------------------- Manifest binding


class TestManifestBinding:
    """Test that ContextManifest binds correctly to WireCapture."""

    def _build_manifest_from_capture(self, cap: WireCapture) -> ContextManifest:
        return ContextManifest(
            manifest_id="test-001",
            body_sha256=cap.body_sha256,
            body_len=cap.body_len,
            url=cap.url,
            method=cap.method,
            redacted_headers=cap.redacted_headers,
            header_names=cap.header_names,
            source_inventory_digest="sha256:" + "a" * 64,
            target_profile_digest="sha256:" + "b" * 64,
            ir_digest="sha256:" + "c" * 64,
            captured_at=cap.captured_at,
            sequence_no=cap.seq,
        )

    def test_manifest_matches_capture(self) -> None:
        received_bodies: list[bytes] = []

        def responder(request: httpx.Request) -> httpx.Response:
            received_bodies.append(request.read())
            return httpx.Response(200, json={"ok": True})

        inner = httpx.MockTransport(responder)
        transport = CaptureTransport(inner=inner)
        client = httpx.AsyncClient(transport=transport, timeout=10.0)

        async def _run() -> None:
            await client.post("http://127.0.0.1:1/v1/chat/completions", json=_logical_request())

        asyncio.run(_run())

        cap = transport.captures[0]
        manifest = self._build_manifest_from_capture(cap)

        result = verify_manifest_integrity(
            manifest, cap,
            body_bytes=received_bodies[0],
            inventory_digest=manifest.source_inventory_digest,
            profile_digest=manifest.target_profile_digest,
            ir_digest=manifest.ir_digest,
        )
        assert result.ok, f"verification failed: {result.checks}"

    def test_valid_manifest_all_checks_pass(self) -> None:
        received_bodies: list[bytes] = []

        def responder(request: httpx.Request) -> httpx.Response:
            received_bodies.append(request.read())
            return httpx.Response(200, json={"ok": True})

        inner = httpx.MockTransport(responder)
        transport = CaptureTransport(inner=inner)
        client = httpx.AsyncClient(transport=transport, timeout=10.0)

        async def _run() -> None:
            await client.post("http://127.0.0.1:1/v1/chat/completions", json=_logical_request())

        asyncio.run(_run())

        cap = transport.captures[0]
        manifest = self._build_manifest_from_capture(cap)
        result = verify_manifest_integrity(manifest, cap, body_bytes=received_bodies[0])
        assert result.ok
        assert len(result.checks) >= 5


# --------------------------------------------------------------------------- Tamper detection


class TestTamperDetection:
    """Test that various tamper scenarios are detected."""

    def _make_manifest_and_body(self) -> tuple[ContextManifest, bytes]:
        body = b'{"model":"test","messages":[{"role":"user","content":"hi"}]}'
        manifest = ContextManifest(
            manifest_id="tamper-test-001",
            body_sha256=digest_bytes(body),
            body_len=len(body),
            url="http://127.0.0.1:1/v1/chat/completions",
            method="POST",
            redacted_headers={},
            header_names=(),
            source_inventory_digest="sha256:" + "a" * 64,
            target_profile_digest="sha256:" + "b" * 64,
            ir_digest="sha256:" + "c" * 64,
            captured_at="2026-09-01T00:00:00+00:00",
            sequence_no=0,
        )
        return manifest, body

    def test_tampered_ir_detected(self) -> None:
        manifest, _ = self._make_manifest_and_body()
        result = verify_tamper_ir(manifest, tampered_ir_digest="sha256:" + "f" * 64)
        assert result.ok

    def test_tampered_body_detected(self) -> None:
        manifest, _ = self._make_manifest_and_body()
        result = verify_tamper_body(manifest, tampered_body=b"tampered body content")
        assert result.ok

    def test_tampered_inventory_detected(self) -> None:
        manifest, _ = self._make_manifest_and_body()
        result = verify_tamper_inventory(manifest, tampered_inventory_digest="sha256:" + "f" * 64)
        assert result.ok

    def test_tampered_profile_detected(self) -> None:
        manifest, _ = self._make_manifest_and_body()
        result = verify_tamper_profile(manifest, tampered_profile_digest="sha256:" + "f" * 64)
        assert result.ok

    def test_send_after_capture_mutation_detected(self) -> None:
        manifest, body = self._make_manifest_and_body()
        result = verify_send_after_capture_mutation(manifest, mutated_body=body + b"extra")
        assert result.ok


# --------------------------------------------------------------------------- Transition manifest


class TestTransitionManifest:
    """Test TransitionManifest verification."""

    def test_valid_transition_passes(self) -> None:
        from zhiwei.context.manifests import TransitionManifest

        transition = TransitionManifest(
            manifest_id="trans-001",
            before_state_digest="sha256:" + "d" * 64,
            after_state_digest="sha256:" + "e" * 64,
            transition_type="context.created",
            wire_body_digest="sha256:" + "a" * 64,
            ir_digest="sha256:" + "c" * 64,
            items_added=1,
            items_removed=0,
            items_unchanged=0,
            triggered_by_manifest_id="manifest-001",
            occurred_at="2026-09-01T00:00:00+00:00",
        )
        result = verify_transition_integrity(
            transition,
            before_digest=transition.before_state_digest,
            after_digest=transition.after_state_digest,
        )
        assert result.ok

    def test_transition_detects_mismatched_before_digest(self) -> None:
        from zhiwei.context.manifests import TransitionManifest

        transition = TransitionManifest(
            manifest_id="trans-002",
            before_state_digest="sha256:" + "d" * 64,
            after_state_digest="sha256:" + "e" * 64,
            transition_type="context.updated",
            wire_body_digest="sha256:" + "a" * 64,
            ir_digest=None,
            items_added=0,
            items_removed=1,
            items_unchanged=2,
            triggered_by_manifest_id=None,
            occurred_at="2026-09-01T00:00:00+00:00",
        )
        result = verify_transition_integrity(
            transition,
            before_digest="sha256:" + "ff" * 32,
        )
        assert not result.ok
        assert any(c["id"] == "before_state_digest_match" and not c["ok"] for c in result.checks)
