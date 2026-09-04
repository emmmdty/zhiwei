"""MCP capability negotiation.

Handles the negotiation of client and server capabilities during the
MCP initialize handshake. Defines which capabilities are supported and
which are negotiated.

S4 spec §4:
- MCP stdio/Streamable HTTP：tools/resources/prompts/roots/elicitation/sampling/tasks
  完整 capability negotiation
- sampling default off
- Discover background forbidden
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class McpCapability(StrEnum):
    """MCP capabilities that can be negotiated."""

    TOOLS = "tools"
    RESOURCES = "resources"
    PROMPTS = "prompts"
    ROOTS = "roots"
    SAMPLING = "sampling"
    ELICITATION = "elicitation"
    TASKS = "tasks"
    LOGGING = "logging"


@dataclass
class ClientCapabilities:
    """Client capabilities sent during initialize.

    Sampling is default off; must be explicitly opted in.
    """

    tools: bool = True
    resources: bool = True
    prompts: bool = True
    roots: bool = True
    sampling: bool = False
    elicitation: bool = False
    tasks: bool = False
    logging: bool = False

    def to_dict(self) -> dict[str, Any]:
        caps: dict[str, Any] = {}
        if self.tools:
            caps["tools"] = {}
        if self.resources:
            caps["resources"] = {}
        if self.prompts:
            caps["prompts"] = {}
        if self.roots:
            caps["roots"] = {}
        if self.sampling:
            caps["sampling"] = {}
        if self.elicitation:
            caps["elicitation"] = {}
        if self.tasks:
            caps["tasks"] = {}
        if self.logging:
            caps["logging"] = {}
        return caps


@dataclass
class ServerCapabilities:
    """Server capabilities received in initialize response."""

    tools: bool = False
    resources: bool = False
    prompts: bool = False
    roots: bool = False
    sampling: bool = False
    elicitation: bool = False
    tasks: bool = False
    logging: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServerCapabilities:
        return cls(
            tools="tools" in data,
            resources="resources" in data,
            prompts="prompts" in data,
            roots="roots" in data,
            sampling="sampling" in data,
            elicitation="elicitation" in data,
            tasks="tasks" in data,
            logging="logging" in data,
        )


def negotiate_capabilities(
    client: ClientCapabilities,
    server: ServerCapabilities,
) -> set[McpCapability]:
    """Negotiate capabilities between client and server.

    Returns the set of capabilities that both client and server support.
    Only capabilities requested by the client AND offered by the server
    are included in the result.
    """
    negotiated: set[McpCapability] = set()

    if client.tools and server.tools:
        negotiated.add(McpCapability.TOOLS)
    if client.resources and server.resources:
        negotiated.add(McpCapability.RESOURCES)
    if client.prompts and server.prompts:
        negotiated.add(McpCapability.PROMPTS)
    if client.roots and server.roots:
        negotiated.add(McpCapability.ROOTS)
    if client.sampling and server.sampling:
        negotiated.add(McpCapability.SAMPLING)
    if client.elicitation and server.elicitation:
        negotiated.add(McpCapability.ELICITATION)
    if client.tasks and server.tasks:
        negotiated.add(McpCapability.TASKS)
    if client.logging and server.logging:
        negotiated.add(McpCapability.LOGGING)

    return negotiated


def is_capability_negotiated(
    capability: McpCapability,
    negotiated: set[McpCapability],
) -> bool:
    """Check if a specific capability was successfully negotiated."""
    return capability in negotiated
