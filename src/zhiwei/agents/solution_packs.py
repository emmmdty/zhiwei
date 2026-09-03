"""SolutionPack loading and validation.

Validates pack structure, agent references, and dependency chains
before allowing packs to be registered.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from zhiwei.agents.domain import (
    AgentDefinition,
    SolutionPack,
    SolutionPackStatus,
)


class PackValidationError(RuntimeError):
    """Pack validation failed during loading."""


class PackLoader:
    """Validates and loads solution packs.

    Ensures agent references exist and dependencies form a valid chain
    of published packs.
    """

    def __init__(self) -> None:
        self._agents: dict[UUID, AgentDefinition] = {}
        self._packs: dict[UUID, SolutionPack] = {}

    def register_agent(self, agent: AgentDefinition) -> None:
        """Register an agent definition for pack validation."""
        self._agents[agent.id] = agent

    def register_pack(self, pack: SolutionPack) -> None:
        """Register a published pack for dependency validation."""
        self._packs[pack.id] = pack

    def load(self, data: dict[str, Any]) -> SolutionPack:
        """Validate and load a solution pack from a dict.

        Validates required fields, agent reference, and dependency chain.
        """
        try:
            # Filter computed fields that pydantic serializes but cannot accept back
            clean_data = {k: v for k, v in data.items() if k != "content_digest"}
            pack = SolutionPack(**clean_data)
        except Exception as exc:
            raise PackValidationError(f"Invalid pack data: {exc}") from exc

        if pack.agent_definition_id not in self._agents:
            raise PackValidationError(
                f"Agent definition {pack.agent_definition_id} not found"
            )

        for dep_id in pack.depends_on:
            if dep_id not in self._packs:
                raise PackValidationError(
                    f"Dependency pack {dep_id} not found"
                )
            dep_pack = self._packs[dep_id]
            if dep_pack.status != SolutionPackStatus.PUBLISHED:
                raise PackValidationError(
                    f"Dependency pack {dep_id} is not published (status: {dep_pack.status})"
                )

        return pack
