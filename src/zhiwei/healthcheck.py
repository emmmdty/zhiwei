"""S11 compose healthcheck 探针。

slim 基础镜像没有 curl/wget，健康检查用标准库 urllib 走 HTTP GET；任何非 2xx、
超时、连接失败都以非零退出——`docker compose up --wait` 依赖这个退出码做就绪判定。
只做探测，不读配置、不建数据库连接、不接触 provider。
"""

from __future__ import annotations

import argparse
import sys
import urllib.request

DEFAULT_TIMEOUT = 2.0


def main() -> int:
    parser = argparse.ArgumentParser(description="HTTP 健康探针（stdlib-only）")
    parser.add_argument("url", help="探测目标 URL")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args()
    try:
        with urllib.request.urlopen(args.url, timeout=args.timeout) as response:
            if 200 <= response.status < 300:
                return 0
            print(f"healthcheck: HTTP {response.status}", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"healthcheck: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

