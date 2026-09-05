"""Generic solution-pack file loading and conformance.

App 无关的 pack 声明装载器：解析 pack.yaml / agent.yaml / task_graph.yaml 与可选的
skills/ schemas/ views/ evals/ 声明目录，产出 frozen PackFileBundle。所有失败模式
（未知键、缺字段、重复 id、版本不一致、digest 不匹配、YAML 非映射根）一律在 LOAD
期抛 PackFileError——不取常见默认、不静默跳过（fail-closed 纪律）。

职责边界：
- renderer_ref 只是声明字符串，由 WEB 侧 ViewManifest registry 在运行期解析，
  本模块不做任何 App 特定分支（tests/architecture 的冻结扫描兜底这一约束）；
- eval corpus_ref 只保证是可指址的相对路径声明，语料存在性由 eval 注册层负责；
- skill entry 存在性单独产出 skill_entry_missing issue：声明可先于运行期实现
  落库，实现到位后该 issue 必须为空。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from zhiwei.agents.task_graph import TaskPrimitive
from zhiwei.contracts.canonical import canonical_json, digest_bytes

SUPPORTED_SCHEMA_VERSION = 1

_PACK_FILE = "pack.yaml"
_AGENT_FILE = "agent.yaml"
_TASK_GRAPH_FILE = "task_graph.yaml"
# 声明目录（严格面：只接受 *.yaml 文件）；pack 根下其余条目（runtime 代码、
# 语料资产、README 等）是非声明资产，不进入本装载器的校验面。
_SECTION_DIRS = ("skills", "schemas", "views", "evals")

_PRIMITIVE_NAMES = frozenset(primitive.value for primitive in TaskPrimitive)
# pack_id 词干：去掉尾部 -vN 得到家族词干，agent_id/graph_id 必须以它扩展
# （「{stem}-agent-vN / {stem}-task-graph-vN」是既有 pack 的统一命名约定）。
_PACK_ID_STEM = re.compile(r"-v\d+$")


class PackFileError(RuntimeError):
    """Pack 文件装载失败（fail closed）。detail 携带机器可读上下文。"""

    def __init__(self, message: str, *, detail: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.detail: dict[str, str] = dict(detail or {})


@dataclass(frozen=True, slots=True)
class PackConformanceIssue:
    """Conformance 违例：code 是机器可读的稳定词表，location 定位声明，detail 带上下文。"""

    code: str
    location: str
    detail: str


class _PackFileModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    # 带 schema_version 字段的声明文件在此统一封闭版本：不支持值在 LOAD 期拒绝，
    # 而不是流入下游（check_fields=False：无该字段的声明模型不受影响）。
    @field_validator("schema_version", check_fields=False)
    @classmethod
    def _schema_version_supported(cls, value: int) -> int:
        if value != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {value} (supported: {SUPPORTED_SCHEMA_VERSION})"
            )
        return value


class PackRiskRule(_PackFileModel):
    condition: str = Field(min_length=1)
    risk: str = Field(min_length=1)


class PackRisk(_PackFileModel):
    default: str = Field(min_length=1)
    escalation_rules: tuple[PackRiskRule, ...] = ()


class PackDeclaration(_PackFileModel):
    schema_version: int
    pack_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    version: int = Field(ge=1)
    frozen_at: date
    capabilities: tuple[str, ...] = Field(min_length=1)
    task_primitives: tuple[str, ...] = Field(min_length=1)
    core_deps: tuple[str, ...] = Field(min_length=1)
    source_connectors: tuple[str, ...] = Field(min_length=1)
    risk: PackRisk
    # content_digest 在声明时必须与 pack.yaml 内容（去掉本键）的 canonical JSON
    # 摘要一致；历史 pack 先于该约定，缺省时不校验（声明即校验，不声明不放行新面）。
    content_digest: str | None = None


class AgentInputContract(_PackFileModel):
    required: tuple[str, ...] = ()
    properties: dict[str, Any] = Field(default_factory=dict)


class AgentDeclaration(_PackFileModel):
    schema_version: int
    agent_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    version: int = Field(ge=1)
    frozen_at: date
    input: AgentInputContract = Field(default_factory=AgentInputContract)
    output: dict[str, Any] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)


class TriggerDeclaration(_PackFileModel):
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    events: tuple[str, ...] = Field(min_length=1)


class TaskDeclaration(_PackFileModel):
    id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    description: str = ""
    depends_on: tuple[str, ...] = ()
    condition: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: tuple[str, ...] = ()


class EdgeDeclaration(_PackFileModel):
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)


class TaskGraphDeclaration(_PackFileModel):
    schema_version: int
    graph_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str
    version: int = Field(ge=1)
    frozen_at: date
    triggers: tuple[TriggerDeclaration, ...] = ()
    tasks: tuple[TaskDeclaration, ...] = Field(min_length=1)
    edges: tuple[EdgeDeclaration, ...] = ()


class SkillDeclaration(_PackFileModel):
    skill_id: str = Field(min_length=1)
    description: str
    entry: str = Field(min_length=1)
    inputs_schema_ref: str | None = None
    outputs_schema_ref: str | None = None


class SchemaDeclaration(_PackFileModel):
    schema_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    schema_: dict[str, Any] = Field(alias="schema")


class ViewDeclaration(_PackFileModel):
    view_id: str = Field(min_length=1)
    kind: Literal["input", "result"]
    app_id: str = Field(min_length=1)
    schema_ref: str = Field(min_length=1)
    renderer_ref: str = Field(min_length=1)


class EvalDeclaration(_PackFileModel):
    suite_id: str = Field(min_length=1)
    corpus_ref: str = Field(min_length=1)
    registered_unit_count_hint: int = Field(ge=1)


class PackFileBundle(_PackFileModel):
    pack: PackDeclaration
    agent: AgentDeclaration | None = None
    task_graph: TaskGraphDeclaration | None = None
    skills: tuple[SkillDeclaration, ...] = ()
    schemas: tuple[SchemaDeclaration, ...] = ()
    views: tuple[ViewDeclaration, ...] = ()
    evals: tuple[EvalDeclaration, ...] = ()


ModelT = TypeVar("ModelT", bound=BaseModel)


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PackFileError(
            f"pack file missing: {path.name}",
            detail={"file": path.name, "reason": "missing"},
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PackFileError(
            f"pack file is not valid YAML: {path.name}: {exc}",
            detail={"file": path.name, "reason": "yaml_parse_error"},
        ) from exc
    if not isinstance(raw, dict):
        raise PackFileError(
            f"pack file root must be a mapping: {path.name}",
            detail={"file": path.name, "reason": "non_mapping_root"},
        )
    if not all(isinstance(key, str) for key in raw):
        raise PackFileError(
            f"pack file keys must be strings: {path.name}",
            detail={"file": path.name, "reason": "non_string_key"},
        )
    return raw


def _build_model(model_cls: type[ModelT], data: dict[str, Any], file: str) -> ModelT:
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise PackFileError(
            f"invalid pack declaration: {file}: {exc.error_count()} error(s)",
            detail={
                "file": file,
                "reason": "schema_validation",
                "errors": exc.json(),
            },
        ) from exc


def _digest_normalized(value: Any) -> Any:
    """YAML 时间标量没有 JCS 表示：date/datetime 归一为 ISO 字符串后再摘要。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _digest_normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_digest_normalized(item) for item in value]
    return value


