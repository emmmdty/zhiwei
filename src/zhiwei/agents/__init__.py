"""S2 agents：AgentDefinition / SolutionPack domain models and version management。

事实源：design doc §3.1（Agent Graph）、S2-T1 plan、ADR-005（version lifecycle）。
"""

from __future__ import annotations

from zhiwei.agents.domain import (
    AgentDefinition,
    AgentDefinitionStatus,
    SolutionPack,
    SolutionPackStatus,
    TaskGraphSchema,
)
from zhiwei.agents.schemas import (
    SchemaNotFoundError,
    SchemaRegistry,
    SchemaValidationError,
    TaskPrimitiveSchema,
)
from zhiwei.agents.solution_packs import PackLoader, PackValidationError
from zhiwei.agents.versions import (
    AgentVersionManager,
    InvalidPackReferenceError,
    InvalidParentReferenceError,
    PackVersionManager,
    VersionStateError,
)

__all__ = [
    "AgentDefinition",
    "AgentDefinitionStatus",
    "AgentVersionManager",
    "InvalidPackReferenceError",
    "InvalidParentReferenceError",
    "PackLoader",
    "PackValidationError",
    "PackVersionManager",
    "SchemaNotFoundError",
    "SchemaRegistry",
    "SchemaValidationError",
    "SolutionPack",
    "SolutionPackStatus",
    "TaskGraphSchema",
    "TaskPrimitiveSchema",
    "VersionStateError",
]
