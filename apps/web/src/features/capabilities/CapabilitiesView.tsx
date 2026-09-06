// S10-T4 Capability Hub Publisher 面（S4 例外契约解锁，docs/handoffs/
// s4-capability-hub-e2e-exception.md + specs/s4 §6）。
//
// 契约（api/capabilities.py / api/connections.py 的真实投影）：
// - 列表/检视只渲染 API 真实返回的字段（classification/risk_level/
//   content_digest/source_url/metadata 逐字；缺席 → unknown）——不发明
//   SBOM/vulnerability 检查项；
// - bind/unbind（builder/非只读角色，published 版本 only——未发布绑定被
//   server 409 拒，机器可读原因原样上浮）；
// - admit/publish（capability_publisher 入口）、suspend/revoke（security_admin
//   入口，非该角色不渲染）；
// - ConnectionsPanel：connection create/status/suspend/revoke。
// 角色显隐只管入口可见性，权限由 server PEP 强制（§4 最后一段）。

import { useCallback, useEffect, useState } from "react";
import { api, SessionExpiredError } from "../../lib/api";
import { ConfirmButton } from "../../components/ConfirmButton";
import { StateBanner } from "../../components/StateBanner";
import { useRefetchOnFocus } from "../../components/useRefetchOnFocus";
import { hasRole, type SessionUser } from "../../lib/session";
import { ConnectionsPanel } from "./ConnectionsPanel";

interface ProviderRecord {
  id: string;
  provider_id: string;
  name: string;
  version: number;
  description: string;
  status: string;
  classification: string;
  source_url: string | null;
  risk_level: string;
  content_digest: string;
}

interface CapabilityVersionRecord {
  id: string;
  capability_type: string;
  name: string;
  version: number;
  status: string;
  risk_level: string;
  content_digest: string;
  test_digest: string | null;
  parent_id: string | null;
  metadata: Record<string, unknown>;
}

interface VersionDiffRecord {
  from_version: number;
  to_version: number;
  content_changed: boolean;
  risk_changed: boolean;
  status_changed: boolean;
}

interface BindingRecord {
  id: string;
  agent_definition_id: string;
  agent_version_id: string;
  capability_version_id: string;
  status: string;
}

interface CapabilitiesViewProps {
  user: SessionUser;
  readOnly: boolean;
  onSessionExpired: () => Promise<void>;
}

