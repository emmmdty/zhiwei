"""External eval adapter 共享基建：清单、确定性探测与离线完整性执行。

事实源：specs/s7-memory.md §8（external-status 二选一 sealed artifact 契约）、
ADR-013 决策 2、S9 §3（external 分开报告 + scope 标签）。

为什么拆出 base 模块：S9 §2 要求 per-adapter 模块（longmemeval/locomo/bird/
promptfoo/inspect）与 blind/metamorphic 诊断建立在同一套 preflight/manifest
机制上；机制本体在这里单一维护，package `__init__` 只保留向后兼容 re-export。

契约要点：

- 清单登记于 `config/evals/external_adapters.yaml`（已审查档案库）；未知 adapter 名
  fail closed（LookupError），清单 schema 之外的字段拒绝（extra=forbid），路径必须是
  不越界的相对路径。
- 可用性探测是**确定性的本地检查**：许可文件 / version 文件 / 数据目录 / 数据文件的
  存在性。仓库当前没有任何外部基准数据 → 探测结果为 unavailable，并给出
  机器可读的缺失原因（缺什么、期望路径在哪）。探测绝不联网——数据由 operator
  在部署处放置，适配器永远不静默下载。
- available 分支「实际运行 adapter」的离线语义是**完整性执行**（corpus-integrity）：
  逐文件 checksum、逐行 JSONL schema 校验、question 计数。它**不产生质量分数**——
  外部诊断分数需要 live 模型（operator 显式触发），因此无论探测结果如何，
  外部基准质量 claim 都保持 `planned/unavailable`，不得用内部 fixture 替代
  外部诊断或写成已验证。
- 诊断 scope 隔离：每个探测结果与完整性执行产物都携带 `external_diagnostic:*`
  标签；内部 suite 的名字不是合法诊断 scope，两类报告不得混写（specs/s9 §3）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from zhiwei.contracts.canonical import canonical_json, digest_bytes

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_MANIFEST_PATH = REPO_ROOT / "config" / "evals" / "external_adapters.yaml"

AVAILABLE = "available"
UNAVAILABLE = "unavailable"

# 离线完整执行的 run_kind 标记：sealed artifact 里显式声明本 artifact 不是质量诊断。
CORPUS_INTEGRITY_RUN = "corpus-integrity"

# 诊断 scope 标签（specs/s9 §3 层级：internal frozen / blind holdout / external
# diagnostic / metamorphic）。诊断机制只允许产出这三种前缀的 scope——内部 suite
# 名不得冒充诊断 scope，诊断报告也不得混入内部 suite 报告。
EXTERNAL_DIAGNOSTIC_SCOPE = "external_diagnostic"
BLIND_HOLDOUT_SCOPE = "blind_holdout"
METAMORPHIC_SCOPE = "metamorphic"
DIAGNOSTIC_SCOPES = frozenset(
    {EXTERNAL_DIAGNOSTIC_SCOPE, BLIND_HOLDOUT_SCOPE, METAMORPHIC_SCOPE}
)


def diagnostic_scope(kind: str, name: str) -> str:
    """构造诊断 scope 标签；kind 必须是已声明的诊断类（fail closed）。"""
    if kind not in DIAGNOSTIC_SCOPES:
        raise ValueError(f"未知诊断 scope 类型: {kind!r}")
    if not name:
        raise ValueError("诊断 scope 需要非空名称")
    return f"{kind}:{name}"


def ensure_diagnostic_scope(scope: str) -> str:
    """校验 scope 是诊断标签；内部 suite 名等非诊断 scope 一律拒绝。"""
    kind = scope.partition(":")[0]
    if kind not in DIAGNOSTIC_SCOPES:
        raise ValueError(f"scope 不是诊断标签，禁止混入诊断报告: {scope!r}")
    return scope


class ExternalAdapterSpec(BaseModel):
    """清单里一个 external adapter 的显式声明（extra=forbid：schema 外字段拒绝）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    benchmark: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    data_dir: str = Field(min_length=1)
    data_glob: str = Field(min_length=1)
    license_file: str = Field(min_length=1)
    version_file: str = Field(min_length=1)
    required_fields: tuple[str, ...] = Field(min_length=1)

    @field_validator("data_dir", "license_file", "version_file")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"adapter 路径必须是不越界的仓库相对路径: {value!r}")
        return value


@dataclass(frozen=True, slots=True)
class AdapterProbe:
    """一次确定性探测的结果：状态 + 机器可读原因 + 命中的本地文件。"""

    adapter: str
    benchmark: str
    claim_id: str
    scope: str
    status: str
    reasons: tuple[dict[str, str], ...]
    data_files: tuple[Path, ...]
    license_file: Path | None
    version_file: Path | None
    version: str | None


def load_adapter_manifest(path: Path | None = None) -> tuple[ExternalAdapterSpec, ...]:
    """加载 adapter 清单；schema 漂移、重复名、路径越界一律 fail closed。"""
    manifest_path = path or DEFAULT_MANIFEST_PATH
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("adapters"), list):
        raise ValueError(f"{manifest_path}: 清单必须是含 adapters 列表的对象")
    specs: list[ExternalAdapterSpec] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw["adapters"]):
        try:
            spec = ExternalAdapterSpec.model_validate(entry)
        except ValueError as exc:
            raise ValueError(f"{manifest_path}: adapters[{index}] 不符合清单 schema: {exc}") from exc
        if spec.name in seen:
            raise ValueError(f"{manifest_path}: adapter 名重复: {spec.name}")
        seen.add(spec.name)
        specs.append(spec)
    if not specs:
        raise ValueError(f"{manifest_path}: adapter 清单为空")
    return tuple(specs)


