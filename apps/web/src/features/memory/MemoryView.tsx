// S10-T4 Memory Center 面（S7 例外契约解锁，docs/handoffs/
// s7-memory-center-e2e-exception.md + specs/s7 §5、ADR-009）。
//
// 契约（api/memory.py 投影）：
// - 列表按 type/status/source 筛选（API 支持的 query 参数，server 过滤）；
// - 状态词汇逐字渲染：candidate/confirmed/superseded/revoked/expired；
// - confirm 入口镜像 api/memory.py confirm_record 的 scope 门禁：team 记录
//   仅 Steward 可见（_STEWARD_ROLE_NAMES），非 team 记录本人即可确认
//   （tests/contract/memory test_confirm_own_candidate 冻结该语义）；server
//   403/409 仍兜底；
// - revoke/correct/delete/export/resolve 全部映射真实端点；delete 返回 204，
//   cascade/tombstone 边界以 refetch 后记录的 status=revoked + tombstone=true
//   呈现（不发明 API 未返回的 cascade 细节）。

import { useCallback, useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { ConfirmButton } from "../../components/ConfirmButton";
import { StateBanner } from "../../components/StateBanner";
import { useRefetchOnFocus } from "../../components/useRefetchOnFocus";
import type { SessionUser } from "../../lib/session";

interface MemoryRecordView {
  id: string;
  version: number;
  scope: string;
  type: string;
  subject: string;
  key: string;
  canonical_value: string;
  source_refs: { source_id: string; source_type: string; description: string }[];
  confidence: number;
  sensitivity: string;
  status: string;
  approver_ref: string | null;
  conflict_refs: string[];
  tombstone: boolean;
}

interface ConflictView {
  conflict_id: string;
  kind: string;
  record_a_id: string;
  record_b_id: string;
  resolved: boolean;
}

// api/memory.py _STEWARD_ROLE_NAMES 的 UI 镜像（confirm 入口显隐；判定仍由
// server 执行）
const STEWARD_ROLE_NAMES = ["memory_steward", "steward", "admin"] as const;

const MEMORY_TYPES = ["preference", "fact", "decision", "episode", "lesson"] as const;
const MEMORY_STATUSES = ["candidate", "confirmed", "superseded", "revoked", "expired"] as const;

export function MemoryView({
  user,
  readOnly,
  onSessionExpired,
}: {
  user: SessionUser;
  readOnly: boolean;
  onSessionExpired: () => Promise<void>;
}) {
  const [records, setRecords] = useState<MemoryRecordView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [isSteward] = useState(
    user.role_bindings.some((b) => (STEWARD_ROLE_NAMES as readonly string[]).includes(b.name))
  );

  const load = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (typeFilter) params.set("type", typeFilter);
      if (statusFilter) params.set("status", statusFilter);
      if (sourceFilter) params.set("source", sourceFilter);
      const query = params.toString();
      setRecords(
        await api.get<MemoryRecordView[]>(`/api/v1/memory/records${query ? `?${query}` : ""}`)
      );
      setError(null);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
    // statusFilter/typeFilter/sourceFilter 变化即触发 server 过滤查询
  }, [typeFilter, statusFilter, sourceFilter, onSessionExpired]);

  useEffect(() => {
    load();
  }, [load]);
  useRefetchOnFocus(load);

  if (selected) {
    return (
      <RecordDetailView
        recordId={selected}
        isSteward={isSteward}
        readOnly={readOnly}
        onBack={() => {
          setSelected(null);
          load();
        }}
        onSessionExpired={onSessionExpired}
      />
    );
  }

  return (
    <section aria-label="Memory">
      <h2>Memory</h2>
      {loading ? (
        <StateBanner tone="loading" text="Loading memory records…" />
      ) : error ? (
        <StateBanner tone="error" text={`Error: ${error}`} />
      ) : (
        <>
          <label>
            Type
            <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
              <option value="">all</option>
              {MEMORY_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <label>
            Status
            <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="">all</option>
              {MEMORY_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <label>
            Source
            <input value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)} />
          </label>

          {records.length === 0 ? (
            <StateBanner tone="empty" text="No memory records" />
          ) : (
            <table aria-label="Records">
              <thead>
                <tr>
                  <th>Record</th>
                  <th>Scope</th>
                  <th>Type</th>
                  <th>Subject</th>
                  <th>Status</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {records.map((r) => (
                  <tr key={r.id}>
                    <td>{r.id}</td>
                    <td>{r.scope}</td>
                    <td>{r.type}</td>
                    <td>{r.subject}</td>
                    <td>{r.status}</td>
                    <td>
                      <button onClick={() => setSelected(r.id)}>Open</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <ConflictsSection onSessionExpired={onSessionExpired} />

          <ExportSection onSessionExpired={onSessionExpired} />
          <StatsSection onSessionExpired={onSessionExpired} />
        </>
      )}
    </section>
  );
}

function RecordDetailView({
  recordId,
  isSteward,
  readOnly,
  onBack,
  onSessionExpired,
}: {
  recordId: string;
  isSteward: boolean;
  readOnly: boolean;
  onBack: () => void;
  onSessionExpired: () => Promise<void>;
}) {
  const [record, setRecord] = useState<MemoryRecordView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [correctedValue, setCorrectedValue] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setRecord(await api.get<MemoryRecordView>(`/api/v1/memory/records/${recordId}`));
      setError(null);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [recordId, onSessionExpired]);

  useEffect(() => {
    load();
  }, [load]);

  const act = async (
    path: "confirm" | "revoke" | "delete",
    body?: Record<string, unknown>
  ) => {
    setBusy(true);
    setError(null);
    try {
      const updated = await api.post<MemoryRecordView | null>(
        `/api/v1/memory/records/${recordId}/${path}`,
        body ?? {}
      );
      if (updated) {
        setRecord(updated);
      } else {
        // delete 返回 204：cascade/tombstone 边界以 refetch 的记录呈现
        await load();
      }
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const correct = async () => {
    setBusy(true);
    setError(null);
    try {
      setRecord(
        await api.post<MemoryRecordView>(`/api/v1/memory/records/${recordId}/correct`, {
          record_id: recordId,
          canonical_value: correctedValue,
        })
      );
      setCorrectedValue("");
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <StateBanner tone="loading" text="Loading memory record…" />;
  if (error && !record) return <StateBanner tone="error" text={`Error: ${error}`} />;
  if (!record) return <div>Memory record not found</div>;

  return (
    <section aria-label={`Memory record ${recordId}`}>
      <button onClick={onBack}>Back</button>
      <h2>Memory record</h2>
      <ul>
        <li>id: {record.id}</li>
        <li>version: {record.version}</li>
        <li>scope: {record.scope}</li>
        <li>type: {record.type}</li>
        <li>subject: {record.subject}</li>
        <li>status: {record.status}</li>
        <li>value: {record.canonical_value}</li>
        <li>confidence: {record.confidence}</li>
        <li>sensitivity: {record.sensitivity}</li>
        <li>approver: {record.approver_ref ?? "unknown"}</li>
        <li>tombstone: {String(record.tombstone)}</li>
        <li>sources: {JSON.stringify(record.source_refs)}</li>
      </ul>
      {error && <StateBanner tone="error" text={error} />}

      {/* confirm：team 记录 steward-only 入口，非 team 记录本人可确认
          （api/memory.py confirm_record 仅对 team scope 做 _STEWARD_ROLE_NAMES
          门禁）；server 仍兜底判定 */}
      {(isSteward || record.scope !== "team") && (
        <button onClick={() => void act("confirm")} disabled={readOnly || busy}>
          Confirm
        </button>
      )}
      {!readOnly && (
        <>
          <ConfirmButton
            label="Revoke"
            confirmLabel="Confirm revoke"
            notice="Revoke this record (tombstone)?"
            onConfirm={() => void act("revoke", { record_id: recordId, reason: "" })}
          />
          <ConfirmButton
            label="Delete"
            confirmLabel="Confirm delete"
            notice="Delete this record (tombstone boundary applies)?"
            onConfirm={() => void act("delete", { record_id: recordId })}
          />
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void correct();
            }}
          >
            <label>
              Corrected value
              <input
                value={correctedValue}
                onChange={(e) => setCorrectedValue(e.target.value)}
                required
              />
            </label>
            <button type="submit" disabled={busy}>
              Submit correction
            </button>
          </form>
        </>
      )}
    </section>
  );
}

function ConflictsSection({ onSessionExpired }: { onSessionExpired: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [conflicts, setConflicts] = useState<ConflictView[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setConflicts(await api.get<ConflictView[]>("/api/v1/memory/conflicts"));
      setError(null);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [onSessionExpired]);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const resolve = async (conflictId: string) => {
    setError(null);
    try {
      await api.post("/api/v1/memory/conflicts/resolve", { conflict_id: conflictId });
      await load();
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <>
      <button onClick={() => setOpen(!open)}>Conflicts</button>
      {open && (
        <>
          {loading ? (
            <StateBanner tone="loading" text="Loading conflicts…" />
          ) : error ? (
            <StateBanner tone="error" text={error} />
          ) : conflicts.length === 0 ? (
            <StateBanner tone="empty" text="No unresolved conflicts" />
          ) : (
            <table aria-label="Conflicts">
              <thead>
                <tr>
                  <th>Conflict</th>
                  <th>Kind</th>
                  <th>Record A</th>
                  <th>Record B</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {conflicts.map((c) => (
                  <tr key={c.conflict_id}>
                    <td>{c.conflict_id}</td>
                    <td>{c.kind}</td>
                    <td>{c.record_a_id}</td>
                    <td>{c.record_b_id}</td>
                    <td>
                      <button onClick={() => void resolve(c.conflict_id)}>Resolve</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </>
  );
}

function ExportSection({ onSessionExpired }: { onSessionExpired: () => Promise<void> }) {
  const [count, setCount] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const exportAll = async () => {
    setError(null);
    try {
      const result = await api.post<{ count: number }>("/api/v1/memory/export", {});
      setCount(result.count);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <div>
      <button onClick={() => void exportAll()}>Export</button>
      {count !== null && <span>exported records: {count}</span>}
      {error && <StateBanner tone="error" text={error} />}
    </div>
  );
}

function StatsSection({ onSessionExpired }: { onSessionExpired: () => Promise<void> }) {
  const [stats, setStats] = useState<{
    total_records: number;
    by_status: Record<string, number>;
    unresolved_conflicts: number;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const loadStats = async () => {
    setError(null);
    try {
      setStats(
        await api.get<{
          total_records: number;
          by_status: Record<string, number>;
          unresolved_conflicts: number;
        }>("/api/v1/memory/stats")
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <div>
      <button onClick={() => void loadStats()}>Stats</button>
      {stats && (
        <ul>
          <li>total records: {stats.total_records}</li>
          <li>by status: {JSON.stringify(stats.by_status)}</li>
          <li>unresolved conflicts: {stats.unresolved_conflicts}</li>
        </ul>
      )}
      {error && <StateBanner tone="error" text={error} />}
    </div>
  );
}
