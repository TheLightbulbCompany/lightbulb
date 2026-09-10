// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";

interface FakeAgent {
  id: string;
  name?: string;
}

let activePanelId: string | null;
let agentsState: {
  agents: FakeAgent[];
  defaultId: string | undefined;
};

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => agentsState,
  agentDisplayName: (agent: FakeAgent) => agent.name ?? agent.id,
}));
vi.mock("../workspaceController", () => ({
  useWorkspaceState: () => ({ activePanelId }),
}));
vi.mock("@/components/messaging/MessagingWorkspace", () => ({
  MessagingWorkspace: ({
    initialAgentId,
    subscribeToRoom,
  }: {
    initialAgentId: string | null;
    subscribeToRoom: boolean;
  }) => (
    <div
      data-testid="messaging-workspace"
      data-agent-id={initialAgentId ?? ""}
      data-subscribed={subscribeToRoom}
    />
  ),
}));

import { ChatPanel } from "../panels/ChatPanel";

type ChatPanelProps = React.ComponentProps<typeof ChatPanel>;

function makeProps(agentId: string): ChatPanelProps {
  const api: Record<string, unknown> = {
    id: `chat:${agentId}`,
    title: "Messages",
  };
  api.setTitle = vi.fn((title: string) => {
    api.title = title;
  });
  return { params: { agentId }, api } as unknown as ChatPanelProps;
}

beforeEach(() => {
  activePanelId = null;
  agentsState = { agents: [], defaultId: undefined };
});

describe("ChatPanel", () => {
  it("resolves the default sentinel to the default agent's canonical DM", () => {
    agentsState = {
      agents: [{ id: "agent-ceo", name: "Friday" }],
      defaultId: "agent-ceo",
    };
    const props = makeProps(DEFAULT_AGENT_SENTINEL);

    render(<ChatPanel {...props} />);

    expect(screen.getByTestId("messaging-workspace")).toHaveAttribute(
      "data-agent-id",
      "agent-ceo",
    );
    expect(props.api.setTitle).toHaveBeenCalledWith("Friday");
  });

  it("keeps an explicit agent deep link while the roster catches up", () => {
    const props = makeProps("agent-late");

    render(<ChatPanel {...props} />);

    expect(screen.getByTestId("messaging-workspace")).toHaveAttribute(
      "data-agent-id",
      "agent-late",
    );
    expect(props.api.setTitle).not.toHaveBeenCalled();
  });

  it("gives the one live room subscription to the globally active chat tab", () => {
    const friday = makeProps("agent-friday");
    const atlas = makeProps("agent-atlas");
    activePanelId = "chat:agent-friday";
    const panels = () => (
      <>
        <ChatPanel {...friday} />
        <ChatPanel {...atlas} />
      </>
    );
    const view = render(panels());

    let workspaces = screen.getAllByTestId("messaging-workspace");
    expect(workspaces[0]).toHaveAttribute("data-subscribed", "true");
    expect(workspaces[1]).toHaveAttribute("data-subscribed", "false");

    activePanelId = "chat:agent-atlas";
    view.rerender(panels());
    workspaces = screen.getAllByTestId("messaging-workspace");
    expect(workspaces[0]).toHaveAttribute("data-subscribed", "false");
    expect(workspaces[1]).toHaveAttribute("data-subscribed", "true");
  });

  it("updates the panel title when the agent roster arrives", () => {
    const props = makeProps("agent-late");
    const view = render(<ChatPanel {...props} />);
    agentsState = {
      agents: [{ id: "agent-late", name: "Atlas" }],
      defaultId: "agent-late",
    };

    view.rerender(<ChatPanel {...props} />);

    expect(props.api.setTitle).toHaveBeenCalledWith("Atlas");
  });
});
