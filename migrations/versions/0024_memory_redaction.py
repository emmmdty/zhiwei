"""F-R6-05（D6 决议）：memory_records 终态覆写 digest 占位（redaction +
boundary）——canonical_value 列级 UPDATE 授权 + 触发器 carve-out 收窄。

Revision ID: 0024_memory_redaction
Revises: 0023_trigger_watermarks

背景：撤销/删除/TTL 过期后 canonical_value 原文永驻（0012 触发器内容列
永久不可变）——「删除」的字面承诺与实现不一致，遗忘权场景下受控个人
信息驻留存储。

设计要点：

- 触发器重写（CREATE OR REPLACE）：canonical_value 覆写仅允许随
  active→terminal 同一条 UPDATE 发生（终态行仍全拒绝——不存在二次覆写
  窗口），且新值必须匹配 `^redacted:sha256:[0-9a-f]{64}$` 占位形态
  （防借终态转移写入任意内容）；其余内容列不可变守卫逐字保留；
- GRANT UPDATE (canonical_value) 加入列级授权集（test_database
  MUTABLE_COLUMNS 契约同步扩展）；
- 范围口径：redaction 仅覆盖遗忘语义（REVOKED/EXPIRED）；SUPERSEDED
  保留原文（纠正语义，判分器读取）——域层状态机决定，触发器不区分；
- downgrade 恢复 0012 原触发器与授权。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_memory_redaction"
down_revision: str | None = "0023_trigger_watermarks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GUARD = "memory_records_guard"


_NEW_GUARD = f"""
CREATE OR REPLACE FUNCTION {_GUARD}() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $guard$
BEGIN
    IF OLD.status IN ('superseded', 'revoked', 'expired') THEN
        RAISE EXCEPTION 'memory record % is terminal (%)', OLD.id, OLD.status;
    END IF;
    IF NEW.canonical_value IS DISTINCT FROM OLD.canonical_value THEN
        -- F-R6-05（D6）：digest 占位覆写仅限 active→terminal 同一条 UPDATE；
        -- 形态收窄防止借终态转移写入任意内容。SUPERSEDED 的原文保留由域层
        -- 状态机保证（不走本 carve-out）。
        IF NEW.status IN ('revoked', 'expired')
           AND NEW.canonical_value ~ '^redacted:sha256:[0-9a-f]{{64}}$' THEN
            NULL;  -- redaction carve-out
        ELSE
            RAISE EXCEPTION 'memory record % content is immutable', OLD.id;
        END IF;
    END IF;
    IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.scope IS DISTINCT FROM OLD.scope
       OR NEW.scope_subject_id IS DISTINCT FROM OLD.scope_subject_id
       OR NEW.type IS DISTINCT FROM OLD.type
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.key IS DISTINCT FROM OLD.key
       OR NEW.author_ref IS DISTINCT FROM OLD.author_ref
       OR NEW.dedup_hash IS DISTINCT FROM OLD.dedup_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.acl_version IS DISTINCT FROM OLD.acl_version THEN
        RAISE EXCEPTION 'memory record % content is immutable', OLD.id;
    END IF;
    RETURN NEW;
END;
$guard$;
"""


def upgrade() -> None:
    op.execute(_NEW_GUARD)
    op.execute(
        'GRANT UPDATE (canonical_value) ON TABLE memory_records TO zhiwei_app'
    )


def downgrade() -> None:
    op.execute('REVOKE UPDATE (canonical_value) ON TABLE memory_records FROM zhiwei_app')
    # 恢复 0012 原触发器（内容列含 canonical_value 全量不可变）
    op.execute(
        f"""
CREATE OR REPLACE FUNCTION {_GUARD}() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
AS $guard$
BEGIN
    IF OLD.status IN ('superseded', 'revoked', 'expired') THEN
        RAISE EXCEPTION 'memory record % is terminal (%)', OLD.id, OLD.status;
    END IF;
    IF NEW.organization_id IS DISTINCT FROM OLD.organization_id
       OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.scope IS DISTINCT FROM OLD.scope
       OR NEW.scope_subject_id IS DISTINCT FROM OLD.scope_subject_id
       OR NEW.type IS DISTINCT FROM OLD.type
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.key IS DISTINCT FROM OLD.key
       OR NEW.canonical_value IS DISTINCT FROM OLD.canonical_value
       OR NEW.author_ref IS DISTINCT FROM OLD.author_ref
       OR NEW.dedup_hash IS DISTINCT FROM OLD.dedup_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.acl_version IS DISTINCT FROM OLD.acl_version THEN
        RAISE EXCEPTION 'memory record % content is immutable', OLD.id;
    END IF;
    RETURN NEW;
END;
$guard$;
"""
    )
