"""S2-T7 集成夹具：真实 redis-server（源码构建，127.0.0.1 随机端口）。"""

from __future__ import annotations

import shutil
import socket
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

REDIS_BIN_CANDIDATES = (
    Path("/tmp/opencode/redis-build/redis-7.2.5/src/redis-server"),
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def redis_server() -> Generator[tuple[str, subprocess.Popen[bytes], Path], None, None]:
    """启动真实 redis-server；二进制缺失则跳过（SSE/Redis 契约需真实实例）。"""
    binary = next((c for c in REDIS_BIN_CANDIDATES if c.exists()), None)
    if binary is None and shutil.which("redis-server"):
        binary = Path(str(shutil.which("redis-server")))
    if binary is None:
        pytest.skip("redis-server binary unavailable (build via docs/handoffs/s2.md §8)")
    port = _free_port()
    proc = subprocess.Popen(
        [str(binary), "--port", str(port), "--save", "", "--appendonly", "no",
         "--bind", "127.0.0.1", "--daemonize", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    import time

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            if proc.poll() is not None:
                raise RuntimeError("redis-server exited immediately") from None
            time.sleep(0.05)
    else:
        proc.terminate()
        raise RuntimeError("redis-server did not become ready")
    yield f"redis://127.0.0.1:{port}/0", proc, Path(f"/tmp/opencode/redis-test-{port}")
    proc.terminate()
    proc.wait(timeout=10)
