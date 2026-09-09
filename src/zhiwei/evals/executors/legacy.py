"""Legacy executor：逐行校验 CHECKSUMS.sha256，不改动冻结资产。"""

from __future__ import annotations

import hashlib

from zhiwei.evals.domain import RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.legacy_assets import LegacyAssetInventory


class LegacyExecutor:
    """对每个 checksum 注册单位做本地文件哈希校验，无网络、无子进程。"""

    def __init__(self, inventory: LegacyAssetInventory) -> None:
        self._inventory = inventory

    async def execute(self, unit: RegisteredUnit) -> SampleOutcome:
        expected = self._inventory.digest_for(unit)
        path = self._inventory.resolve(unit)
        content = path.read_bytes()
        actual = f"sha256:{hashlib.sha256(content).hexdigest()}"
        verified = actual == expected
        return SampleOutcome(
            unit=unit,
            status=SampleStatus.COMPLETED if verified else SampleStatus.FAILED,
            result={
                "path": unit.sample_id,
                "content_digest": actual,
                "verified": verified,
            },
        )
