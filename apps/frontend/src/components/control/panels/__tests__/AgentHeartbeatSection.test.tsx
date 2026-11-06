import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
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

  const off = { ...ceo, runtimeConfig: { heartbeat: { enabled: false, intervalSec: 28800 } } };

  it.each([
    ["not_entitled", "Heartbeats need an active plan or trial."],
    ["company_not_active", "Your team is still being set up. Try again in a minute."],
    ["heartbeats_disabled", "Heartbeats are turned off for this workspace."],
    ["friday_not_seeded", "Lightbulb isn't set up in this workspace yet."],
  ])("says why turning on did nothing (%s) and stays off", async (status, line) => {
    post.mockResolvedValueOnce({ status });
    render(<AgentHeartbeatSection agentId="a1" agent={off} />);
    fireEvent.click(screen.getByText("Turn on"));
    await waitFor(() => expect(screen.getByText(line)).toBeTruthy());
    expect(screen.getByText("Off")).toBeTruthy();
    expect(screen.getByText("Turn on")).toBeTruthy();
  });

  it("shows a retry line when the request throws", async () => {
    post.mockRejectedValueOnce(new Error("boom"));
    render(<AgentHeartbeatSection agentId="a1" agent={off} />);
    fireEvent.click(screen.getByText("Turn on"));
    await waitFor(() => expect(screen.getByText("Couldn't change the heartbeat. Try again.")).toBeTruthy());
  });

  it("shows no message when it armed", async () => {
    post.mockResolvedValueOnce({ status: "armed" });
    render(<AgentHeartbeatSection agentId="a1" agent={off} />);
    fireEvent.click(screen.getByText("Turn on"));
    await waitFor(() => expect(post).toHaveBeenCalledWith("/agents/a1/heartbeat", { enabled: true }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("has no toggle for a specialist and is read-only for a member", () => {
    isAdmin = false;
    render(<AgentHeartbeatSection agentId="a2" agent={{ ...ceo, id: "a2", role: "engineer" }} />);
    expect(screen.queryByText("Turn off")).toBeNull();
    expect(screen.getByTestId("viewer").textContent).toBe("read-only");
  });
});
