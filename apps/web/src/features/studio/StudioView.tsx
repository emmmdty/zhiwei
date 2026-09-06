// S10-T2 Agent Studio 视图：draft 编辑 + 受约束 Task Graph 编辑器。
//
// 契约（api/agents.py AgentDraft 投影 + 冻结 CAS 语义
// tests/contract/api/test_agents_studio_frozen.py）：
// - 每次保存 PUT 全量 draft + If-Match（客户端恒携带——428 在 UI 不可达）；
//   412 → revision_conflict 横幅 + Reload draft（CAS 前置：先读后写）；
// - validate 防抖实时调用 POST /{id}/validate，issue 按 code/task_id 渲染。
//   capability 的约束执行点是 server 校验器（unknown_capability），UI 只提供
//   声明集建议（datalist），不硬判——§4 最后一段同纪律；
// - Task editor 只允许 13 个 Core primitives、6 型端口词汇与 3 键预算词汇；
//   edges 由 dependencies 派生（server 要求两者严格一致）；环等结构错误由
//   server 构造期拒绝（422），UI 如实呈现且不伪装成功；
// - 13 分区（specs/s10 §3）：Overview/Instructions/Task/Budget/Access 接真实
//   draft 面；Knowledge/Memory/Tools/Triggers/Model/Evidence/Evals 的后端命令
//   未接通——无后端 action 的控件不出现，只留如实占位（fix-B 起占位文案不再
//   引用计划任务号，只声明「无后端命令故无控件」）；Release 是完整发布流
//   （release/ReleasePanel）：readiness、版本 diff、S9 release commands
//   （create/advance/rollback）与不可变 manifest 展示。

import { useCallback, useEffect, useState, type ReactNode } from "react";
import {
  ApiError,
  SessionExpiredError,
  studioApi,
  type AgentDraft,
  type StudioPortType,
  type StudioTaskGraph,
  type StudioTaskNode,
  type StudioValidationIssue,
} from "../../api/client";
import { RefusalNotice } from "../../components/RefusalNotice";
import { StateBanner } from "../../components/StateBanner";
import { ReleasePanel } from "./release/ReleasePanel";

// 13 Core primitives（镜像 src/zhiwei/agents/task_graph.py TaskPrimitive）
const PRIMITIVES = [
  "Intake",
  "Plan",
  "Clarify",
  "Retrieve",
  "Analyze",
  "InvokeTool",
  "Delegate",
  "Verify",
  "RequestApproval",
  "Synthesize",
  "EmitArtifact",
  "WriteMemoryCandidate",
  "Finish",
] as const;

// 端口类型词汇与预算键词汇（镜像 task_graph.py STUDIO_PORT_TYPES/STUDIO_BUDGET_KEYS）
const PORT_TYPES: StudioPortType[] = ["string", "number", "boolean", "object", "array", "ref"];
const BUDGET_KEYS = ["max_model_calls", "max_tokens", "max_usd_micros"] as const;
const BUDGET_LABELS: Record<(typeof BUDGET_KEYS)[number], string> = {
  max_model_calls: "Max model calls",
  max_tokens: "Max tokens",
  max_usd_micros: "Max USD micros",
};

interface StudioViewProps {
  readOnly: boolean;
  onSessionExpired: () => Promise<void>;
}

