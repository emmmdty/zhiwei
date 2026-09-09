// S10-T4 Knowledge 管理面（api/knowledge.py 投影，S5 §7）。
//
// 每个控件映射真实端点：list/add/connect/sync/status/versions/acl/disable。
// sync/status 渲染真实投影（SyncResultRecord / SourceStatusRecord 逐字）；
// disable 走两段式确认。错误面：403 → 结构化未授权提示（server-driven），
// 其余错误 → error 态 + Retry（重放同一 GET，offline 恢复路径）。
// stale 处理：窗口 focus 后 refetch（specs/s10 §5）。

import { useCallback, useEffect, useState } from "react";
import { ApiError, api, SessionExpiredError } from "../../lib/api";
import { ConfirmButton } from "../../components/ConfirmButton";
import { StateBanner } from "../../components/StateBanner";
import { useRefetchOnFocus } from "../../components/useRefetchOnFocus";

interface SourceRecord {
  id: string;
  source_type: string;
  connector: string;
  uri: string;
  classification: string;
  status: string;
  version_count: number;
  latest_version_seq: number | null;
  latest_content_digest: string | null;
  acl_allowed_principals: string[];
  acl_denied_principals: string[];
  acl_allowed_groups: string[];
}

interface SyncResultRecord {
  source_id: string;
  sync_status: string;
  versions_created: number;
  versions_marked_stale: number;
  connector: string;
  sync_watermark: string | null;
  error: string | null;
}

interface SourceStatusRecord {
  source_id: string;
  status: string;
  version_seq: number | null;
  content_digest: string | null;
  locator_connector: string | null;
  locator_uri: string | null;
  freshness_state: string;
  acl_allowed: boolean;
  acl_reason: string;
  classification: string;
  score_breakdown: Record<string, unknown>;
}

interface SourceVersionRecord {
  id: string;
  source_object_id: string;
  version_seq: number;
  connector: string;
  uri: string;
  content_digest: string;
  state: string;
  classification: string;
  connector_version: string;
  parser_version: string;
  index_version: string;
}

const CLASSIFICATIONS = ["PUBLIC", "INTERNAL", "CONFIDENTIAL"] as const;

