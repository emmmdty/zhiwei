"""F-R2-04/F-R9-04：runner 侧沙箱校验与域校验对齐（prebuilt/k8s）。

域层 SandboxSpec.validate_sandbox 与各 runner 的 _validate_sandbox* 必须检查
同一组约束（non_root/read_only_rootfs/no_docker_socket/no_network/digest 格式）；
k8s manifest 的容器 image 必须取沙箱 pinned digest（admission 钉住的 digest 才是
实际 pull 的对象），不得回退到 runner 部署配置。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from zhiwei.capabilities.runners.contracts import (
    RunnerInvocationRequest,
    RunnerKind,
    RunnerSpec,
)
from zhiwei.capabilities.runners.kubernetes import KubernetesRunner
from zhiwei.capabilities.runners.prebuilt import PrebuiltRunner

_VALID_DIGEST = "sha256:" + "c" * 64


def _spec(kind: RunnerKind, *, image_digest: str = _VALID_DIGEST) -> RunnerSpec:
    return RunnerSpec(
        id=uuid4(),
        name="test-runner",
        kind=kind,
        image_digest=image_digest,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )


def _sandbox(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "image_digest": _VALID_DIGEST,
        "non_root": True,
        "read_only_rootfs": True,
        "no_docker_socket": True,
        "no_network": True,
    }
    base.update(overrides)
    return base


class TestPrebuiltSandboxValidation:
    def test_compliant_sandbox_passes(self) -> None:
        runner = PrebuiltRunner(_spec(RunnerKind.PREBUILT))
        assert runner._validate_sandbox(_sandbox()) == []

    def test_no_network_false_rejected(self) -> None:
        runner = PrebuiltRunner(_spec(RunnerKind.PREBUILT))
        violations = runner._validate_sandbox(_sandbox(no_network=False))
        assert any("network" in v for v in violations)

    def test_read_only_rootfs_false_rejected(self) -> None:
        runner = PrebuiltRunner(_spec(RunnerKind.PREBUILT))
        violations = runner._validate_sandbox(_sandbox(read_only_rootfs=False))
        assert any("read-only" in v for v in violations)

    def test_non_root_false_rejected(self) -> None:
        runner = PrebuiltRunner(_spec(RunnerKind.PREBUILT))
        violations = runner._validate_sandbox(_sandbox(non_root=False))
        assert any("non-root" in v for v in violations)

    def test_malformed_digest_rejected(self) -> None:
        runner = PrebuiltRunner(_spec(RunnerKind.PREBUILT))
        violations = runner._validate_sandbox(_sandbox(image_digest="sha256:abc123"))
        assert any("digest" in v for v in violations)

    def test_missing_digest_rejected(self) -> None:
        runner = PrebuiltRunner(_spec(RunnerKind.PREBUILT))
        violations = runner._validate_sandbox(_sandbox(image_digest=""))
        assert violations != []


class TestKubernetesSandboxValidation:
    def _runner(self) -> KubernetesRunner:
        # 部署配置 digest 与沙箱 pinned digest 刻意不同：manifest image 必须取
        # 沙箱 digest（admission 钉住的对象），不得回退到 runner 部署配置。
        return KubernetesRunner(
            _spec(RunnerKind.KUBERNETES, image_digest="sha256:" + "d" * 64)
        )

    def test_compliant_sandbox_passes(self) -> None:
        assert self._runner()._validate_sandbox_k8s(_sandbox()) == []

    def test_no_network_false_rejected(self) -> None:
        violations = self._runner()._validate_sandbox_k8s(_sandbox(no_network=False))
        assert any("network" in v for v in violations)

    def test_read_only_rootfs_false_rejected(self) -> None:
        violations = self._runner()._validate_sandbox_k8s(
            _sandbox(read_only_rootfs=False)
        )
        assert any("read-only" in v for v in violations)

    def test_malformed_digest_rejected(self) -> None:
        violations = self._runner()._validate_sandbox_k8s(
            _sandbox(image_digest="sha256:abc123")
        )
        assert any("digest" in v for v in violations)

    def test_manifest_image_is_sandbox_pinned_digest(self) -> None:
        runner = self._runner()
        request = RunnerInvocationRequest(
            invocation_id=uuid4(),
            tool_name="tool",
            tool_type="mcp_tool",
            input_args={},
            sandbox_spec=_sandbox(),
            timeout_seconds=30,
            idempotency_key="k",
        )
        manifest = runner._build_job_manifest(request)
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == _VALID_DIGEST
        assert container["image"] != "sha256:" + "d" * 64