def _pack_content_digest(content: dict[str, Any]) -> str:
    return digest_bytes(canonical_json(_digest_normalized(content)))


def _load_pack_declaration(root: Path) -> PackDeclaration:
    data = _read_mapping(root / _PACK_FILE)
    declared_digest = data.pop("content_digest", None)
    pack = _build_model(PackDeclaration, data, _PACK_FILE)
    if declared_digest is not None:
        if not isinstance(declared_digest, str) or not declared_digest.startswith("sha256:"):
            raise PackFileError(
                f"content_digest must be a sha256-prefixed digest in {_PACK_FILE}",
                detail={"file": _PACK_FILE, "reason": "digest_format"},
            )
        actual = _pack_content_digest(data)
        if declared_digest != actual:
            raise PackFileError(
                f"content_digest mismatch in {_PACK_FILE}",
                detail={
                    "file": _PACK_FILE,
                    "reason": "digest_mismatch",
                    "declared": declared_digest,
                    "actual": actual,
                },
            )
    return pack


def _load_optional_declaration(
    root: Path, filename: str, model_cls: type[ModelT]
) -> ModelT | None:
    if not (root / filename).is_file():
        return None
    return _build_model(model_cls, _read_mapping(root / filename), filename)


def _load_section(root: Path, section: str, model_cls: type[ModelT]) -> tuple[ModelT, ...]:
    directory = root / section
    if not directory.is_dir():
        return ()
    declarations: list[ModelT] = []
    for entry in sorted(directory.iterdir()):
        if entry.is_dir() or entry.suffix != ".yaml":
            raise PackFileError(
                f"unexpected entry in {section}/: {entry.name}",
                detail={"file": f"{section}/{entry.name}", "reason": "unexpected_entry"},
            )
        declarations.append(
            _build_model(model_cls, _read_mapping(entry), f"{section}/{entry.name}")
        )
    return tuple(declarations)


