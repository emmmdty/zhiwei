"""S11 reference 参照服务实现（specs/s11 §2/§6）。

三个角色共用一个 FastAPI 应用骨架：
- `mcp`：JSON-RPC POST /mcp（initialize / tools/list / tools/call 的最小真实面），
  使 MCP 客户端可以在真实 HTTP 传输上完成握手——仓内 fake MCP server（内存
  transport）之外的第一条真实链路参照；
- `openapi`：GET /openapi.json + GET /tickets——OpenAPI 能力导入的真实参照面；
- `source`：GET /documents/{id}——知识源连接器同步的 HTTP 文档参照。

/healthz 是 compose healthcheck 消费的就绪端点（元数据-only）。
"""

from __future__ import annotations

import argparse
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request

ROLE = Literal["mcp", "openapi", "source"]

_SERVER = "zhiwei-reference/0.1.0"

# fixture 载荷：确定性内容（无时钟、无随机），保证演示可重放。
_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_ticket",
        "description": "参考工单查询（fixture 数据）",
        "inputSchema": {
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}},
            "required": ["ticket_id"],
        },
    }
]
_MCP_TICKETS: dict[str, dict[str, Any]] = {
    "T-1001": {"status": "open", "title": "fixture 工单一", "assignee": "alice"},
    "T-1002": {"status": "closed", "title": "fixture 工单二", "assignee": "bob"},
}
_OPENAPI_TICKETS: list[dict[str, Any]] = [
    {"id": "T-1001", "status": "open", "title": "fixture 工单一"},
    {"id": "T-1002", "status": "closed", "title": "fixture 工单二"},
]
_SOURCE_DOCS: dict[str, dict[str, str]] = {
    "doc-001": {
        "title": "产品运行手册",
        "content": "知微本地产品的安装与启动遵循 docs/operations/install.md。",
    },
    "doc-002": {
        "title": "事故响应分级",
        "content": "事故按影响面分级；恢复时间属于测量结果，不预先承诺。",
    },
}

_JSONRPC_ERROR_METHOD_NOT_FOUND = -32601


def build_app(role: str) -> FastAPI:
    app = FastAPI(title=f"ZhiWei Reference ({role})", version="0.1.0", docs_url=None, redoc_url=None)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "role": role, "server": _SERVER}

    if role == "mcp":

        @app.post("/mcp")
        async def mcp(request: Request) -> dict[str, Any]:
            body = await request.json()
            method = body.get("method")
            request_id = body.get("id")
            result = _mcp_result(method, body.get("params") or {})
            if result is None:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": _JSONRPC_ERROR_METHOD_NOT_FOUND, "message": method},
                }
            return {"jsonrpc": "2.0", "id": request_id, "result": result}

    elif role == "openapi":

        @app.get("/openapi.json")
        async def openapi_document() -> dict[str, Any]:
            return _openapi_document()

        @app.get("/tickets")
        async def tickets() -> list[dict[str, Any]]:
            return _OPENAPI_TICKETS

    elif role == "source":

        @app.get("/documents/{document_id}")
        async def document(document_id: str) -> dict[str, Any]:
            doc = _SOURCE_DOCS.get(document_id)
            if doc is None:
                raise HTTPException(status_code=404, detail="unknown document")
            return {"id": document_id, **doc}

        @app.get("/index.json")
        async def index() -> list[dict[str, str]]:
            return [{"id": key, "title": value["title"]} for key, value in _SOURCE_DOCS.items()]

    else:  # pragma: no cover - argparse 已收窄取值
        raise ValueError(f"未知 reference 角色: {role}")

    return app


def _mcp_result(method: str | None, params: dict[str, Any]) -> dict[str, Any] | None:
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "zhiwei-reference-mcp", "version": "0.1.0"},
        }
    if method == "tools/list":
        return {"tools": _MCP_TOOLS}
    if method == "tools/call":
        ticket_id = str((params.get("arguments") or {}).get("ticket_id", ""))
        ticket = _MCP_TICKETS.get(ticket_id)
        if ticket is None:
            return {"isError": True, "content": [{"type": "text", "text": "unknown ticket"}]}
        return {
            "content": [{"type": "text", "text": str(ticket)}],
            "structuredContent": ticket,
        }
    return None


def _openapi_document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "ZhiWei Reference OpenAPI", "version": "0.1.0"},
        "servers": [{"url": "/"}],
        "paths": {
            "/tickets": {
                "get": {
                    "operationId": "listTickets",
                    "summary": "参考工单列表（fixture）",
                    "responses": {
                        "200": {
                            "description": "工单列表",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/Ticket"},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Ticket": {
                    "type": "object",
                    "required": ["id", "status", "title"],
                    "properties": {
                        "id": {"type": "string"},
                        "status": {"type": "string", "enum": ["open", "closed"]},
                        "title": {"type": "string"},
                    },
                }
            }
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ZhiWei reference 参照服务")
    parser.add_argument("--role", required=True, choices=["mcp", "openapi", "source"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9101)
    args = parser.parse_args()
    uvicorn.run(build_app(args.role), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