export function CapabilitiesView({ user, readOnly, onSessionExpired }: CapabilitiesViewProps) {
  const [providers, setProviders] = useState<ProviderRecord[]>([]);
  const [versions, setVersions] = useState<CapabilityVersionRecord[]>([]);
  const [bindings, setBindings] = useState<BindingRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedProvider, setSelectedProvider] = useState<string | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<string | null>(null);

  const isPublisher = hasRole(user, "capability_publisher");
  const isSecurityAdmin = hasRole(user, "security_admin");
  // bind 入口对所有非只读变更角色开放；更细的 capability_version 绑定权限由
  // server PEP 强制（S4 契约的「Builder 只能绑定 published 版本」是 server 判定）
  const canBind = !readOnly;

  const load = useCallback(async () => {
    try {
      const [p, v, b] = await Promise.all([
        api.get<ProviderRecord[]>("/api/v1/capabilities/providers"),
        api.get<CapabilityVersionRecord[]>("/api/v1/capabilities/versions"),
        api.get<BindingRecord[]>("/api/v1/capabilities/bindings"),
      ]);
      setProviders(p);
      setVersions(v);
      setBindings(b);
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

  if (selectedProvider) {
    return (
      <ProviderDetailView
        providerId={selectedProvider}
        readOnly={readOnly}
        isPublisher={isPublisher}
        isSecurityAdmin={isSecurityAdmin}
        onBack={() => {
          setSelectedProvider(null);
          load();
        }}
        onSessionExpired={onSessionExpired}
      />
    );
  }
  if (selectedVersion) {
    return (
      <VersionDetailView
        versionId={selectedVersion}
        readOnly={readOnly}
        onBack={() => {
          setSelectedVersion(null);
          load();
        }}
        onSessionExpired={onSessionExpired}
      />
    );
  }

  return (
    <section aria-label="Capabilities">
      <h2>Capabilities</h2>
      {loading ? (
        <StateBanner tone="loading" text="Loading capabilities…" />
      ) : error ? (
        <StateBanner tone="error" text={`Error: ${error}`} />
      ) : (
        <>
          {isPublisher && !readOnly && <ImportProviderForm onImported={load} />}
          <h3>Providers</h3>
          {providers.length === 0 ? (
            <StateBanner tone="empty" text="No providers" />
          ) : (
            <table aria-label="Providers">
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Name</th>
                  <th>Version</th>
                  <th>Status</th>
                  <th>Risk</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {providers.map((p) => (
                  <tr key={p.id}>
                    <td>{p.id}</td>
                    <td>{p.name}</td>
                    <td>{p.version}</td>
                    <td>{p.status}</td>
                    <td>{p.risk_level}</td>
                    <td>
                      <button onClick={() => setSelectedProvider(p.id)}>Open</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h3>Capability versions</h3>
          {versions.length === 0 ? (
            <StateBanner tone="empty" text="No capability versions" />
          ) : (
            <table aria-label="Capability versions">
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Type</th>
                  <th>Name</th>
                  <th>Status</th>
                  <th>Risk</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id}>
                    <td>{v.id}</td>
                    <td>{v.capability_type}</td>
                    <td>{v.name}</td>
                    <td>{v.status}</td>
                    <td>{v.risk_level}</td>
                    <td>
                      <button onClick={() => setSelectedVersion(v.id)}>Inspect</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {canBind && <BindForm versions={versions} onBound={load} />}
          <h3>Bindings</h3>
          {bindings.length === 0 ? (
            <StateBanner tone="empty" text="No bindings" />
          ) : (
            <table aria-label="Bindings">
              <thead>
                <tr>
                  <th>Binding</th>
                  <th>Capability version</th>
                  <th>Agent definition</th>
                  <th>Agent version</th>
                  <th>Status</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {bindings.map((b) => (
                  <tr key={b.id}>
                    <td>{b.id}</td>
                    <td>{b.capability_version_id}</td>
                    <td>{b.agent_definition_id}</td>
                    <td>{b.agent_version_id}</td>
                    <td>{b.status}</td>
                    <td>
                      {canBind && (
                        <ConfirmButton
                          label="Unbind"
                          confirmLabel="Confirm unbind"
                          notice="Remove this binding?"
                          onConfirm={() => void unbind(b.id, load, onSessionExpired)}
                        />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <ConnectionsPanel user={user} readOnly={readOnly} onSessionExpired={onSessionExpired} />
        </>
      )}
    </section>
  );
}

async function unbind(
  bindingId: string,
  reload: () => Promise<void>,
  onSessionExpired: () => Promise<void>
): Promise<void> {
  try {
    await api.delete(`/api/v1/capabilities/bindings/${bindingId}`);
    await reload();
  } catch (e) {
    if (e instanceof SessionExpiredError) await onSessionExpired();
  }
}

function ImportProviderForm({ onImported }: { onImported: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <button onClick={() => setOpen(!open)}>Import provider</button>
      {open && (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError(null);
            try {
              await api.post("/api/v1/capabilities/providers", {
                name,
                source_url: sourceUrl || null,
              });
              setOpen(false);
              setName("");
              setSourceUrl("");
              await onImported();
            } catch (err) {
              setError(err instanceof Error ? err.message : String(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          <label>
            Provider name
            <input value={name} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label>
            Source URL
            <input value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} />
          </label>
          <button type="submit" disabled={busy || !name}>
            Register provider
          </button>
          {error && <StateBanner tone="error" text={error} />}
        </form>
      )}
    </>
  );
}

function BindForm({
  versions,
  onBound,
}: {
  versions: CapabilityVersionRecord[];
  onBound: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [capabilityVersionId, setCapabilityVersionId] = useState("");
  const [agentDefinitionId, setAgentDefinitionId] = useState("");
  const [agentVersionId, setAgentVersionId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <>
      <button onClick={() => setOpen(!open)}>Bind capability</button>
      {open && (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError(null);
            try {
              await api.post("/api/v1/capabilities/bindings", {
                capability_version_id: capabilityVersionId,
                agent_definition_id: agentDefinitionId,
                agent_version_id: agentVersionId,
              });
              setOpen(false);
              setCapabilityVersionId("");
              setAgentDefinitionId("");
              setAgentVersionId("");
              await onBound();
            } catch (err) {
              // 409（未发布版本绑定被拒）等机器可读原因原样上浮
              setError(err instanceof Error ? err.message : String(err));
            } finally {
              setBusy(false);
            }
          }}
        >
          <label>
            Capability version id
            <input
              value={capabilityVersionId}
              onChange={(e) => setCapabilityVersionId(e.target.value)}
              required
            />
          </label>
          <label>
            Agent definition id
            <input
              value={agentDefinitionId}
              onChange={(e) => setAgentDefinitionId(e.target.value)}
              required
            />
          </label>
          <label>
            Agent version id
            <input
              value={agentVersionId}
              onChange={(e) => setAgentVersionId(e.target.value)}
              required
            />
          </label>
          <button type="submit" disabled={busy || versions.length === 0}>
            Create binding
          </button>
          {error && <StateBanner tone="error" text={error} />}
        </form>
      )}
    </>
  );
}

function ProviderDetailView({
  providerId,
  readOnly,
  isPublisher,
  isSecurityAdmin,
  onBack,
  onSessionExpired,
}: {
  providerId: string;
  readOnly: boolean;
  isPublisher: boolean;
  isSecurityAdmin: boolean;
  onBack: () => void;
  onSessionExpired: () => Promise<void>;
}) {
  const [provider, setProvider] = useState<ProviderRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setProvider(await api.get<ProviderRecord>(`/api/v1/capabilities/providers/${providerId}`));
      setError(null);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [providerId, onSessionExpired]);

  useEffect(() => {
    load();
  }, [load]);

  const act = async (action: string) => {
    setBusy(true);
    setError(null);
    try {
      setProvider(
        await api.post<ProviderRecord>(`/api/v1/capabilities/providers/${providerId}/actions`, {
          action,
        })
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <StateBanner tone="loading" text="Loading provider…" />;
  if (error && !provider) return <StateBanner tone="error" text={`Error: ${error}`} />;
  if (!provider) return <div>Provider not found</div>;

  return (
    <section aria-label={`Provider ${providerId}`}>
      <button onClick={onBack}>Back</button>
      <h2>Provider</h2>
      {/* 检视元数据逐字（API 投影字段）；缺席字段 → unknown，不发明检查项 */}
      <ul>
        <li>id: {provider.id}</li>
        <li>name: {provider.name}</li>
        <li>version: {provider.version}</li>
        <li>status: {provider.status}</li>
        <li>classification: {provider.classification}</li>
        <li>risk level: {provider.risk_level}</li>
        <li>content digest: {provider.content_digest}</li>
        <li>source url: {provider.source_url ?? "unknown"}</li>
      </ul>
      {error && <StateBanner tone="error" text={error} />}
      <h3>Lifecycle actions</h3>
      {isPublisher && (
        <>
          <button onClick={() => act("admit")} disabled={readOnly || busy}>
            Admit
          </button>
          <button onClick={() => act("publish")} disabled={readOnly || busy}>
            Publish
          </button>
        </>
      )}
      {isSecurityAdmin && (
        <>
          <button onClick={() => act("suspend")} disabled={readOnly || busy}>
            Suspend
          </button>
          <button onClick={() => act("revoke")} disabled={readOnly || busy}>
            Revoke
          </button>
        </>
      )}
    </section>
  );
}

function VersionDetailView({
  versionId,
  readOnly,
  onBack,
  onSessionExpired,
}: {
  versionId: string;
  readOnly: boolean;
  onBack: () => void;
  onSessionExpired: () => Promise<void>;
}) {
  const [version, setVersion] = useState<CapabilityVersionRecord | null>(null);
  const [diff, setDiff] = useState<VersionDiffRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setVersion(await api.get<CapabilityVersionRecord>(`/api/v1/capabilities/versions/${versionId}`));
      setError(null);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [versionId, onSessionExpired]);

  useEffect(() => {
    load();
  }, [load]);

  const showDiff = async () => {
    setError(null);
    try {
      setDiff(
        await api.get<VersionDiffRecord>(`/api/v1/capabilities/versions/${versionId}/diff`)
      );
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  if (loading) return <StateBanner tone="loading" text="Loading capability version…" />;
  if (error && !version) return <StateBanner tone="error" text={`Error: ${error}`} />;
  if (!version) return <div>Capability version not found</div>;

  return (
    <section aria-label={`Capability version ${versionId}`}>
      <button onClick={onBack}>Back</button>
      <h2>Capability version</h2>
      <ul>
        <li>id: {version.id}</li>
        <li>capability type: {version.capability_type}</li>
        <li>name: {version.name}</li>
        <li>version: {version.version}</li>
        <li>status: {version.status}</li>
        <li>risk level: {version.risk_level}</li>
        <li>content digest: {version.content_digest}</li>
        <li>test digest: {version.test_digest ?? "unknown"}</li>
        <li>parent: {version.parent_id ?? "unknown"}</li>
        <li>metadata: {JSON.stringify(version.metadata)}</li>
      </ul>
      <button onClick={showDiff} disabled={readOnly}>
        Show diff
      </button>
      {diff && (
        <ul>
          <li>
            diff: v{diff.from_version} → v{diff.to_version}
          </li>
          <li>content changed: {String(diff.content_changed)}</li>
          <li>risk changed: {String(diff.risk_changed)}</li>
          <li>status changed: {String(diff.status_changed)}</li>
        </ul>
      )}
      {error && <StateBanner tone="error" text={error} />}
    </section>
  );
}
