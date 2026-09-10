// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";

interface FakeAgent {
  id: string;
  name?: string;
}

let agentsState: {
  agents: FakeAgent[];
  defaultId: string | undefined;
};

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => agentsState,
  agentDisplayName: (agent: FakeAgent) => agent.name ?? agent.id,
}));
vi.mock("@/components/messaging/MessagingWorkspace", () => ({
  MessagingWorkspace: ({ initialAgentId }: { initialAgentId: string | null }) => (
    <div data-testid="messaging-workspace" data-agent-id={initialAgentId ?? ""} />
  ),
}));

import { ChatPanel } from "../panels/ChatPanel";

type ChatPanelProps = React.ComponentProps<typeof ChatPanel>;

function makeProps(agentId: string): ChatPanelProps {
  const api: Record<string, unknown> = { title: "Messages" };
  api.setTitle = vi.fn((title: string) => {
    api.title = title;
  });
  return { params: { agentId }, api } as unknown as ChatPanelProps;
}

beforeEach(() => {
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
