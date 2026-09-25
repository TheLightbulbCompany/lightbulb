import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { AgentHeartbeatSection } from "../AgentHeartbeatSection";

const post = vi.fn().mockResolvedValue({});
let isAdmin = true;
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => isAdmin }));
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ post, mutate: vi.fn() }) }));
vi.mock("@/lib/api", () => ({ useApi: () => ({ saveWorkspaceFile: vi.fn() }) }));
vi.mock("@/hooks/useWorkspaceFiles", () => ({
  useConfigFile: () => ({ file: null, isLoading: false, error: undefined }),
}));
vi.mock("@/components/chat/FileContentViewer", () => ({
  FileContentViewer: ({ onSave }: { onSave?: unknown }) => (
    <div data-testid="viewer">{onSave ? "editable" : "read-only"}</div>
  ),
}));

const ceo = { id: "a1", name: "CEO", role: "ceo", runtimeConfig: { heartbeat: { enabled: true, intervalSec: 28800 } } };

describe("AgentHeartbeatSection", () => {
  beforeEach(() => {
    isAdmin = true;
    post.mockClear();
  });

  it("shows the timer and lets an admin switch the CEO's heartbeat off", () => {
    render(<AgentHeartbeatSection agentId="a1" agent={ceo} />);
    expect(screen.getByText("On · wakes every 8 h")).toBeTruthy();
    expect(screen.getByTestId("viewer").textContent).toBe("editable");
    fireEvent.click(screen.getByText("Turn off"));
    expect(post).toHaveBeenCalledWith("/agents/a1/heartbeat", { enabled: false });
  });

  it("has no toggle for a specialist and is read-only for a member", () => {
    isAdmin = false;
    render(<AgentHeartbeatSection agentId="a2" agent={{ ...ceo, id: "a2", role: "engineer" }} />);
    expect(screen.queryByText("Turn off")).toBeNull();
    expect(screen.getByTestId("viewer").textContent).toBe("read-only");
  });
});
