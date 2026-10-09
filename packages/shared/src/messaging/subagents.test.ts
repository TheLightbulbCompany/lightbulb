import { describe, expect, it } from "vitest";
import { EMPTY_ROOM_SUBAGENTS, applySubagentEvent, delegationIdOf, liveSubagents, pruneFinishedSubagents, reconcileSubagentSnapshot, subagentActivity } from "./subagents";

const A = "agent-ceo";
const base = { subagent_id: "sa-1", goal: "Map Paperclip", task_index: 0, task_count: 3, delegation_id: "d-1" };

describe("subagent reducer (port of Hermes desktop store/subagents.ts)", () => {
  it("start then tool shows a running row with the tool as activity", () => {
    let s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.start", base, 1000);
    s = applySubagentEvent(s, A, "subagent.tool", { ...base, tool_name: "web_search", tool_preview: "paperclip docs" }, 2000);
    const [row] = liveSubagents(s, A);
    expect(row.status).toBe("running");
    expect(row.startedAt).toBe(1000);
    expect(subagentActivity(row)).toBe('Web Search("paperclip docs")');
  });

  it("complete with unknown status fails closed", () => {
    let s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.start", base, 1000);
    s = applySubagentEvent(s, A, "subagent.complete", { ...base, status: "running" }, 2000);
    expect(s.items[0].status).toBe("failed");
    expect(liveSubagents(s, A)).toEqual([]);
  });

  it("maps timeout to failed with a synthesized summary and cancelled to interrupted", () => {
    const t = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.complete", { ...base, status: "timeout", duration_seconds: 612 }, 1);
    expect(t.items[0].status).toBe("failed");
    expect(t.items[0].summary).toBe("Timed out after 612s");
    const c = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.complete", { ...base, status: "cancelled" }, 1);
    expect(c.items[0].status).toBe("interrupted");
  });

  it("a terminal row ignores later events, and a pruned id never comes back", () => {
    let s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.complete", { ...base, status: "completed" }, 1);
    s = applySubagentEvent(s, A, "subagent.tool", { ...base, tool_name: "x" }, 2);
    expect(s.items[0].status).toBe("completed");
    s = pruneFinishedSubagents(s, A);
    expect(s.items).toEqual([]);
    s = applySubagentEvent(s, A, "subagent.tool", { ...base, tool_name: "x" }, 3);
    expect(s.items).toEqual([]);
  });

  it("prune keeps running background rows and other agents' rows", () => {
    let s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.start", base, 1);
    s = applySubagentEvent(s, A, "subagent.complete", { ...base, subagent_id: "sa-2", status: "completed" }, 2);
    s = applySubagentEvent(s, "agent-fin", "subagent.complete", { ...base, subagent_id: "sa-3", status: "completed" }, 2);
    s = pruneFinishedSubagents(s, A);
    expect(s.items.map((i) => i.id).sort()).toEqual(["sa-1", "sa-3"]);
  });

  it("snapshot seeds cold rows, drops live rows it no longer lists, and never overwrites newer live text", () => {
    let s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.tool", { ...base, tool_name: "read_file", tool_preview: "a.md" }, 5000);
    s = applySubagentEvent(s, A, "subagent.start", { ...base, subagent_id: "gone" }, 5000);
    s = reconcileSubagentSnapshot(s, [
      { ...base, agent_id: A, status: "running", last_tool: "web_search", started_at: 4 },
      { subagent_id: "sa-9", goal: "New", agent_id: A, status: "queued", last_tool: "terminal", started_at: 6 },
    ], 7000);
    const ids = s.items.map((i) => i.id).sort();
    expect(ids).toEqual(["sa-1", "sa-9"]);
    const sa1 = s.items.find((i) => i.id === "sa-1")!;
    expect(subagentActivity(sa1)).toBe('Read File("a.md")');
    expect(sa1.startedAt).toBe(4000);
    expect(subagentActivity(s.items.find((i) => i.id === "sa-9")!)).toBe("Terminal");
  });

  it("a queued row with no activity says queued", () => {
    const s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.start", { ...base, status: "queued" }, 1);
    expect(subagentActivity(s.items[0])).toBe("queued");
  });
});

describe("delegationIdOf", () => {
  it("reads the bridge's ToolResultBlock[] shape (turn.py tool_complete)", () => {
    expect(delegationIdOf([{ type: "text", text: '{"status":"dispatched","delegation_id":"d-1"}' }])).toBe("d-1");
    expect(delegationIdOf([{ type: "image" }, { type: "text", text: '{"delegation_id":"d-3"}' }])).toBe("d-3");
    expect(delegationIdOf([{ type: "text", text: "not json" }])).toBeNull();
    expect(delegationIdOf([])).toBeNull();
  });
  it("still reads a JSON string or an object", () => {
    expect(delegationIdOf('{"delegation_id":"d-1"}')).toBe("d-1");
    expect(delegationIdOf({ delegation_id: "d-2" })).toBe("d-2");
    expect(delegationIdOf("oops")).toBeNull();
  });
});