export function KnowledgeView({
  readOnly,
  onSessionExpired,
}: {
  readOnly: boolean;
  onSessionExpired: () => Promise<void>;
}) {
  const [sources, setSources] = useState<SourceRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [sourceType, setSourceType] = useState("");
  const [connector, setConnector] = useState("");
  const [uri, setUri] = useState("");
  const [classification, setClassification] = useState<string>("PUBLIC");
  const [addBusy, setAddBusy] = useState(false);
  const [syncResult, setSyncResult] = useState<SyncResultRecord | null>(null);
  const [statusResult, setStatusResult] = useState<SourceStatusRecord | null>(null);
  const [versionsResult, setVersionsResult] = useState<SourceVersionRecord[] | null>(null);
  const [aclSource, setAclSource] = useState<SourceRecord | null>(null);
  const [allowedPrincipals, setAllowedPrincipals] = useState("");
  const [deniedPrincipals, setDeniedPrincipals] = useState("");
  const [allowedGroups, setAllowedGroups] = useState("");

  const load = useCallback(async () => {
    try {
      setSources(await api.get<SourceRecord[]>("/api/v1/knowledge/sources"));
      setError(null);
      setForbidden(false);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      if (e instanceof ApiError && e.status === 403) {
        setForbidden(true);
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setLoading(false);
    }
  }, [onSessionExpired]);

  useEffect(() => {
    load();
  }, [load]);
  useRefetchOnFocus(load);

  const addSource = async () => {
    setAddBusy(true);
    setError(null);
    try {
      await api.post("/api/v1/knowledge/sources", {
        source_type: sourceType,
        connector,
        uri,
        classification,
      });
      setShowAdd(false);
      setSourceType("");
      setConnector("");
      setUri("");
      await load();
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setAddBusy(false);
    }
  };

  const act = async (sourceId: string, path: "connect" | "disable") => {
    setError(null);
    try {
      await api.post<SourceRecord>(`/api/v1/knowledge/sources/${sourceId}/${path}`);
      await load();
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const sync = async (sourceId: string) => {
    setError(null);
    try {
      setSyncResult(
        await api.post<SyncResultRecord>(`/api/v1/knowledge/sources/${sourceId}/sync`, {
          force: false,
        })
      );
      await load();
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const showStatus = async (sourceId: string) => {
    setError(null);
    try {
      setStatusResult(
        await api.get<SourceStatusRecord>(`/api/v1/knowledge/sources/${sourceId}/status`)
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const showVersions = async (sourceId: string) => {
    setError(null);
    try {
      setVersionsResult(
        await api.get<SourceVersionRecord[]>(`/api/v1/knowledge/sources/${sourceId}/versions`)
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const saveAcl = async () => {
    if (!aclSource) return;
    setError(null);
    try {
      await api.put(`/api/v1/knowledge/sources/${aclSource.id}/acl`, {
        allowed_principals: splitCsv(allowedPrincipals),
        denied_principals: splitCsv(deniedPrincipals),
        allowed_groups: splitCsv(allowedGroups),
      });
      const sourceId = aclSource.id;
      setAclSource(null);
      // ACL 变更改变 status 投影的 acl 判定：同步刷新，避免展示陈旧状态
      setStatusResult(
        await api.get<SourceStatusRecord>(`/api/v1/knowledge/sources/${sourceId}/status`)
      );
      await load();
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  if (loading) return <StateBanner tone="loading" text="Loading knowledge sources…" />;
  if (forbidden) {
    return (
      <section aria-label="Knowledge">
        <h2>Knowledge</h2>
        <StateBanner tone="error" text="Not authorized (403)" />
        <button onClick={() => void load()}>Retry</button>
      </section>
    );
  }

  return (
    <section aria-label="Knowledge">
      <h2>Knowledge</h2>
      {error && (
        <>
          <StateBanner tone="error" text={`Error: ${error}`} />
          {/* offline 恢复路径：Retry 重放同一 GET（spec §5 reconnect） */}
          <button onClick={() => void load()}>Retry</button>
        </>
      )}
      <button onClick={() => setShowAdd(!showAdd)}>Add source</button>
      {showAdd && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void addSource();
          }}
        >
          <label>
            Source type
            <input value={sourceType} onChange={(e) => setSourceType(e.target.value)} required />
          </label>
          <label>
            Connector
            <input value={connector} onChange={(e) => setConnector(e.target.value)} required />
          </label>
          <label>
            URI
            <input value={uri} onChange={(e) => setUri(e.target.value)} required />
          </label>
          <label>
            Classification
            <select value={classification} onChange={(e) => setClassification(e.target.value)}>
              {CLASSIFICATIONS.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={addBusy}>
            Save source
          </button>
        </form>
      )}
      {sources.length === 0 ? (
        <StateBanner tone="empty" text="No sources" />
      ) : (
        <table aria-label="Sources">
          <thead>
            <tr>
              <th>Source</th>
              <th>Type</th>
              <th>Connector</th>
              <th>URI</th>
              <th>Classification</th>
              <th>Status</th>
              <th>Versions</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.id}>
                <td>{s.id}</td>
                <td>{s.source_type}</td>
                <td>{s.connector}</td>
                <td>{s.uri}</td>
                <td>{s.classification}</td>
                <td>{s.status}</td>
                <td>{s.version_count}</td>
                <td>
                  {/* 变更动作对只读会话禁用（auditor 全局只读）；Status/Versions
                      是读路径保持可用。权限由 server PEP 强制。 */}
                  <button onClick={() => void act(s.id, "connect")} disabled={readOnly}>
                    Connect
                  </button>
                  <button onClick={() => void sync(s.id)} disabled={readOnly}>
                    Sync
                  </button>
                  <button onClick={() => void showStatus(s.id)}>Status</button>
                  <button onClick={() => void showVersions(s.id)}>Versions</button>
                  <button
                    onClick={() => {
                      setAclSource(s);
                      setAllowedPrincipals(s.acl_allowed_principals.join(", "));
                      setDeniedPrincipals(s.acl_denied_principals.join(", "));
                      setAllowedGroups(s.acl_allowed_groups.join(", "));
                    }}
                    disabled={readOnly}
                  >
                    ACL
                  </button>
                  <ConfirmButton
                    label="Disable"
                    confirmLabel="Confirm disable"
                    notice="Disable sync for this source?"
                    disabled={readOnly}
                    onConfirm={() => void act(s.id, "disable")}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {syncResult && (
        <ul aria-label="Sync result">
          <li>sync: {syncResult.sync_status}</li>
          <li>versions created: {syncResult.versions_created}</li>
          <li>versions marked stale: {syncResult.versions_marked_stale}</li>
          <li>connector: {syncResult.connector}</li>
          <li>watermark: {syncResult.sync_watermark ?? "unknown"}</li>
          {syncResult.error && <li>error: {syncResult.error}</li>}
        </ul>
      )}

      {statusResult && (
        <ul aria-label="Status result">
          <li>status: {statusResult.status}</li>
          <li>freshness: {statusResult.freshness_state}</li>
          <li>acl allowed: {String(statusResult.acl_allowed)}</li>
          <li>acl reason: {statusResult.acl_reason}</li>
          <li>classification: {statusResult.classification}</li>
          <li>version: {statusResult.version_seq ?? "unknown"}</li>
          <li>digest: {statusResult.content_digest ?? "unknown"}</li>
          <li>score breakdown: {JSON.stringify(statusResult.score_breakdown)}</li>
        </ul>
      )}

      {versionsResult && (
        <table aria-label="Source versions">
          <thead>
            <tr>
              <th>Source</th>
              <th>Seq</th>
              <th>Connector</th>
              <th>State</th>
              <th>Digest</th>
            </tr>
          </thead>
          <tbody>
            {versionsResult.map((v) => (
              <tr key={v.id}>
                <td>{v.source_object_id}</td>
                <td>{v.version_seq}</td>
                <td>{v.connector}</td>
                <td>{v.state}</td>
                <td>{v.content_digest}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {aclSource && (
        <form
          aria-label="ACL editor"
          onSubmit={(e) => {
            e.preventDefault();
            void saveAcl();
          }}
        >
          <p>ACL for {aclSource.id}</p>
          <label>
            Allowed principals
            <input
              value={allowedPrincipals}
              onChange={(e) => setAllowedPrincipals(e.target.value)}
            />
          </label>
          <label>
            Denied principals
            <input value={deniedPrincipals} onChange={(e) => setDeniedPrincipals(e.target.value)} />
          </label>
          <label>
            Allowed groups
            <input value={allowedGroups} onChange={(e) => setAllowedGroups(e.target.value)} />
          </label>
          <button type="submit">Save ACL</button>
        </form>
      )}
    </section>
  );
}

function splitCsv(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}
