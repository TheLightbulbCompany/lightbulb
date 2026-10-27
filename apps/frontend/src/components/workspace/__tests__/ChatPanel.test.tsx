// @vitest-environment jsdom
import { useEffect, useReducer } from "react";
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

// The shell hands the panel its params as props; `api.updateParameters` stands in for the
// controller's merge (undefined deletes) followed by the re-render it causes.
function makeProps(agentId?: string) {
  let params: Record<string, string> = agentId ? { agentId } : {};
  const listeners = new Set<() => void>();
  return {
    get params() { return params; },
    set params(next: Record<string, string>) { params = next; },
    subscribe: (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    api: {
      setTitle: vi.fn(),
      updateParameters: (partial: Record<string, string | undefined>) => {
        const merged: Record<string, string | undefined> = { ...params, ...partial };
        for (const k of Object.keys(partial)) if (partial[k] === undefined) delete merged[k];
        params = merged as Record<string, string>;
        listeners.forEach((listener) => listener());
      },
    },
  };
}

function Harness({ props, params }: { props: ReturnType<typeof makeProps>; params?: Record<string, string> }) {
  const [, force] = useReducer((n: number) => n + 1, 0);
  useEffect(() => props.subscribe(force), [props]);
  return <ChatPanel id={MESSAGES_PANEL_ID} params={params ?? props.params} />;
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
    render(<Harness props={props} />);
    expect(openMessage).not.toHaveBeenCalled();
    act(() => props.api.updateParameters({ roomId: "r1", messageId: "m1" }));
    expect(openMessage).toHaveBeenLastCalledWith("r1", "m1");
    act(() => props.api.updateParameters({ roomId: "r2", messageId: undefined }));
    expect(openMessage).toHaveBeenLastCalledWith("r2", null);
  });

  it("consumes session intents including explicit repeats without falling into agent selection", () => {
    const props = makeProps();
    props.params = { sessionId: "s1", sessionIntent: "1" };
    const view = render(<Harness props={props} />);
    expect(openSession).toHaveBeenLastCalledWith("s1", false, undefined);
    view.rerender(<Harness props={props} />);
    expect(openSession).toHaveBeenCalledTimes(1);
    act(() => props.api.updateParameters({ sessionId: "s1", sessionIntent: "2" }));
    expect(openSession).toHaveBeenCalledTimes(2);
    // A session search hit opens the same session at a turn.
    act(() => props.api.updateParameters({ sessionId: "s1", seq: "42", sessionIntent: "3" }));
    expect(openSession).toHaveBeenLastCalledWith("s1", false, 42);
    expect(selectAgentDm).not.toHaveBeenCalled();
    expect(openMessage).not.toHaveBeenCalled();
  });
  // A reload with ?panel=chat&session= in the URL: the shell applies the URL command after the
  // panel has mounted with its default params, so the panel sees the command as new props.
  it("opens the session a URL command set before the panel mounted", () => {
    const props = makeProps("agent-a");
    const view = render(<Harness props={props} />);
    expect(openSession).not.toHaveBeenCalled();
    view.rerender(<Harness props={props} params={{ sessionId: "s-reload", sessionIntent: "1" }} />);
    expect(openSession).toHaveBeenLastCalledWith("s-reload", false, undefined);
  });

  it("resolves the sentinel when the default roster arrives", () => {
    const props = makeProps(DEFAULT_AGENT_SENTINEL);
    const view = render(<Harness props={props} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
    defaultId = "agent-ceo";
    view.rerender(<Harness props={props} />);
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-ceo", false); // the default-agent fallback is not a pick
    expect(props.api.setTitle).not.toHaveBeenCalled();
  });

  it("lands a fresh workspace in #general, never the default agent's DM", () => {
    defaultId = "agent-ceo";
    freshLanding = true;
    render(<Harness props={makeProps(DEFAULT_AGENT_SENTINEL)} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
  });

  it("waits for the room list before falling back to the default agent", () => {
    defaultId = "agent-ceo";
    freshLanding = null;
    const props = makeProps(DEFAULT_AGENT_SENTINEL);
    const view = render(<Harness props={props} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
    freshLanding = false; // a returning workspace
    view.rerender(<Harness props={props} />);
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-ceo", false);
  });

  it("opens an explicit agent even in a fresh workspace", () => {
    freshLanding = true;
    const props = makeProps();
    render(<Harness props={props} />);
    act(() => props.api.updateParameters({ agentId: "agent-friday" }));
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-friday", true);
  });

  it("keeps an explicit agent deep link while the roster catches up", () => {
    const props = makeProps();
    render(<Harness props={props} />);
    act(() => props.api.updateParameters({ agentId: "agent-late" }));
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-late", true);
  });

  it("treats the agent the conversation mounted with as a cold load, not a pick", () => {
    const props = makeProps("a1");
    render(<Harness props={props} />);
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
    const view = render(<Harness props={props} />);
    act(() => props.api.updateParameters({ agentId: "agent-friday" }));
    act(() => props.api.updateParameters({ agentId: undefined, roomId: "r1" }));
    expect(openMessage).not.toHaveBeenCalled();
    const focused = vi.fn();
    window.addEventListener("getstarted:focus-composer", focused);
    await act(async () => { resolve("dm-1"); });
    roomProps = { roomId: "dm-1", composer: {} };
    view.rerender(<Harness props={props} />);
    window.removeEventListener("getstarted:focus-composer", focused);
    expect(focused).toHaveBeenCalledTimes(1);
  });

  it("retargets a mounted conversation on parameter changes", () => {
    const props = makeProps("agent-friday");
    render(<Harness props={props} />);
    act(() => props.api.updateParameters({ agentId: "agent-atlas" }));
    expect(selectAgentDm.mock.calls).toEqual([["agent-friday", false], ["agent-atlas", true]]);
  });

  it("does not snap room selection back when the selector changes identity", () => {
    const props = makeProps("agent-friday");
    const view = render(<Harness props={props} />);
    selectAgentDm = vi.fn().mockResolvedValue(undefined);
    view.rerender(<Harness props={props} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
    act(() => props.api.updateParameters({ agentId: undefined }));
    expect(selectAgentDm).not.toHaveBeenCalled();
    act(() => props.api.updateParameters({ agentId: "agent-friday" }));
    expect(selectAgentDm).toHaveBeenCalledExactlyOnceWith("agent-friday", true);
  });

  it("leaves generic room selection to the shared owner", () => {
    render(<Harness props={makeProps()} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
  });

  it("allows a failed agent selection to retry after clearing the target", async () => {
    selectAgentDm.mockRejectedValueOnce(new Error("Unavailable"));
    const props = makeProps("agent-friday");
    render(<Harness props={props} />);
    await act(async () => {});
    act(() => props.api.updateParameters({ agentId: undefined }));
    act(() => props.api.updateParameters({ agentId: "agent-friday" }));
    expect(selectAgentDm).toHaveBeenCalledTimes(2);
  });

  it("discards a deferred focus request when room selection fails", async () => {
    selectAgentDm.mockRejectedValueOnce(new Error("Unavailable"));
    requestFocusComposer("agent-friday", "Do not replay later");
    deferFocusComposerRequest("agent-friday");
    render(<Harness props={makeProps("agent-friday")} />);

    await act(async () => {});

    expect(releaseFocusComposerRequest("agent-friday")).toBe(false);
  });

  it("does not replay a newer selection after an older request fails", async () => {
    let rejectOld!: (error: Error) => void;
    selectAgentDm.mockImplementationOnce(() => new Promise<void>((_, reject) => { rejectOld = reject; }));
    const props = makeProps("agent-friday");
    const view = render(<Harness props={props} />);
    act(() => props.api.updateParameters({ agentId: "agent-atlas" }));
    await act(async () => { rejectOld(new Error("Old request failed")); });
    selectAgentDm = vi.fn().mockResolvedValue(undefined);
    view.rerender(<Harness props={props} />);
    expect(selectAgentDm).not.toHaveBeenCalled();
  });
});
