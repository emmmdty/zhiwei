"""S11 reference 参照服务：MCP / OpenAPI / source 三个本地 HTTP 参照实现。

specs/s11 §2 组件清单的「reference MCP/OpenAPI/source」：三分钟演示与能力绑定的
本地参照目标。全部 fixture 数据、CPU-only、不发外部请求；网络面 internal。
入口：`python -m zhiwei.reference.server --role mcp|openapi|source --port N`。
"""

from __future__ import annotations

from .server import build_app, main

__all__ = ["build_app", "main"]
