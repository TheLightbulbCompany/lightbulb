/**
 * Background subagents in a room: a port of Hermes's desktop store (v2026.9.14
 * apps/desktop/src/store/subagents.ts) as pure functions, so web and iPhone share one reducer.
 * Upstream rules kept: an unknown status on subagent.complete fails closed; a terminal row ignores
 * later events; a snapshot never appends over newer live text; prune keeps running background rows.
 * Added: retired ids (a pruned id never comes back), and rows keyed per agent.
 */
export type SubagentStatus = "completed" | "failed" | "interrupted" | "queued" | "running";
export type SubagentStreamKind = "progress" | "summary" | "thinking" | "tool";
export interface SubagentStreamEntry { at: number; isError?: boolean; kind: SubagentStreamKind; text: string }
export interface SubagentProgress {
  id: string; agentId: string; parentId: string | null; goal: string; delegationId?: string; model?: string;
  status: SubagentStatus; taskCount: number; taskIndex: number; startedAt: number; updatedAt: number;
  durationSeconds?: number; toolCount?: number; stream: SubagentStreamEntry[]; summary?: string; currentTool?: string;
}
export interface RoomSubagents { items: SubagentProgress[]; retired: string[] }
export const EMPTY_ROOM_SUBAGENTS: RoomSubagents = { items: [], retired: [] };

type Payload = Record<string, unknown>;
const TERMINAL: ReadonlySet<SubagentStatus> = new Set(["completed", "failed", "interrupted"]);
const MAX_STREAM = 24;
const PREVIEW_MAX = 220;
const TOOL_PREVIEW_MAX = 96;
const MAX_RETIRED = 200;

const isStr = (v: unknown): v is string => typeof v === "string";
const str = (v: unknown) => (isStr(v) ? v : "");
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const capitalize = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

function asStatus(v: unknown, terminalEvent: boolean): SubagentStatus {
  if (v === "completed" || v === "failed" || v === "interrupted") return v;
  if (v === "timeout" || v === "error") return "failed";
  if (v === "cancelled" || v === "canceled") return "interrupted";
  if (terminalEvent) return "failed";
  return v === "queued" ? "queued" : "running";
}

function compact(text: string, max = PREVIEW_MAX): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

const toolLabel = (name: string) => name.split("_").filter(Boolean).map(capitalize).join(" ") || name;
const formatTool = (name: string, preview = "") => {
  const snippet = compact(preview, TOOL_PREVIEW_MAX);
  return snippet ? `${toolLabel(name)}("${snippet}")` : toolLabel(name);
};

function appendStream(stream: SubagentStreamEntry[], entry: SubagentStreamEntry): SubagentStreamEntry[] {
  const last = stream.at(-1);
  if (last?.kind === entry.kind && last.text === entry.text && last.isError === entry.isError) return stream;
  return [...stream, entry].slice(-MAX_STREAM);
}

const timeoutSummary = (p: Payload) => (str(p.status) === "timeout" ? `Timed out after ${num(p.duration_seconds) ?? "?"}s` : "");

function streamFromPayload(p: Payload, status: SubagentStatus, eventType: string, at: number): SubagentStreamEntry[] {
  const out: SubagentStreamEntry[] = [];
  const tool = str(p.tool_name);
  const preview = str(p.tool_preview) || str(p.text);
  const text = compact(str(p.text) || preview);
  if (tool) out.push({ at, isError: !!p.error, kind: "tool", text: formatTool(tool, preview) });
  if (eventType === "subagent.progress" && text) out.push({ at, isError: !!p.error, kind: "progress", text });
  if (eventType === "subagent.thinking" && text) out.push({ at, kind: "thinking", text });
  const summary = compact(str(p.summary) || str(p.text) || timeoutSummary(p));
  if (TERMINAL.has(status) && summary) out.push({ at, isError: status === "failed", kind: "summary", text: summary });
  return out;
}

const idOf = (p: Payload) => str(p.subagent_id) || `${str(p.parent_id) || "root"}:${num(p.task_index) ?? 0}:${str(p.goal)}`;

