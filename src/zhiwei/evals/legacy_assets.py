"""Legacy 资产适配器：把冻结 CHECKSUMS.sha256 注册为可验证的样本单位。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from zhiwei.evals.domain import RegisteredUnit, unit_sort_key


@dataclass(frozen=True, slots=True)
class LegacyAssetInventory:
    """冻结资产的只读清单：每个样本 = checksum 文件里的一行。

    只适配现有 `evals/` 资产，不迁移、不重写语料。`load(root)` 的 root 是包含
    `CHECKSUMS.sha256` 的目录；清单里的路径以仓库根为基准（`make checksums` 的
    `sha256sum` 输出格式），因此样本文件按 `root.parent / sample_id` 解析。
    digest 映射与仓库根目录属于实现细节，不进入相等性判断。
    """

    registered_units: tuple[RegisteredUnit, ...]
    _root: Path = field(repr=False)
    _digests: dict[tuple[str, str], str] = field(repr=False, default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> LegacyAssetInventory:
        checksum_file = root / "CHECKSUMS.sha256"
        lines = checksum_file.read_text(encoding="utf-8").splitlines()
        units: list[RegisteredUnit] = []
        digests: dict[tuple[str, str], str] = {}
        for line in lines:
            digest, path = line.split(maxsplit=1)
            unit = RegisteredUnit(sample_id=path, unit_id="checksum")
            units.append(unit)
            # 文件里是裸 hex；统一成项目 canonical digest 前缀，与 digest_for 的契约一致。
            digests[(unit.sample_id, unit.unit_id)] = f"sha256:{digest}"
        return cls(
            registered_units=tuple(sorted(units, key=unit_sort_key)),
            _root=root,
            _digests=digests,
        )

    def resolve(self, unit: RegisteredUnit) -> Path:
        """把注册单位解析为仓库内的绝对路径。"""
        if unit.unit_id != "checksum":
            raise ValueError(f"legacy registry only knows checksum units: {unit!r}")
        return self._root.parent / unit.sample_id

    def digest_for(self, unit: RegisteredUnit) -> str:
        """取一个注册单位的期望 digest；未知单位一律拒绝（fail closed）。"""
        key = (unit.sample_id, unit.unit_id)
        if key not in self._digests:
            raise ValueError(f"unit is not part of the legacy asset registry: {unit!r}")
        return self._digests[key]
