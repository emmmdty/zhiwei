"""S6 Case repositories — in-memory implementation for unit tests.

Provides an in-memory CaseRepository that satisfies CaseRepositoryProtocol
for deterministic unit testing without database dependencies.

事实源：S6 spec §4。
"""

from __future__ import annotations

from uuid import UUID

from zhiwei.cases.domain import Case


class InMemoryCaseRepository:
    """In-memory case repository for unit testing."""

    def __init__(self) -> None:
        self._cases: dict[UUID, Case] = {}

    async def get_case(self, case_id: UUID) -> Case | None:
        if isinstance(case_id, str):
            from uuid import UUID as _UUID
            case_id = _UUID(case_id)
        return self._cases.get(case_id)

    async def save_case(self, case: Case) -> Case:
        self._cases[case.id] = case
        return case

    async def list_cases(
        self, *, organization_id: UUID, workspace_id: UUID
    ) -> list[Case]:
        return sorted(
            [
                c
                for c in self._cases.values()
                if c.organization_id == organization_id
                and c.workspace_id == workspace_id
            ],
            key=lambda c: c.created_at,
        )
