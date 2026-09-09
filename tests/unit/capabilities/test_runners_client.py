"""RunnerClient IPC secret fail-closed 契约（F-R9-08，与 R2 F-R2-11 同源）。

修复前构造器内置默认 secret（b"default-test-secret"）：部署期若未显式注入，
HMAC 签名可被任何知道该常量的进程伪造。修复后 secret 为必填且拒绝空值——
与 compose secrets 的 operator 覆盖模式同构（缺配置拒绝启动，不取「常见默认」）。
"""

import pytest

from zhiwei.capabilities.runners.client import RunnerClient
from zhiwei.capabilities.runners.contracts import RunnerRegistry


def test_missing_ipc_secret_rejected_at_construction() -> None:
    with pytest.raises(TypeError):
        RunnerClient(RunnerRegistry())  # type: ignore[call-arg]


def test_empty_ipc_secret_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        RunnerClient(RunnerRegistry(), ipc_secret=b"")
