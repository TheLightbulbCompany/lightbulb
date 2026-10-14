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
let activeRoomId: string | null = null;
let sessionFocus: { sessionId: string | null } | null = null;
let roomProps: { roomId: string; composer: { focusAgentId?: string } } | undefined;
vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [], defaultId }),
}));
vi.mock("@/components/messaging/MessagingWorkspaceContext", () => ({
  useMessagingWorkspace: () => ({ selectAgentDm, openSession, openMessage, freshLanding, activeRoomId, sessionFocus, roomProps }),
}));
vi.mock("@/components/messaging/MessagingWorkspace", () => ({
  MessagingConversation: () => null,
}));

import { ChatPanel } from "../panels/ChatPanel";
type ChatPanelProps = React.ComponentProps<typeof ChatPanel>;

function makeProps(agentId?: string) {
  const listeners = new Set<(params: { agentId?: string }) => void>();
  const api = {
    id: MESSAGES_PANEL_ID,
    title: "Chat",
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
  activeRoomId = null;
  sessionFocus = null;
  roomProps = undefined;
  selectAgentDm = vi.fn().mockResolvedValue(undefined);
});

describe("ChatPanel", () => {
  it("does not re-open the room that is already open (the replay of its own URL)", () => {
    activeRoomId = "r1";
    const props = makeProps();
    props.params = { roomId: "r1" };
    render(<ChatPanel {...props} />);
    expect(openMessage).not.toHaveBeenCalled();
    act(() => props.api.updateParameters({ roomId: "r1", messageId: "m1" } as never));
    expect(openMessage).toHaveBeenLastCalledWith("r1", "m1");
    act(() => props.api.updateParameters({ roomId: "r2", messageId: undefined } as never));
    expect(openMessage).toHaveBeenLastCalledWith("r2", null);
  });

  it("consumes session intents including explicit repeats without falling into agent selection", () => {
    const props = makeProps();
    props.params = { sessionId: "s1", sessionIntent: "1" };
    const view = render(<ChatPanel {...props} />);
    expect(openSession).toHaveBeenLastCalledWith("s1");
    view.rerender(<ChatPanel {...props} />);
    expect(openSession).toHaveBeenCalledTimes(1);
    act(() => props.api.updateParameters({ sessionId: "s1", sessionIntent: "2" }));
    expect(openSession).toHaveBeenCalledTimes(2);
    expect(selectAgentDm).not.toHaveBeenCalled();
    expect(openMessage).not.toHaveBeenCalled();
  });
  // A reload with ?panel=chat&session= in the URL: the shell applies the URL command right after
  // it restores the layout, before this panel has mounted. Dockview then renders the panel once with
  // the restored params and again with the command's, and fires no parameters-changed event for it.
  it("opens the session a URL command set before the panel mounted", () => {
    const props = makeProps("agent-a");
    const view = render(<ChatPanel {...props} />);
    expect(openSession).not.toHaveBeenCalled();
    view.rerender(<ChatPanel {...props} params={{ sessionId: "s-reload", sessionIntent: "1" }} />);
    expect(openSession).toHaveBeenLastCalledWith("s-reload");
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
    const props = makeProps();
    render(<ChatPanel {...props} />);
    act(() => props.api.updateParameters({ agentId: "agent-friday" }));
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-friday", true);
  });

  it("keeps an explicit agent deep link while the roster catches up", () => {
    const props = makeProps();
    render(<ChatPanel {...props} />);
    act(() => props.api.updateParameters({ agentId: "agent-late" }));
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-late", true);
  });

  it("treats the agent restored with the layout as a cold load, not a pick", () => {
    const props = makeProps("a1");
    render(<ChatPanel {...props} />);
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("a1", false);
    act(() => props.api.updateParameters({ agentId: "a2" }));
    expect(selectAgentDm).toHaveBeenLastCalledWith("a2", true);
  });

  it("does not drop a pending agent focus when a room intent is a replay", async () => {
    let resolve!: (roomId: string) => void;
    selectAgentDm.mockImplementationOnce(() => new Promise((r) => { resolve = r as never; }) as never);
    activeRoomId = "r1";
    requestFocusComposer("agent-friday", "hello");
    deferFocusComposerRequest("agent-friday");
    const props = makeProps();
    const view = render(<ChatPanel {...props} />);
    act(() => props.api.updateParameters({ agentId: "agent-friday" }));
    act(() => props.api.updateParameters({ agentId: undefined, roomId: "r1" } as never));
    expect(openMessage).not.toHaveBeenCalled();
    const focused = vi.fn();
    window.addEventListener("getstarted:focus-composer", focused);
    await act(async () => { resolve("dm-1"); });
    roomProps = { roomId: "dm-1", composer: {} };
    view.rerender(<ChatPanel {...props} />);
    window.removeEventListener("getstarted:focus-composer", focused);
    expect(focused).toHaveBeenCalledTimes(1);
  });

  it("retargets a mounted conversation on Dockview parameter changes", () => {
    const props = makeProps("agent-friday");
    render(<ChatPanel {...props} />);
    act(() => props.api.updateParameters({ agentId: "agent-atlas" }));
    expect(selectAgentDm.mock.calls).toEqual([["agent-friday", false], ["agent-atlas", true]]);
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
