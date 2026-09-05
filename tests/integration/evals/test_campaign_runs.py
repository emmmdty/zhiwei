"""S9-T1 GREEN: campaign 子运行复用既有 EvalRun 运行时（不新建第二套 EvalRun）。

验证：4 单位 registry 划分为 2 个子运行、精确覆盖、partial→resume→seal、campaign
完成只认全部子运行 sealed、操作员冻结的 manifest ids 落列且未知引用被 FK 拒绝。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from zhiwei.contracts.canonical import digest_bytes
from zhiwei.evals.campaigns import (
    CampaignStatus,
    CreateCampaignCommand,
    EvalCampaignService,
)
from zhiwei.evals.domain import EvalMode, RegisteredUnit, SampleOutcome, SampleStatus
from zhiwei.evals.runs import EvalFoundationService, EvalStateError, RunPhase
from zhiwei.evals.sealing import EvalSealRefused
from zhiwei.object_store.posix import PosixObjectStore
from zhiwei.persistence.database import create_database_engine, create_session_factory
from zhiwei.persistence.models import ArtifactManifest, EvalCampaign, EvalRun, EvalSample
from zhiwei.persistence.repositories import TenantRepository
from zhiwei.persistence.tenant import TenantContext, tenant_session

REPO_ROOT = Path(__file__).resolve().parents[3]
ADMIN_DSN = os.environ.get(
    "ZHIWEI_TEST_ADMIN_DSN", "postgresql://zhiwei_migrator@127.0.0.1:55432/zhiwei_test"
)
APP_DSN = os.environ.get(
    "ZHIWEI_TEST_APP_DSN", "postgresql://zhiwei_app@127.0.0.1:55432/zhiwei_test"
)
ADMIN_URL = ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
APP_URL = APP_DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
MIGRATION_REVISION = "0013_evals_campaigns"

DatabaseFixture = tuple[
    AsyncEngine,
    async_sessionmaker[AsyncSession],
    TenantContext,
    PosixObjectStore,
]


@pytest.fixture(scope="module", autouse=True)
def migrated_database() -> Iterator[None]:
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", ADMIN_URL)
    config.attributes["database_url"] = ADMIN_URL
    command.upgrade(config, "head")
    yield


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[DatabaseFixture]:
    engine = create_database_engine(APP_URL)
    sessions = create_session_factory(engine)
    organization_id, workspace_id = uuid4(), uuid4()
    context = TenantContext(organization_id=organization_id, workspace_id=workspace_id)
    async with tenant_session(sessions, context) as session:
        repository = TenantRepository(session, context)
        await repository.create_organization(organization_id, status="active")
        await repository.create_workspace(workspace_id, name="S9-T1")
    try:
        yield engine, sessions, context, PosixObjectStore(tmp_path / "objects")
    finally:
        await engine.dispose()


def _unit(index: int) -> RegisteredUnit:
    return RegisteredUnit(sample_id=f"sample-{index}", unit_id=f"unit-{index}")


def _campaign_command(
    units: tuple[RegisteredUnit, ...], child_sizes: tuple[int, ...], **overrides: object
) -> CreateCampaignCommand:
    return CreateCampaignCommand(
        mode=EvalMode.FIXTURE,
        suite_id=uuid4(),
        suite_version=1,
        registered_units=units,
        child_sizes=child_sizes,
        code_digest=digest_bytes(b"campaign code"),
        config_digest=digest_bytes(b"campaign config"),
        schema_digest=digest_bytes(b"campaign schema"),
        **overrides,  # type: ignore[arg-type]
    )


def _outcome(
    unit: RegisteredUnit, status: SampleStatus = SampleStatus.COMPLETED
) -> SampleOutcome:
    return SampleOutcome(unit=unit, status=status, result={"ok": status is SampleStatus.COMPLETED})


def _child_statuses(view: object) -> dict[UUID, RunPhase]:
    return {child.eval_run_id: child.status for child in view.children}  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_campaign_partitions_registry_across_children_with_exact_coverage(
    database: DatabaseFixture,
) -> None:
    _, sessions, context, store = database
    units = tuple(_unit(index) for index in range(1, 5))
    async with tenant_session(sessions, context) as session:
        created = await EvalCampaignService(session, context, store).create(
            _campaign_command(units, (2, 2))
        )
    assert created.unit_count == 4
    assert len(created.eval_run_ids) == 2

    async with tenant_session(sessions, context) as session:
        campaign = await session.get(EvalCampaign, created.campaign_id)
        children = (
            await session.scalars(
                select(EvalRun).where(EvalRun.campaign_id == created.campaign_id)
            )
        ).all()
        samples = (
            await session.scalars(
                select(EvalSample)
                .where(EvalSample.eval_run_id.in_(created.eval_run_ids))
                .order_by(EvalSample.sample_id, EvalSample.unit_id)
            )
        ).all()

    assert campaign is not None
    assert campaign.status == CampaignStatus.RUNNING.value
    assert campaign.unit_count == 4
    assert campaign.schema_version == 1
    assert {child.id for child in children} == set(created.eval_run_ids)
    assert all(child.campaign_id == created.campaign_id for child in children)

    # 精确覆盖：每个注册单位恰好出现在一个子运行里，且按 child_sizes 顺序划分
    child_units: dict[UUID, list[tuple[str, str]]] = {child_id: [] for child_id in created.eval_run_ids}
    for sample in samples:
        child_units[sample.eval_run_id].append((sample.sample_id, sample.unit_id))
    assert [sorted(child_units[child_id]) for child_id in created.eval_run_ids] == [
        [("sample-1", "unit-1"), ("sample-2", "unit-2")],
        [("sample-3", "unit-3"), ("sample-4", "unit-4")],
    ]


@pytest.mark.asyncio
async def test_campaign_child_partial_resume_seal_and_completion_requires_all_children(
    database: DatabaseFixture,
) -> None:
    _, sessions, context, store = database
    units = tuple(_unit(index) for index in range(1, 5))
    async with tenant_session(sessions, context) as session:
        created = await EvalCampaignService(session, context, store).create(
            _campaign_command(units, (2, 2))
        )
    first_child, second_child = created.eval_run_ids
    first_units = units[:2]
    second_units = units[2:]

    async with tenant_session(sessions, context) as session:
        runs = EvalFoundationService(session, context, store)
        await runs.record_outcome(first_child, _outcome(first_units[0]))
        await runs.pause(first_child)

    async with tenant_session(sessions, context) as session:
        service = EvalCampaignService(session, context, store)
        view = await service.status(created.campaign_id)
    assert view.status is CampaignStatus.RUNNING
    statuses = _child_statuses(view)
    assert statuses[first_child] is RunPhase.PARTIAL
    assert statuses[second_child] is RunPhase.RUNNING

    # partial 子运行不能 seal；campaign 在仍有未 sealed 子运行时不能完成
    with pytest.raises(EvalSealRefused, match="terminal"):
        async with tenant_session(sessions, context) as session:
            await EvalFoundationService(session, context, store).seal(
                first_child,
                migration_revision=MIGRATION_REVISION,
                test_report={"status": "partial", "scope": "campaign"},
            )
    with pytest.raises(EvalStateError, match="sealed"):
        async with tenant_session(sessions, context) as session:
            await EvalCampaignService(session, context, store).complete(created.campaign_id)

    # resume 恢复 RUNNING 且 registry 冻结不变
    async with tenant_session(sessions, context) as session:
        await EvalCampaignService(session, context, store).resume_child(
            created.campaign_id, first_child
        )
    async with tenant_session(sessions, context) as session:
        service = EvalCampaignService(session, context, store)
        view = await service.status(created.campaign_id)
        registry = (
            await session.scalars(
                select(EvalSample)
                .where(EvalSample.eval_run_id == first_child)
                .order_by(EvalSample.sample_id, EvalSample.unit_id)
            )
        ).all()
    assert _child_statuses(view)[first_child] is RunPhase.RUNNING
    assert [(row.sample_id, row.unit_id, row.status) for row in registry] == [
        ("sample-1", "unit-1", "completed"),
        ("sample-2", "unit-2", "registered"),
    ]

    async with tenant_session(sessions, context) as session:
        runs = EvalFoundationService(session, context, store)
        await runs.record_outcome(first_child, _outcome(first_units[1]))
        await runs.seal(
            first_child,
            migration_revision=MIGRATION_REVISION,
            test_report={"status": "passed", "scope": "campaign-child-1"},
        )

    async with tenant_session(sessions, context) as session:
        view = await EvalCampaignService(session, context, store).status(created.campaign_id)
    assert view.status is CampaignStatus.PARTIAL

    async with tenant_session(sessions, context) as session:
        runs = EvalFoundationService(session, context, store)
        for unit in second_units:
            await runs.record_outcome(second_child, _outcome(unit))
        await runs.seal(
            second_child,
            migration_revision=MIGRATION_REVISION,
            test_report={"status": "passed", "scope": "campaign-child-2"},
        )

    async with tenant_session(sessions, context) as session:
        service = EvalCampaignService(session, context, store)
        view = await service.complete(created.campaign_id)
    assert view.status is CampaignStatus.COMPLETED

    async with tenant_session(sessions, context) as session:
        campaign = await session.get(EvalCampaign, created.campaign_id)
    assert campaign is not None and campaign.status == CampaignStatus.COMPLETED.value

    # 已完成的 campaign 是终态：拒绝再次完成与子运行 resume
    with pytest.raises(EvalStateError, match="already"):
        async with tenant_session(sessions, context) as session:
            await EvalCampaignService(session, context, store).complete(created.campaign_id)
    with pytest.raises(EvalStateError, match="completed"):
        async with tenant_session(sessions, context) as session:
            await EvalCampaignService(session, context, store).resume_child(
                created.campaign_id, second_child
            )


@pytest.mark.asyncio
async def test_campaign_creation_refuses_a_partition_that_is_not_exact(
    database: DatabaseFixture,
) -> None:
    _, sessions, context, store = database
    with pytest.raises(ValueError, match="cover"):
        async with tenant_session(sessions, context) as session:
            await EvalCampaignService(session, context, store).create(
                _campaign_command(tuple(_unit(index) for index in range(1, 5)), (3,))
            )
    # fail closed：划分被拒后不留任何 campaign 行
    async with tenant_session(sessions, context) as session:
        campaigns = (
            await session.scalars(
                select(EvalCampaign).where(
                    EvalCampaign.organization_id == context.organization_id
                )
            )
        ).all()
    assert campaigns == []


@pytest.mark.asyncio
async def test_campaign_freezes_operator_supplied_manifest_ids_onto_child_runs(
    database: DatabaseFixture,
) -> None:
    _, sessions, context, store = database
    units = (_unit(1), _unit(2))
    async with tenant_session(sessions, context) as session:
        # 占位的 prereg/model binding artifact manifest 行：S9 后续 Task 由
        # prereg/绑定 manifest 的正式上传流程产出；这里直接落行验证冻结引用链。
        manifest_ids: list[UUID] = []
        for kind in ("eval_prereg", "eval_model_binding"):
            manifest = ArtifactManifest(
                id=uuid4(),
                organization_id=context.organization_id,
                workspace_id=context.workspace_id,
                owner_resource_type=kind,
                owner_resource_id=uuid4(),
                object_key=f"{kind}/{uuid4()}.json",
                content_digest=digest_bytes(kind.encode()),
                size_bytes=2,
                media_type="application/json",
                artifact_schema_id=kind,
                schema_version=1,
                classification="PUBLIC",
                retention={},
            )
            session.add(manifest)
            manifest_ids.append(manifest.id)
        await session.flush()
        created = await EvalCampaignService(session, context, store).create(
            _campaign_command(
                units,
                (2,),
                prereg_manifest_id=manifest_ids[0],
                model_manifest_id=manifest_ids[1],
            )
        )
    eval_run_id = created.eval_run_ids[0]

    async with tenant_session(sessions, context) as session:
        children = (
            await session.scalars(
                select(EvalRun).where(EvalRun.campaign_id == created.campaign_id)
            )
        ).all()
        assert all(child.prereg_manifest_id == manifest_ids[0] for child in children)
        assert all(child.model_manifest_id == manifest_ids[1] for child in children)

        runs = EvalFoundationService(session, context, store)
        for unit in units:
            await runs.record_outcome(eval_run_id, _outcome(unit))
        sealed = await runs.seal(
            eval_run_id,
            migration_revision=MIGRATION_REVISION,
            test_report={"status": "passed", "scope": "manifest-freeze"},
        )
    # manifest ids 冻结引用进入 seal 返回值；sealed payload 协议本身不变。
    assert sealed.prereg_manifest_id == manifest_ids[0]
    assert sealed.model_manifest_id == manifest_ids[1]
    assert sealed.campaign_id == created.campaign_id


@pytest.mark.asyncio
async def test_unknown_manifest_id_is_refused_fail_closed(database: DatabaseFixture) -> None:
    _, sessions, context, store = database
    with pytest.raises(IntegrityError):
        async with tenant_session(sessions, context) as session:
            await EvalCampaignService(session, context, store).create(
                _campaign_command(
                    (_unit(1), _unit(2)), (2,), prereg_manifest_id=uuid4()
                )
            )
