"""冻结的 DatasetVersion 契约：digest 绑定且不可变。"""

from __future__ import annotations

import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class DatasetVersionSpec(BaseModel):
    """一个已发布的 dataset 版本；content_digest 必须与对象存储中的内容一致。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_id: UUID
    version: int
    content_digest: str
    manifest_id: UUID

    @field_validator("content_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if _DIGEST_PATTERN.fullmatch(value) is None:
            raise ValueError("content_digest must be a lowercase SHA-256 digest")
        return value