def _ensure_unique(identifiers: list[tuple[str, str]], kind: str) -> None:
    seen: dict[str, str] = {}
    for identifier, source in identifiers:
        if identifier in seen:
            raise PackFileError(
                f"duplicate {kind} id: {identifier}",
                detail={
                    "id": identifier,
                    "kind": kind,
                    "first": seen[identifier],
                    "second": source,
                    "reason": "duplicate_id",
                },
            )
        seen[identifier] = source


def _check_version_alignment(
    pack: PackDeclaration, agent: AgentDeclaration | None, graph: TaskGraphDeclaration | None
) -> None:
    mismatched: list[str] = []
    if agent is not None and agent.version != pack.version:
        mismatched.append(_AGENT_FILE)
    if graph is not None and graph.version != pack.version:
        mismatched.append(_TASK_GRAPH_FILE)
    if mismatched:
        raise PackFileError(
            f"pack version mismatch: {pack.version} declared vs {', '.join(mismatched)}",
            detail={
                "reason": "version_mismatch",
                "pack_version": str(pack.version),
                "files": ",".join(mismatched),
            },
        )


def load_pack_dir(path: Path) -> PackFileBundle:
    """Load and structurally validate a pack declaration directory (fail closed)."""
    root = Path(path)
    pack = _load_pack_declaration(root)
    agent = _load_optional_declaration(root, _AGENT_FILE, AgentDeclaration)
    graph = _load_optional_declaration(root, _TASK_GRAPH_FILE, TaskGraphDeclaration)
    skills = _load_section(root, "skills", SkillDeclaration)
    schemas = _load_section(root, "schemas", SchemaDeclaration)
    views = _load_section(root, "views", ViewDeclaration)
    evals = _load_section(root, "evals", EvalDeclaration)

    _ensure_unique(
        [(task.id, _TASK_GRAPH_FILE) for task in graph.tasks] if graph else [], "task"
    )
    _ensure_unique(
        [(skill.skill_id, f"skills/{skill.skill_id}") for skill in skills], "skill"
    )
    _ensure_unique(
        [(schema.schema_id, f"schemas/{schema.schema_id}") for schema in schemas], "schema"
    )
    _ensure_unique(
        [(view.view_id, f"views/{view.view_id}") for view in views], "view"
    )
    _ensure_unique(
        [(declaration.suite_id, f"evals/{declaration.suite_id}") for declaration in evals],
        "eval suite",
    )
    _check_version_alignment(pack, agent, graph)

    return PackFileBundle(
        pack=pack,
        agent=agent,
        task_graph=graph,
        skills=skills,
        schemas=schemas,
        views=views,
        evals=evals,
    )