@cache
def _load_default_registry() -> dict[str, ExternalAdapterSpec]:
    return {spec.name: spec for spec in load_adapter_manifest()}


def external_adapter_names() -> frozenset[str]:
    """已登记的 external adapter 名集合（探测确定性来自清单，进程内缓存）。"""
    return frozenset(_load_default_registry())


# 供 CLI/测试引用的稳定只读视图。
EXTERNAL_ADAPTER_NAMES: frozenset[str] = external_adapter_names()


def resolve_external_adapter(name: str) -> ExternalAdapterSpec:
    """按名解析 adapter；未知名称 fail closed（LookupError）。"""
    try:
        return _load_default_registry()[name]
    except KeyError:
        raise LookupError(f"未知 external adapter: {name}") from None


def probe_adapter(spec: ExternalAdapterSpec, *, root: Path = REPO_ROOT) -> AdapterProbe:
    """确定性本地探测：清单声明的许可/version/数据文件是否真实存在。

    本地文件不存在即 unavailable——不猜测、不降级、不取「常见默认」。
    reasons 每条携带 {code, path, detail}，path 为仓库相对 POSIX 路径（机器可读）。
    """
    base = root / spec.data_dir
    license_path = root / spec.license_file
    version_path = root / spec.version_file

    reasons: list[dict[str, str]] = []
    if not license_path.is_file():
        reasons.append(
            {
                "code": "missing_file",
                "path": spec.license_file,
                "detail": "数据许可文件缺失",
            }
        )
    if not version_path.is_file():
        reasons.append(
            {
                "code": "missing_file",
                "path": spec.version_file,
                "detail": "数据版本文件缺失",
            }
        )
    if not base.is_dir():
        reasons.append(
            {
                "code": "missing_data_dir",
                "path": spec.data_dir,
                "detail": "数据目录不存在",
            }
        )
    data_files: tuple[Path, ...] = ()
    if base.is_dir():
        found = sorted(path for path in base.glob(spec.data_glob) if path.is_file())
        if not found:
            reasons.append(
                {
                    "code": "no_data_files",
                    "path": spec.data_dir,
                    "detail": f"数据目录内无匹配 {spec.data_glob} 的数据文件",
                }
            )
        data_files = tuple(found)

    version_text: str | None = None
    if version_path.is_file():
        version_text = version_path.read_text(encoding="utf-8").strip()

    status = AVAILABLE if not reasons else UNAVAILABLE
    return AdapterProbe(
        adapter=spec.name,
        benchmark=spec.benchmark,
        claim_id=spec.claim_id,
        scope=diagnostic_scope(EXTERNAL_DIAGNOSTIC_SCOPE, spec.name),
        status=status,
        reasons=tuple(reasons),
        data_files=data_files,
        license_file=license_path if license_path.is_file() else None,
        version_file=version_path if version_path.is_file() else None,
        version=version_text,
    )


def run_available_adapter(
    spec: ExternalAdapterSpec, probe: AdapterProbe, *, root: Path = REPO_ROOT
) -> dict[str, Any]:
    """对 available 的 adapter 实际执行离线完整性运行（corpus-integrity）。

    - 未探测到 available 时拒绝执行（fail closed）；
    - 逐数据文件计算 sha256/size/行数，逐行解析 JSON 并校验清单声明的必需字段，
      任何违例抛 RuntimeError（带 file:line 定位）——数据不完整就不产出密封 artifact；
    - 输出确定性的 checksum 报告（排序后聚合），两次执行逐字节一致；
    - 产物携带 external_diagnostic scope 标签，sealed 时与内部 suite 报告分开。
    """
    if probe.status != AVAILABLE:
        raise RuntimeError(
            f"external adapter {spec.name} 状态为 {probe.status}，拒绝运行（fail closed）"
        )
    if probe.license_file is None or probe.version_file is None or probe.version is None:
        raise RuntimeError(f"external adapter {spec.name} 许可/version 缺失，拒绝运行")

    license_bytes = probe.license_file.read_bytes()
    files: list[dict[str, Any]] = []
    question_count = 0
    for data_file in probe.data_files:
        relative = data_file.relative_to(root).as_posix()
        content = data_file.read_bytes()
        line_count = 0
        for line_no, line in enumerate(
            content.decode("utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise RuntimeError(f"{relative}:{line_no}: 数据行不是合法 JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise RuntimeError(f"{relative}:{line_no}: 数据行必须是 JSON object")
            missing = [field for field in spec.required_fields if field not in row]
            if missing:
                raise RuntimeError(
                    f"{relative}:{line_no}: 数据行缺少必需字段 {missing}"
                )
            line_count += 1
        files.append(
            {
                "path": relative,
                "sha256": digest_bytes(content),
                "size_bytes": len(content),
                "record_count": line_count,
            }
        )
        question_count += line_count

    total_checksum = digest_bytes(canonical_json(files))
    return {
        "run_kind": CORPUS_INTEGRITY_RUN,
        "scope": probe.scope,
        "benchmark": spec.benchmark,
        "license": {
            "path": probe.license_file.relative_to(root).as_posix(),
            "sha256": digest_bytes(license_bytes),
            "size_bytes": len(license_bytes),
        },
        "version": {
            "path": probe.version_file.relative_to(root).as_posix(),
            "content": probe.version,
        },
        "data": {
            "data_dir": spec.data_dir,
            "data_glob": spec.data_glob,
            "files": files,
            "record_count": question_count,
            "total_checksum": total_checksum,
        },
    }