function toProgress(p: Payload, agentId: string, prev: SubagentProgress | undefined, eventType: string, at: number): SubagentProgress {
  const status = asStatus(p.status, eventType === "subagent.complete");
  return {
    id: prev?.id ?? idOf(p),
    agentId,
    parentId: str(p.parent_id) || prev?.parentId || null,
    goal: str(p.goal) || prev?.goal || "Subagent",
    delegationId: str(p.delegation_id) || prev?.delegationId,
    model: str(p.model) || prev?.model,
    status,
    taskCount: num(p.task_count) ?? prev?.taskCount ?? 1,
    taskIndex: num(p.task_index) ?? prev?.taskIndex ?? 0,
    startedAt: prev?.startedAt ?? at,
    updatedAt: at,
    durationSeconds: num(p.duration_seconds) ?? prev?.durationSeconds,
    toolCount: num(p.tool_count) ?? prev?.toolCount,
    stream: streamFromPayload(p, status, eventType, at).reduce(appendStream, prev?.stream ?? []),
    summary: str(p.summary) || timeoutSummary(p) || prev?.summary || undefined,
    currentTool: TERMINAL.has(status) ? undefined : str(p.tool_name) || prev?.currentTool,
  };
}

export function applySubagentEvent(state: RoomSubagents, agentId: string, type: string, payload: Payload, now: number): RoomSubagents {
  const id = idOf(payload);
  if (state.retired.includes(id)) return state;
  const index = state.items.findIndex((i) => i.id === id);
  const prev = index >= 0 ? state.items[index] : undefined;
  if (prev && TERMINAL.has(prev.status)) return state;
  const next = toProgress(payload, agentId, prev, type, now);
  const items = index >= 0 ? state.items.map((i, k) => (k === index ? next : i)) : [...state.items, next];
  return { ...state, items };
}

export function reconcileSubagentSnapshot(state: RoomSubagents, children: Payload[], now: number): RoomSubagents {
  const ids = new Set(children.map((c) => str(c.subagent_id)).filter(Boolean));
  const items = state.items.filter((i) => TERMINAL.has(i.status) || ids.has(i.id));
  for (const child of children) {
    const id = str(child.subagent_id);
    const agentId = str(child.agent_id);
    if (!id || !agentId || state.retired.includes(id)) continue;
    const index = items.findIndex((i) => i.id === id);
    const prev = items[index];
    if (prev && TERMINAL.has(prev.status)) continue;
    const projected = toProgress(child, agentId, prev, "", now);
    projected.startedAt = (num(child.started_at) ?? 0) * 1000 || prev?.startedAt || projected.startedAt;
    projected.updatedAt = prev?.updatedAt ?? projected.startedAt;
    if (prev) projected.stream = prev.stream;
    if (!projected.stream.length && str(child.last_tool)) {
      projected.stream = [{ at: projected.updatedAt, kind: "tool", text: formatTool(str(child.last_tool)) }];
    }
    if (index < 0) items.push(projected);
    else items[index] = JSON.stringify(prev) === JSON.stringify(projected) ? prev : projected;
  }
  const same = items.length === state.items.length && items.every((i, k) => i === state.items[k]);
  return same ? state : { ...state, items };
}

export function pruneFinishedSubagents(state: RoomSubagents, agentId: string): RoomSubagents {
  const gone = state.items.filter((i) => i.agentId === agentId && TERMINAL.has(i.status)).map((i) => i.id);
  if (!gone.length) return state;
  return {
    items: state.items.filter((i) => !gone.includes(i.id)),
    retired: [...state.retired, ...gone].slice(-MAX_RETIRED),
  };
}

export function liveSubagents(state: RoomSubagents, agentId?: string): SubagentProgress[] {
  return state.items.filter((i) => (i.status === "running" || i.status === "queued") && (!agentId || i.agentId === agentId));
}

export function subagentActivity(item: SubagentProgress): string {
  const last = item.stream.at(-1)?.text;
  if (last) return last;
  return item.status === "queued" ? "queued" : "waiting for activity";
}

/** The steer/stop route answers 404 `SUBAGENT_NOT_FOUND` once the child has finished. `ApiError` keeps the code in `body`. */
export function isSubagentGone(e: unknown): boolean {
  return (e as { body?: { code?: unknown } } | null)?.body?.code === "SUBAGENT_NOT_FOUND";
}

/** The `delegation_id` a `delegate_task` tool result carries (object or JSON string), else null. */
export function delegationIdOf(result: unknown): string | null {
  const parse = (v: unknown): unknown => { if (typeof v !== "string") return v; try { return JSON.parse(v); } catch { return null; } };
  const r = parse(result) as { delegation_id?: unknown } | null;
  return r && typeof r.delegation_id === "string" ? r.delegation_id : null;
}
