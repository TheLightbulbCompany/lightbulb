// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";

// --- Mocks -------------------------------------------------------------------
// useAgents is fully stubbed so each test drives roster/settled state directly.
// ChatPanel imports BOTH useAgents and agentDisplayName from this module, so the
// mock must export both.
interface FakeAgent {
  id: string;
  name?: string;
  identity?: { name?: string };
}
let agentsState: { agents: FakeAgent[]; defaultId: string | undefined; isSettled: boolean };
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => agentsState,
  agentDisplayName: (a: FakeAgent) => a.identity?.name || a.name || a.id,
}));

// The GLOBAL /teams/agents roster read (pause/resume state).
let teamsAgentsData:
  | { agents?: { id: string; status?: string | null }[] }
  | undefined;
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read: () => ({ data: teamsAgentsData }) }),
}));

const pause = vi.fn(async () => {});
const resume = vi.fn(async () => {});
vi.mock("@/components/teams/agents/hooks/useAgentMutations", () => ({
  useAgentMutations: () => ({ pause, resume }),
}));

const openPanel = vi.fn();
vi.mock("../workspaceController", () => ({
  openPanel: (...args: unknown[]) => openPanel(...args),
}));

// Stub the heavy chat window down to a probe that exposes the agentId it was
// handed plus a button that exercises the onOpenFile bridge.
vi.mock("@/components/chat/AgentChatWindow", () => ({
  AgentChatWindow: ({
    agentId,
    onOpenFile,
  }: {
    agentId: string | null;
    onOpenFile?: (path: string) => void;
  }) => (
    <div data-testid="agent-chat" data-agent-id={agentId ?? ""}>
      <button data-testid="open-file" onClick={() => onOpenFile?.("dir/file.txt")}>
        open
      </button>
    </div>
  ),
}));

import { ChatPanel } from "../panels/ChatPanel";

type ChatPanelProps = React.ComponentProps<typeof ChatPanel>;

function makeProps(agentId: string): ChatPanelProps {
  const api: Record<string, unknown> = { title: "Chat", close: vi.fn() };
  api.setTitle = vi.fn((t: string) => {
    api.title = t;
  });
  return { params: { agentId }, api } as unknown as ChatPanelProps;
}

beforeEach(() => {
  agentsState = { agents: [], defaultId: undefined, isSettled: true };
  teamsAgentsData = undefined;
  openPanel.mockClear();
  pause.mockClear();
  resume.mockClear();
});

describe("ChatPanel", () => {
  it("resolves the default sentinel to the concrete default agent", () => {
    agentsState = {
      agents: [{ id: "agent-ceo", name: "Friday" }],
      defaultId: "agent-ceo",
      isSettled: true,
    };
    const props = makeProps(DEFAULT_AGENT_SENTINEL);
    render(<ChatPanel {...props} />);

    // The resolved concrete id is handed to the chat window...
    expect(screen.getByTestId("agent-chat").getAttribute("data-agent-id")).toBe(
      "agent-ceo",
    );
    // ...and the tab title / header reflect the resolved agent name.
    expect(props.api.setTitle).toHaveBeenCalledWith("Friday");
    expect(screen.getByText("Friday")).toBeTruthy();
  });

  it("does NOT show agent-gone while the roster is still settling", () => {
    // isSettled false ⇒ membership check must not fire even for an unknown id.
    agentsState = { agents: [], defaultId: undefined, isSettled: false };
    const props = makeProps("ghost");
    render(<ChatPanel {...props} />);

    expect(screen.queryByText(/no longer exists/i)).toBeNull();
    const win = screen.getByTestId("agent-chat");
    expect(win.getAttribute("data-agent-id")).toBe("ghost");
  });

  it("shows agent-gone ONLY once settled with an unknown id, and closes on click", () => {
    agentsState = {
      agents: [{ id: "real", name: "Real" }],
      defaultId: "real",
      isSettled: true,
    };
    const props = makeProps("ghost");
    render(<ChatPanel {...props} />);

    expect(screen.getByText(/no longer exists/i)).toBeTruthy();
    // The chat window is not mounted in the gone state.
    expect(screen.queryByTestId("agent-chat")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /close panel/i }));
    expect(props.api.close).toHaveBeenCalledTimes(1);
  });

  it("action cluster opens inspector + tasks via openPanel with pushUrl", () => {
    agentsState = {
      agents: [{ id: "agent-ceo", name: "Friday" }],
      defaultId: "agent-ceo",
      isSettled: true,
    };
    const props = makeProps("agent-ceo");
    render(<ChatPanel {...props} />);

    fireEvent.click(screen.getByTitle("Inspect agent"));
    expect(openPanel).toHaveBeenCalledWith(
      "agent-inspector",
      { agent: "agent-ceo" },
      { pushUrl: true },
    );

    fireEvent.click(screen.getByTitle("Agent tasks"));
    expect(openPanel).toHaveBeenCalledWith(
      "teams-issues",
      { agent: "agent-ceo" },
      { pushUrl: true },
    );
  });

  it("reflects paused status from the global roster read", () => {
    agentsState = {
      agents: [{ id: "agent-ceo", name: "Friday" }],
      defaultId: "agent-ceo",
      isSettled: true,
    };
    teamsAgentsData = { agents: [{ id: "agent-ceo", status: "paused" }] };
    const props = makeProps("agent-ceo");
    render(<ChatPanel {...props} />);

    // Paused ⇒ the toggle offers "Resume".
    expect(screen.getByTitle("Resume agent")).toBeTruthy();
    expect(screen.queryByTitle("Pause agent")).toBeNull();
  });
});
