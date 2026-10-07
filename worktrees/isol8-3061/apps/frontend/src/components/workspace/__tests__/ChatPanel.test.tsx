// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";
import { DEFAULT_AGENT_SENTINEL, MESSAGES_PANEL_ID } from "../panelRegistry";
import {
  _resetFocusComposerRequestForTests,
  deferFocusComposerRequest,
  releaseFocusComposerRequest,
  requestFocusComposer,
} from "@/lib/focusComposerRequest";

let defaultId: string | undefined;
let selectAgentDm = vi.fn<(id: string, explicit?: boolean) => Promise<void>>();
const openSession = vi.fn();
const openMessage = vi.fn();
let freshLanding: boolean | null = false;
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [], defaultId }),
}));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspace: () => ({ selectAgentDm, openSession, openMessage, freshLanding }),
}));
vi.mock("@/components/messaging/MessagingWorkspace", () => ({
  MessagingConversation: () => null,
  MessagingWorkspace: () => null,
}));

import { ChatPanel } from "../panels/ChatPanel";
type ChatPanelProps = React.ComponentProps<typeof ChatPanel>;

function makeProps(agentId?: string) {
  const listeners = new Set<(params: { agentId?: string }) => void>();
  const api = {
    id: MESSAGES_PANEL_ID,
    title: "Messages",
    setTitle: vi.fn(),
    onDidParametersChange: (listener: (params: { agentId?: string }) => void) => {
      listeners.add(listener);
      return { dispose: () => listeners.delete(listener) };
    },
    updateParameters: (params: { agentId?: string }) => listeners.forEach((listener) => listener(params)),
  };
  return { params: { agentId }, api } as unknown as ChatPanelProps;
}

beforeEach(() => {
  _resetFocusComposerRequestForTests();
  defaultId = undefined;
  freshLanding = false;
  selectAgentDm = vi.fn().mockResolvedValue(undefined);
});

describe("ChatPanel", () => {
  it("consumes session intents including explicit repeats without falling into agent selection", () => {
    const props = makeProps();
    props.params = { roomId: "r1", sessionId: "s1", sessionIntent: "1" };
    const view = render(<ChatPanel {...props} />);
    expect(openSession).toHaveBeenLastCalledWith("s1", { from: "link", roomId: "r1" });
    view.rerender(<ChatPanel {...props} />);
    expect(openSession).toHaveBeenCalledTimes(1);
    act(() => props.api.updateParameters({ roomId: "r1", sessionId: "s1", sessionIntent: "2" }));
    expect(openSession).toHaveBeenCalledTimes(2);
    expect(selectAgentDm).not.toHaveBeenCalled();
    expect(openMessage).not.toHaveBeenCalled();
  });
  it("resolves the sentinel when the default roster arrives", () => {
    const props = makeProps(DEFAULT_AGENT_SENTINEL);
    const view = render(<ChatPanel {...props} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
    defaultId = "agent-ceo";
    view.rerender(<ChatPanel {...props} />);
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-ceo", false); // the default-agent fallback is not a pick
    expect(props.api.setTitle).not.toHaveBeenCalled();
  });

  it("lands a fresh workspace in #general, never the default agent's DM", () => {
    defaultId = "agent-ceo";
    freshLanding = true;
    render(<ChatPanel {...makeProps(DEFAULT_AGENT_SENTINEL)} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
  });

  it("waits for the room list before falling back to the default agent", () => {
    defaultId = "agent-ceo";
    freshLanding = null;
    const props = makeProps(DEFAULT_AGENT_SENTINEL);
    const view = render(<ChatPanel {...props} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
    freshLanding = false; // a returning workspace
    view.rerender(<ChatPanel {...props} />);
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-ceo", false);
  });

  it("opens an explicit agent even in a fresh workspace", () => {
    freshLanding = true;
    render(<ChatPanel {...makeProps("agent-friday")} />);
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-friday", true);
  });

  it("keeps an explicit agent deep link while the roster catches up", () => {
    render(<ChatPanel {...makeProps("agent-late")} />);
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-late", true);
  });

  it("retargets a mounted conversation on Dockview parameter changes", () => {
    const props = makeProps("agent-friday");
    render(<ChatPanel {...props} />);
    act(() => props.api.updateParameters({ agentId: "agent-atlas" }));
    expect(selectAgentDm.mock.calls).toEqual([["agent-friday", true], ["agent-atlas", true]]);
  });

  it("does not snap room selection back when the selector changes identity", () => {
    const props = makeProps("agent-friday");
    const view = render(<ChatPanel {...props} />);
    selectAgentDm = vi.fn().mockResolvedValue(undefined);
    view.rerender(<ChatPanel {...props} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
    act(() => props.api.updateParameters({ agentId: undefined }));
    expect(selectAgentDm).not.toHaveBeenCalled();
    act(() => props.api.updateParameters({ agentId: "agent-friday" }));
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-friday", true);
  });

  it("leaves generic room selection to the shared owner", () => {
    render(<ChatPanel {...makeProps()} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
  });

  it("allows a failed agent selection to retry after clearing the target", async () => {
    selectAgentDm.mockRejectedValueOnce(new Error("Unavailable"));
    const props = makeProps("agent-friday");
    render(<ChatPanel {...props} />);
    await act(async () => {});
    act(() => props.api.updateParameters({ agentId: undefined }));
    act(() => props.api.updateParameters({ agentId: "agent-friday" }));
    expect(selectAgentDm).toHaveBeenCalledTimes(2);
  });

  it("discards a deferred focus request when room selection fails", async () => {
    selectAgentDm.mockRejectedValueOnce(new Error("Unavailable"));
    requestFocusComposer("agent-friday", "Do not replay later");
    deferFocusComposerRequest("agent-friday");
    render(<ChatPanel {...makeProps("agent-friday")} />);

    await act(async () => {});

    expect(releaseFocusComposerRequest("agent-friday")).toBe(false);
  });

  it("does not replay a newer selection after an older request fails", async () => {
    let rejectOld!: (error: Error) => void;
    selectAgentDm.mockImplementationOnce(() => new Promise<void>((_, reject) => { rejectOld = reject; }));
    const props = makeProps("agent-friday");
    const view = render(<ChatPanel {...props} />);
    act(() => props.api.updateParameters({ agentId: "agent-atlas" }));
    await act(async () => { rejectOld(new Error("Old request failed")); });
    selectAgentDm = vi.fn().mockResolvedValue(undefined);
    view.rerender(<ChatPanel {...props} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
  });
});
