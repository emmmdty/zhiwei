// S10-T4 Connection 面（S4 §5 Connection and execution，api/connections.py 投影）。
// open/status/suspend/revoke 全部映射真实端点（open 覆盖 GET /connections/{id}，
// route-coverage.ts 清单的「Open (connection)」映射）；fingerprint/credential_status
// 逐字渲染。suspend/revoke 是 security_admin 入口（非该角色不渲染），只读角色
// 禁用——权限由 server PEP 强制。

import { useCallback, useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { StateBanner } from "../../components/StateBanner";
import { useRefetchOnFocus } from "../../components/useRefetchOnFocus";
import { hasRole, type SessionUser } from "../../lib/session";

interface ConnectionRecord {
  id: string;
  provider_version_id: string;
  subject_mode: string;
  status: string;
  principal_id: string | null;
  version: number;
  fingerprint: string;
}

const SUBJECT_MODES = ["workspace_service", "user_delegated", "service_account"] as const;

export function ConnectionsPanel({
  user,
  readOnly,
  onSessionExpired,
}: {
  user: SessionUser;
  readOnly: boolean;
  onSessionExpired: () => Promise<void>;
}) {
  const [connections, setConnections] = useState<ConnectionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [providerVersionId, setProviderVersionId] = useState("");
  const [subjectMode, setSubjectMode] = useState<string>("workspace_service");
  const [createBusy, setCreateBusy] = useState(false);
  const [statusView, setStatusView] = useState<{ id: string; credentialStatus: string; fingerprint: string } | null>(null);
  const [opened, setOpened] = useState<ConnectionRecord | null>(null);
  const isSecurityAdmin = hasRole(user, "security_admin");

  const open = async (connectionId: string) => {
    setError(null);
    try {
      setOpened(await api.get<ConnectionRecord>(`/api/v1/connections/${connectionId}`));
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const load = useCallback(async () => {
    try {
      setConnections(await api.get<ConnectionRecord[]>("/api/v1/connections"));
      setError(null);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [onSessionExpired]);

  useEffect(() => {
    load();
  }, [load]);
  useRefetchOnFocus(load);

  const create = async () => {
    setCreateBusy(true);
    setError(null);
    try {
      await api.post("/api/v1/connections", {
        provider_version_id: providerVersionId,
        subject_mode: subjectMode,
      });
      setShowCreate(false);
      setProviderVersionId("");
      await load();
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreateBusy(false);
    }
  };

  const act = async (connectionId: string, action: "suspend" | "revoke") => {
    setError(null);
    try {
      await api.post(`/api/v1/connections/${connectionId}/actions`, { action });
      await load();
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const showStatus = async (connectionId: string) => {
    setError(null);
    try {
      const status = await api.get<{
        connection_id: string;
        fingerprint: string;
        credential_status: string;
      }>(`/api/v1/connections/${connectionId}/status`);
      setStatusView({
        id: status.connection_id,
        credentialStatus: status.credential_status,
        fingerprint: status.fingerprint,
      });
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <section aria-label="Connections">
      <h3>Connections</h3>
      {loading ? (
        <StateBanner tone="loading" text="Loading connections…" />
      ) : (
        <>
          {error && <StateBanner tone="error" text={error} />}
          {!readOnly && (
            <>
              <button onClick={() => setShowCreate(!showCreate)}>Create connection</button>
              {showCreate && (
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    void create();
                  }}
                >
                  <label>
                    Provider version id
                    <input
                      value={providerVersionId}
                      onChange={(e) => setProviderVersionId(e.target.value)}
                      required
                    />
                  </label>
                  <label>
                    Subject mode
                    <select value={subjectMode} onChange={(e) => setSubjectMode(e.target.value)}>
                      {SUBJECT_MODES.map((mode) => (
                        <option key={mode} value={mode}>
                          {mode}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button type="submit" disabled={createBusy || !providerVersionId}>
                    Confirm connection
                  </button>
                </form>
              )}
            </>
          )}
          {connections.length === 0 ? (
            <StateBanner tone="empty" text="No connections" />
          ) : (
            <table aria-label="Connections">
              <thead>
                <tr>
                  <th>Connection</th>
                  <th>Provider version</th>
                  <th>Subject mode</th>
                  <th>Status</th>
                  <th>Version</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {connections.map((c) => (
                  <tr key={c.id}>
                    <td>{c.id}</td>
                    <td>{c.provider_version_id}</td>
                    <td>{c.subject_mode}</td>
                    <td>{c.status}</td>
                    <td>{c.version}</td>
                    <td>
                      <button onClick={() => void open(c.id)}>Open</button>
                      <button onClick={() => void showStatus(c.id)}>Status</button>
                      {isSecurityAdmin && (
                        <>
                          <button onClick={() => void act(c.id, "suspend")} disabled={readOnly}>
                            Suspend
                          </button>
                          <button onClick={() => void act(c.id, "revoke")} disabled={readOnly}>
                            Revoke
                          </button>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {opened && (
            <ul aria-label="Connection detail">
              <li>connection: {opened.id}</li>
              <li>status: {opened.status}</li>
              <li>subject mode: {opened.subject_mode}</li>
              <li>provider version: {opened.provider_version_id}</li>
              <li>principal: {opened.principal_id ?? "unknown"}</li>
              <li>version: {opened.version}</li>
              <li>fingerprint: {opened.fingerprint}</li>
            </ul>
          )}
          {statusView && (
            <ul>
              <li>connection: {statusView.id}</li>
              <li>credential: {statusView.credentialStatus}</li>
              <li>fingerprint: {statusView.fingerprint}</li>
            </ul>
          )}
        </>
      )}
    </section>
  );
}
