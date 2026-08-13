"""冻结的 EvalSuiteVersion 契约：注册单位去重排序后不可变。"""

from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from zhiwei.evals.domain import RegisteredUnit, sorted_unique_units

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class EvalSuiteVersionSpec(BaseModel):
    """一个已发布的 suite 版本；注册单位构成运行期 registry 的冻结快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    suite_id: UUID
    version: int
    content_digest: str
    registered_units: tuple[RegisteredUnit, ...]

    @field_validator("registered_units")
    @classmethod
    def normalize_registry(cls, value: tuple[RegisteredUnit, ...]) -> tuple[RegisteredUnit, ...]:
        """注册单位保持稳定排序，重复注册在构造期即拒绝。"""
        return sorted_unique_units(value)

    @field_validator("content_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("content_digest must be a lowercase SHA-256 digest")
        return value