def validate_pack_bundle(
    bundle: PackFileBundle, root: Path
) -> tuple[PackConformanceIssue, ...]:
    """Conformance checks over a loaded bundle.

    root 是 pack 声明目录：skill entry 的存在性/逃逸检查必须落在真实文件系统上，
    因此签名显式要求 root——省略它就无法 fail closed 地完成路径检查。
    """
    issues: list[PackConformanceIssue] = []
    schema_ids = {schema.schema_id for schema in bundle.schemas}

    # (a) 声明与任务图都只允许 Core TaskPrimitive 封闭集
    for primitive in bundle.pack.task_primitives:
        if primitive not in _PRIMITIVE_NAMES:
            issues.append(
                PackConformanceIssue(
                    code="unknown_primitive",
                    location=_PACK_FILE,
                    detail=f"declared task primitive {primitive!r} is not a Core TaskPrimitive",
                )
            )
    if bundle.task_graph is not None:
        graph = bundle.task_graph
        task_ids = {task.id for task in graph.tasks}
        for task in graph.tasks:
            if task.type not in _PRIMITIVE_NAMES:
                issues.append(
                    PackConformanceIssue(
                        code="unknown_primitive",
                        location=_TASK_GRAPH_FILE,
                        detail=f"task {task.id!r} uses unknown primitive {task.type!r}",
                    )
                )
            for dependency in task.depends_on:
                if dependency not in task_ids:
                    issues.append(
                        PackConformanceIssue(
                            code="unresolved_ref",
                            location=_TASK_GRAPH_FILE,
                            detail=f"task {task.id!r} depends on unknown task {dependency!r}",
                        )
                    )
        for edge in graph.edges:
            if edge.from_ not in task_ids or edge.to not in task_ids:
                issues.append(
                    PackConformanceIssue(
                        code="unresolved_ref",
                        location=_TASK_GRAPH_FILE,
                        detail=f"edge {edge.from_!r} -> {edge.to!r} references unknown task",
                    )
                )

    # (b) skill entry：相对、无遍历段、不逃逸 pack 目录；存在性单独 code
    resolved_root = root.resolve()
    for skill in bundle.skills:
        location = f"skills/{skill.skill_id}"
        entry = PurePosixPath(skill.entry)
        if entry.is_absolute() or ".." in entry.parts:
            issues.append(
                PackConformanceIssue(
                    code="path_escape",
                    location=location,
                    detail=(
                        f"skill entry {skill.entry!r} must be a pack-relative path "
                        "without traversal segments"
                    ),
                )
            )
            continue
        target = (root / entry).resolve()
        if not target.is_relative_to(resolved_root):
            # 防御性双查：遍历段已拒绝，这里兜符号链接等解析期逃逸
            issues.append(
                PackConformanceIssue(
                    code="path_escape",
                    location=location,
                    detail=f"skill entry {skill.entry!r} resolves outside the pack dir",
                )
            )
        elif not target.is_file():
            issues.append(
                PackConformanceIssue(
                    code="skill_entry_missing",
                    location=location,
                    detail=f"skill entry {skill.entry!r} does not exist under the pack dir",
                )
            )

    # (c) schema/eval 引用必须可解析
    for skill in bundle.skills:
        for field_name, ref in (
            ("inputs_schema_ref", skill.inputs_schema_ref),
            ("outputs_schema_ref", skill.outputs_schema_ref),
        ):
            if ref is not None and ref not in schema_ids:
                issues.append(
                    PackConformanceIssue(
                        code="unresolved_ref",
                        location=f"skills/{skill.skill_id}",
                        detail=f"{field_name} {ref!r} does not resolve to a declared schema",
                    )
                )
    for view in bundle.views:
        if view.schema_ref not in schema_ids:
            issues.append(
                PackConformanceIssue(
                    code="unresolved_ref",
                    location=f"views/{view.view_id}",
                    detail=f"schema_ref {view.schema_ref!r} does not resolve to a declared schema",
                )
            )
    for declaration in bundle.evals:
        corpus = PurePosixPath(declaration.corpus_ref)
        if declaration.corpus_ref == "" or corpus.is_absolute() or ".." in corpus.parts:
            issues.append(
                PackConformanceIssue(
                    code="unresolved_ref",
                    location=f"evals/{declaration.suite_id}",
                    detail=(
                        f"corpus_ref {declaration.corpus_ref!r} is not a resolvable "
                        "relative path declaration"
                    ),
                )
            )

    # (d) pack_id/agent_id/graph_id 跨文件一致性
    stem = _PACK_ID_STEM.sub("", bundle.pack.pack_id)
    if bundle.agent is not None and not bundle.agent.agent_id.startswith(f"{stem}-"):
        issues.append(
            PackConformanceIssue(
                code="id_mismatch",
                location=_AGENT_FILE,
                detail=(
                    f"agent_id {bundle.agent.agent_id!r} does not extend "
                    f"pack_id stem {stem!r}"
                ),
            )
        )
    if bundle.task_graph is not None and not bundle.task_graph.graph_id.startswith(f"{stem}-"):
        issues.append(
            PackConformanceIssue(
                code="id_mismatch",
                location=_TASK_GRAPH_FILE,
                detail=(
                    f"graph_id {bundle.task_graph.graph_id!r} does not extend "
                    f"pack_id stem {stem!r}"
                ),
            )
        )

    return tuple(issues)