export function StudioView({ readOnly, onSessionExpired }: StudioViewProps) {
  const [drafts, setDrafts] = useState<AgentDraft[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const load = async () => {
    setLoadError(null);
    try {
      setDrafts(await studioApi.listDrafts());
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      if (e instanceof ApiError && e.status === 403) {
        setForbidden(true);
        return;
      }
      setLoadError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    void load();
  }, []);

  if (forbidden) {
    return (
      <section aria-label="Agent Studio">
        <h2>Agent Studio</h2>
        <StateBanner tone="error" text="Not authorized for Agent Studio (403)." />
      </section>
    );
  }
  if (selectedId) {
    return (
      <DraftEditor
        agentId={selectedId}
        readOnly={readOnly}
        onBack={() => {
          setSelectedId(null);
          void load();
        }}
        onSessionExpired={onSessionExpired}
      />
    );
  }

  return (
    <section aria-label="Agent Studio">
      <h2>Agent Studio</h2>
      {drafts === null ? (
        <StateBanner tone="loading" text="Loading agent drafts…" />
      ) : loadError ? (
        <StateBanner tone="error" text={`Error: ${loadError}`} />
      ) : (
        <>
          {drafts.length > 0 ? (
            <table>
              <thead>
                <tr>
                  <th>Agent</th>
                  <th>Revision</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {drafts.map((draft) => (
                  <tr key={draft.agent_id}>
                    <td>{draft.agent_id}</td>
                    <td>{draft.revision}</td>
                    <td>
                      <button onClick={() => setSelectedId(draft.agent_id)}>Open</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            readOnly && <StateBanner tone="empty" text="No agent drafts." />
          )}
          {!readOnly && <CreateDraftForm onCreated={(id) => setSelectedId(id)} />}
        </>
      )}
    </section>
  );
}

function CreateDraftForm({ onCreated }: { onCreated: (agentId: string) => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [capabilities, setCapabilities] = useState("knowledge.retrieve@1");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    setCreating(true);
    setError(null);
    try {
      const draft = await studioApi.createDraft({
        name: name.trim(),
        description,
        capabilities: capabilities
          .split(",")
          .map((c) => c.trim())
          .filter(Boolean),
      });
      onCreated(draft.agent_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div>
      <h3>New draft</h3>
      <label>
        Name
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        Description
        <input value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <label>
        Declared capabilities
        <input value={capabilities} onChange={(e) => setCapabilities(e.target.value)} />
      </label>
      <button disabled={!name.trim() || creating} onClick={() => void create()}>
        Create draft
      </button>
      {error && <StateBanner tone="error" text={`Error: ${error}`} />}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3>{title}</h3>
      {children}
    </section>
  );
}

// 无后端 action 的分区如实占位（§5：不出现死控件）。文案声明「为什么没有
// 控件」——fix-B（D5）起不再引用计划任务号：T4 已收口，占位的原因是分区
// 的后端命令仍未接通，不是「等待某个任务」。
const PLACEHOLDER_NOTE =
  "S10 placeholder: no backend command is wired for this section yet, so no controls are provided.";

function DeferredSection({ title, note }: { title: string; note: string }) {
  return (
    <Section title={title}>
      <p>{note}</p>
    </Section>
  );
}

function serializeGraph(graph: StudioTaskGraph): StudioTaskGraph {
  // 下发前归一：edges 由 dependencies 派生；空 port 名不下发（校验器按未声明
  // 处理）；预算只保留 3 键词汇内的正整数（UI 约束的最后一道）
  const tasks = graph.tasks.map((node) => ({
    ...node,
    budget: Object.fromEntries(
      BUDGET_KEYS.filter((key) => {
        const value = node.budget[key];
        return typeof value === "number" && Number.isInteger(value) && value > 0;
      }).map((key) => [key, node.budget[key]])
    ),
    input_schema: {
      properties: Object.fromEntries(
        Object.entries(node.input_schema.properties ?? {}).filter(([portName]) => portName)
      ),
    },
    output_schema: {
      properties: Object.fromEntries(
        Object.entries(node.output_schema.properties ?? {}).filter(([portName]) => portName)
      ),
    },
  }));
  return {
    tasks,
    edges: tasks.flatMap(
      (node) => node.dependencies.map((dep) => [dep, node.task_id] as [string, string])
    ),
  };
}

function DraftEditor({
  agentId,
  readOnly,
  onBack,
  onSessionExpired,
}: {
  agentId: string;
  readOnly: boolean;
  onBack: () => void;
  onSessionExpired: () => Promise<void>;
}) {
  const [draft, setDraft] = useState<AgentDraft | null>(null);
  const [etag, setEtag] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [issues, setIssues] = useState<StudioValidationIssue[]>([]);

  const load = useCallback(async () => {
    setError(null);
    try {
      const tagged = await studioApi.getDraft(agentId);
      setDraft(tagged.data);
      setEtag(tagged.etag);
      setConflict(false);
      setStatus(null);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      if (e instanceof ApiError && e.status === 403) {
        setForbidden(true);
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [agentId, onSessionExpired]);

  useEffect(() => {
    void load();
  }, [load]);

  // 实时校验（防抖）：validate 是非阻塞的报告面——422（结构非法中间态）与
  // 瞬时错误都按「无 issue」呈现，不伪装成保存失败；合法性在 release Gate 把关
  const graph = draft?.task_graph ?? null;
  const graphKey = graph ? JSON.stringify(graph) : "";
  useEffect(() => {
    if (!graph || readOnly) return;
    const timer = setTimeout(() => {
      studioApi
        .validate(agentId, serializeGraph(graph))
        .then((result) => setIssues(result.issues))
        .catch(() => setIssues([]));
    }, 400);
    return () => clearTimeout(timer);
  }, [graphKey, agentId, readOnly]);

  const updateGraph = (fn: (g: StudioTaskGraph) => StudioTaskGraph) => {
    setDraft((current) =>
      current
        ? { ...current, task_graph: fn(current.task_graph ?? { tasks: [], edges: [] }) }
        : current
    );
  };

  const updateNode = (taskId: string, fn: (node: StudioTaskNode) => StudioTaskNode) => {
    updateGraph((g) => ({
      ...g,
      tasks: g.tasks.map((node) => (node.task_id === taskId ? fn(node) : node)),
    }));
  };

  const addNode = () => {
    updateGraph((g) => {
      let index = g.tasks.length + 1;
      let taskId = `t${index}`;
      while (g.tasks.some((node) => node.task_id === taskId)) {
        index += 1;
        taskId = `t${index}`;
      }
      return {
        ...g,
        tasks: [
          ...g.tasks,
          {
            task_id: taskId,
            task_type: "Intake",
            dependencies: [],
            required_capability: draft?.capabilities[0] ?? "",
            budget: {},
            input_schema: { properties: {} },
            output_schema: { properties: {} },
          },
        ],
      };
    });
  };

  const removeNode = (taskId: string) => {
    updateGraph((g) => ({
      ...g,
      tasks: g.tasks
        .filter((node) => node.task_id !== taskId)
        .map((node) => ({
          ...node,
          dependencies: node.dependencies.filter((dep) => dep !== taskId),
        })),
    }));
  };

  const moveNode = (taskId: string, offset: -1 | 1) => {
    updateGraph((g) => {
      const index = g.tasks.findIndex((node) => node.task_id === taskId);
      const target = index + offset;
      if (index < 0 || target < 0 || target >= g.tasks.length) return g;
      const tasks = [...g.tasks];
      const [moved] = tasks.splice(index, 1);
      tasks.splice(target, 0, moved);
      return { ...g, tasks };
    });
  };

  const toggleDependency = (taskId: string, dep: string, enabled: boolean) => {
    updateNode(taskId, (node) => ({
      ...node,
      dependencies: enabled
        ? [...node.dependencies, dep]
        : node.dependencies.filter((d) => d !== dep),
    }));
  };

  const setBudgetValue = (taskId: string, key: (typeof BUDGET_KEYS)[number], raw: string) => {
    updateNode(taskId, (node) => {
      const budget = { ...node.budget };
      if (raw === "") {
        delete budget[key];
      } else {
        const value = Number(raw);
        if (Number.isInteger(value) && value > 0) budget[key] = value;
      }
      return { ...node, budget };
    });
  };

  const addPort = (taskId: string, kind: "input" | "output") => {
    updateNode(taskId, (node) => {
      const schema = kind === "input" ? node.input_schema : node.output_schema;
      const properties = { ...(schema.properties ?? {}), "": { type: "string" as StudioPortType } };
      return kind === "input"
        ? { ...node, input_schema: { properties } }
        : { ...node, output_schema: { properties } };
    });
  };

  const setPort = (
    taskId: string,
    kind: "input" | "output",
    previousName: string,
    patch: { name?: string; portType?: StudioPortType }
  ) => {
    updateNode(taskId, (node) => {
      const schema = kind === "input" ? node.input_schema : node.output_schema;
      const entries = Object.entries(schema.properties ?? {});
      const renamed = entries.map(([name, spec]) => {
        if (name !== previousName) return [name, spec] as const;
        const nextSpec = { type: patch.portType ?? spec.type };
        return [patch.name ?? name, nextSpec] as const;
      });
      const properties = Object.fromEntries(renamed) as Record<string, { type: StudioPortType }>;
      return kind === "input"
        ? { ...node, input_schema: { properties } }
        : { ...node, output_schema: { properties } };
    });
  };

  const removePort = (taskId: string, kind: "input" | "output", portName: string) => {
    updateNode(taskId, (node) => {
      const schema = kind === "input" ? node.input_schema : node.output_schema;
      const properties = Object.fromEntries(
        Object.entries(schema.properties ?? {}).filter(([name]) => name !== portName)
      );
      return kind === "input"
        ? { ...node, input_schema: { properties } }
        : { ...node, output_schema: { properties } };
    });
  };

  const save = async () => {
    if (!draft || !etag) return;
    setSaving(true);
    setStatus(null);
    setConflict(false);
    try {
      const tagged = await studioApi.saveDraft(agentId, etag, {
        name: draft.name,
        description: draft.description,
        instructions: draft.instructions,
        capabilities: draft.capabilities,
        task_graph: draft.task_graph ? serializeGraph(draft.task_graph) : undefined,
      });
      setDraft(tagged.data);
      if (tagged.etag) setEtag(tagged.etag);
      setStatus(`Saved revision ${tagged.data.revision}`);
    } catch (e) {
      if (e instanceof SessionExpiredError) return onSessionExpired();
      // 412 → 冲突横幅（reason 文本只在这一个元素渲染，避免重复匹配）；
      // 428 理论不可达（UI 恒发送 If-Match），其余错误如实呈现不静默吞掉
      if (e instanceof ApiError && e.status === 412) {
        setConflict(true);
      } else {
        setStatus(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <section aria-label="Agent Studio">
        <h2>Agent Studio</h2>
        <StateBanner tone="loading" text="Loading draft…" />
      </section>
    );
  }
  if (forbidden) {
    return (
      <section aria-label="Agent Studio">
        <h2>Agent Studio</h2>
        <StateBanner tone="error" text="Not authorized for this draft (403)." />
      </section>
    );
  }
  if (error || !draft) {
    return (
      <section aria-label="Agent Studio">
        <h2>Agent Studio</h2>
        <StateBanner tone="error" text={`Error: ${error ?? "draft unavailable"}`} />
        <button onClick={onBack}>Back</button>
      </section>
    );
  }

  const graphTasks = draft.task_graph?.tasks ?? [];

  return (
    <section aria-label="Agent Studio">
      <h2>Agent Studio</h2>
      {readOnly && <p>Studio is read-only for the auditor role.</p>}
      {conflict && (
        <div role="alert">
          <RefusalNotice
            refusal={{
              reason: "revision_conflict",
              message:
                "Another writer advanced this draft. Reload to get the current revision; unsaved edits will be replaced.",
            }}
          />
          <button onClick={() => void load()}>Reload draft</button>
        </div>
      )}
      {status && <p>{status}</p>}

      <Section title="Overview">
        <p>
          Revision {draft.revision} · {draft.lifecycle}
        </p>
        <label>
          Agent name
          <input
            disabled={readOnly}
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          />
        </label>
        <label>
          Draft description
          <textarea
            disabled={readOnly}
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          />
        </label>
        <p>Agent id: {draft.agent_id}</p>
      </Section>

      <Section title="Instructions">
        <label htmlFor="studio-instructions">Instructions</label>
        <textarea
          id="studio-instructions"
          disabled={readOnly}
          value={draft.instructions}
          onChange={(e) => setDraft({ ...draft, instructions: e.target.value })}
        />
      </Section>

      <DeferredSection title="Knowledge" note={`Knowledge — ${PLACEHOLDER_NOTE}`} />
      <DeferredSection title="Memory" note={`Memory — ${PLACEHOLDER_NOTE}`} />
      <DeferredSection title="Tools" note={`Tools — ${PLACEHOLDER_NOTE}`} />

      <Section title="Task">
        {readOnly ? null : <button onClick={addNode}>Add node</button>}
        <datalist id="studio-declared-capabilities">
          {draft.capabilities.map((capability) => (
            <option key={capability} value={capability} />
          ))}
        </datalist>
        {graphTasks.length === 0 ? (
          <StateBanner tone="empty" text="No tasks drafted yet." />
        ) : (
          graphTasks.map((node, index) => (
            <fieldset key={node.task_id}>
              <legend>
                <strong>{node.task_id}</strong>
              </legend>
              {!readOnly && (
                <span>
                  <button
                    disabled={index === 0}
                    onClick={() => moveNode(node.task_id, -1)}
                    aria-label={`Move up (${node.task_id})`}
                  >
                    ↑
                  </button>
                  <button
                    disabled={index === graphTasks.length - 1}
                    onClick={() => moveNode(node.task_id, 1)}
                    aria-label={`Move down (${node.task_id})`}
                  >
                    ↓
                  </button>
                  <button onClick={() => removeNode(node.task_id)}>Remove node</button>
                </span>
              )}
              <label>
                Task type
                <select
                  disabled={readOnly}
                  value={node.task_type}
                  onChange={(e) =>
                    updateNode(node.task_id, (n) => ({ ...n, task_type: e.target.value }))
                  }
                >
                  {PRIMITIVES.map((primitive) => (
                    <option key={primitive} value={primitive}>
                      {primitive}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Required capability
                <input
                  list="studio-declared-capabilities"
                  disabled={readOnly}
                  value={node.required_capability}
                  onChange={(e) =>
                    updateNode(node.task_id, (n) => ({
                      ...n,
                      required_capability: e.target.value,
                    }))
                  }
                />
              </label>
              {BUDGET_KEYS.map((key) => (
                <label key={key}>
                  {BUDGET_LABELS[key]}
                  <input
                    type="number"
                    min={1}
                    disabled={readOnly}
                    value={node.budget[key] ?? ""}
                    onChange={(e) => setBudgetValue(node.task_id, key, e.target.value)}
                  />
                </label>
              ))}
              <fieldset>
                <legend>Dependencies</legend>
                {graphTasks
                  .filter((other) => other.task_id !== node.task_id)
                  .map((other) => (
                    <label key={other.task_id}>
                      <input
                        type="checkbox"
                        disabled={readOnly}
                        checked={node.dependencies.includes(other.task_id)}
                        onChange={(e) =>
                          toggleDependency(node.task_id, other.task_id, e.target.checked)
                        }
                      />
                      {other.task_id}
                    </label>
                  ))}
                {graphTasks.length <= 1 && <p>No other tasks to depend on.</p>}
              </fieldset>
              <PortEditor
                node={node}
                taskId={node.task_id}
                kind="output"
                readOnly={readOnly}
                onAdd={addPort}
                onSet={setPort}
                onRemove={removePort}
              />
              <PortEditor
                node={node}
                taskId={node.task_id}
                kind="input"
                readOnly={readOnly}
                onAdd={addPort}
                onSet={setPort}
                onRemove={removePort}
              />
            </fieldset>
          ))
        )}
        {issues.length > 0 ? (
          <div role="status">
            <p>Validation issues ({issues.length}):</p>
            <ul>
              {issues.map((issue, index) => (
                <li key={`${issue.task_id}-${issue.code}-${index}`}>
                  {issue.code} — task {issue.task_id} ({issue.field}): {issue.detail}
                </li>
              ))}
            </ul>
          </div>
        ) : graphTasks.length > 0 ? (
          <p>No validation issues.</p>
        ) : null}
      </Section>

      <DeferredSection title="Triggers" note={`Triggers — ${PLACEHOLDER_NOTE}`} />
      <DeferredSection title="Model" note={`Model — ${PLACEHOLDER_NOTE}`} />

      <Section title="Budget">
        {graphTasks.length === 0 ? (
          <StateBanner tone="empty" text="No tasks drafted yet." />
        ) : (
          <table>
            <thead>
              <tr>
                <th>Task</th>
                {BUDGET_KEYS.map((key) => (
                  <th key={key}>{BUDGET_LABELS[key]}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {graphTasks.map((node) => (
                <tr key={node.task_id}>
                  {/* task 前缀：裸 task_id 文本在节点列表已渲染（e2e 以 exact
                      文本定位节点），聚合表不重复裸文本 */}
                  <td>task {node.task_id}</td>
                  {BUDGET_KEYS.map((key) => (
                    <td key={key}>{node.budget[key] ?? "unset"}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p>Per-task budget is edited in the Task section (3-key vocabulary).</p>
      </Section>

      <DeferredSection title="Evidence" note={`Evidence — ${PLACEHOLDER_NOTE}`} />
      <DeferredSection title="Evals" note={`Evals — ${PLACEHOLDER_NOTE}`} />

      <Section title="Access">
        <p>Declared capabilities (nodes may only reference these; the validator enforces):</p>
        <ul>
          {draft.capabilities.map((capability) => (
            <li key={capability}>{capability}</li>
          ))}
        </ul>
      </Section>

      <Section title="Release">
        <ReleasePanel
          agentId={draft.agent_id}
          readOnly={readOnly}
          onSessionExpired={onSessionExpired}
        />
      </Section>

      {/* Save 恒渲染（auditor disabled 呈现只读面）；428 不可达因为 etag 缺失
          时按钮先于请求被禁用 */}
      <button disabled={readOnly || saving || !etag} onClick={() => void save()}>
        Save draft
      </button>
      <button onClick={onBack}>Back to drafts</button>
    </section>
  );
}

function PortEditor({
  node,
  taskId,
  kind,
  readOnly,
  onAdd,
  onSet,
  onRemove,
}: {
  node: StudioTaskNode;
  taskId: string;
  kind: "input" | "output";
  readOnly: boolean;
  onAdd: (taskId: string, kind: "input" | "output") => void;
  onSet: (
    taskId: string,
    kind: "input" | "output",
    previousName: string,
    patch: { name?: string; portType?: StudioPortType }
  ) => void;
  onRemove: (taskId: string, kind: "input" | "output", portName: string) => void;
}) {
  const properties = Object.entries(
    (kind === "input" ? node.input_schema : node.output_schema).properties ?? {}
  );
  const prefix = kind === "output" ? "" : "Input ";
  return (
    <fieldset>
      <legend>{kind === "output" ? "Output ports" : "Input ports"}</legend>
      {properties.map(([portName, spec]) => (
        <span key={portName || "__new__"}>
          <label>
            {prefix}Port name
            <input
              disabled={readOnly}
              value={portName}
              onChange={(e) => onSet(taskId, kind, portName, { name: e.target.value })}
            />
          </label>
          <label>
            {prefix}Port type
            <select
              disabled={readOnly}
              value={spec.type}
              onChange={(e) =>
                onSet(taskId, kind, portName, { portType: e.target.value as StudioPortType })
              }
            >
              {PORT_TYPES.map((portType) => (
                <option key={portType} value={portType}>
                  {portType}
                </option>
              ))}
            </select>
          </label>
          {!readOnly && (
            <button onClick={() => onRemove(taskId, kind, portName)}>Remove port</button>
          )}
        </span>
      ))}
      {!readOnly && (
        <button onClick={() => onAdd(taskId, kind)}>
          {kind === "output" ? "Add output port" : "Add input port"}
        </button>
      )}
    </fieldset>
  );
}
