// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { setCurrentEntryNamer } from "@/lib/shellNav";
import {
  chatViewSearch,
  currentChatViewSearch,
  planChatHistoryWrite,
  recordChatView,
  recordMessagesNavigation,
  resetChatView,
  setUnrecordedView,
} from "../chatHistory";

const room = (roomId: string) => ({ roomId, sessionId: null });

afterEach(() => {
  resetChatView();
  window.history.replaceState(null, "", "/chat");
});

describe("chatViewSearch", () => {
  it("prefers the session over the room", () => {
    expect(chatViewSearch({ roomId: "r1", sessionId: "s1" })).toBe("panel=chat&session=s1");
    expect(chatViewSearch(room("r1"))).toBe("panel=chat&room=r1");
    expect(chatViewSearch({ roomId: null, sessionId: null })).toBeNull();
  });
});

describe("planChatHistoryWrite — the pane beside the conversation", () => {
  it("keeps a two-slot pane part when another room is picked", () => {
    expect(planChatHistoryWrite(room("r1"), room("r2"), "?panel=chat&room=r1&pane=browser"))
      .toEqual({ push: "panel=chat&room=r2&pane=browser" });
  });

  it("turns a plain pane URL (Chat as the place) into the room plus that pane", () => {
    expect(planChatHistoryWrite(room("r1"), room("r2"), "?panel=file-viewer&path=%2Fa.md"))
      .toEqual({ push: "panel=chat&room=r2&pane=file-viewer&pane.path=%2Fa.md" });
  });

  it("adds no pane when the URL names a place", () => {
    expect(planChatHistoryWrite(room("r1"), room("r2"), "?panel=teams-issues")).toEqual({ push: "panel=chat&room=r2" });
  });
});

describe("planChatHistoryWrite", () => {
  it("does nothing when the URL already shows the view (a Back replay)", () => {
    expect(planChatHistoryWrite(room("r1"), room("r2"), "?panel=chat&room=r2")).toBeNull();
    expect(planChatHistoryWrite(room("r1"), room("r2"), "?panel=chat&room=r2&message=m9")).toBeNull();
  });

  it("pushes a room pick made from a panel URL", () => {
    expect(planChatHistoryWrite(room("r1"), room("r2"), "?panel=teams-issues")).toEqual({ push: "panel=chat&room=r2" });
  });

  it("replaces a bare /chat entry with the previous room before pushing", () => {
    expect(planChatHistoryWrite(room("r1"), room("r2"), "")).toEqual({
      replace: "panel=chat&room=r1",
      push: "panel=chat&room=r2",
    });
  });

  it("replaces, not pushes, an agent deep link once it resolves to a room", () => {
    expect(planChatHistoryWrite(room("r1"), room("dm"), "?agent=a1")).toEqual({ replace: "panel=chat&room=dm" });
    expect(planChatHistoryWrite(room("r1"), room("dm"), "?panel=chat&agent=a1")).toEqual({ replace: "panel=chat&room=dm" });
  });

  it("does not treat a session URL as showing its room", () => {
    expect(planChatHistoryWrite({ roomId: "r1", sessionId: "s1" }, room("r1"), "?panel=chat&session=s1"))
      .toEqual({ push: "panel=chat&room=r1" });
  });

  it("treats a ?panel=session URL as already showing that session (cold load)", () => {
    expect(planChatHistoryWrite(null, { roomId: "r1", sessionId: "s1" }, "?panel=session&session=s1")).toBeNull();
  });

  it("names a bare entry in place rather than stacking the same view on it", () => {
    expect(planChatHistoryWrite(room("r1"), room("r1"), "")).toEqual({ replace: "panel=chat&room=r1" });
  });
});

describe("recordChatView", () => {
  it("writes nothing for automatic picks or empty views", () => {
    const before = window.history.length;
    recordChatView({ roomId: null, sessionId: null }, false);
    recordChatView(room("r1"), false);
    recordChatView(room("general"), false);
    expect(window.location.search).toBe("");
    expect(window.history.length).toBe(before);
  });

  it("writes explicit picks, replacing the bare entry with the automatic room", () => {
    recordChatView(room("r1"), false);
    const before = window.history.length;
    recordChatView(room("r2"), true);
    expect(window.location.search).toBe("?panel=chat&room=r2");
    expect(window.history.length).toBe(before + 1);
  });

  it("records an opened session view", () => {
    recordChatView(room("r1"), false);
    window.history.pushState(null, "", "/chat?panel=chat&room=r1");
    const before = window.history.length;
    recordChatView({ roomId: "r1", sessionId: "s1" }, true);
    expect(window.location.search).toBe("?panel=chat&session=s1");
    expect(window.history.length).toBe(before + 1);
  });

  it("pushes an explicit room change away from a session view", () => {
    window.history.pushState(null, "", "/chat?panel=chat&session=s1");
    recordChatView({ roomId: "r1", sessionId: "s1" }, true);
    const before = window.history.length;
    recordChatView(room("r2"), true);
    expect(window.location.search).toBe("?panel=chat&room=r2");
    expect(window.history.length).toBe(before + 1);
  });

  it("replaces a bare URL with the session view it was showing", () => {
    recordChatView({ roomId: "r1", sessionId: "s1" }, false);
    const replace = vi.spyOn(window.history, "replaceState");
    const before = window.history.length;
    recordChatView(room("r2"), true);
    expect(replace).toHaveBeenCalledExactlyOnceWith(null, "", "/chat?panel=chat&session=s1");
    expect(window.history.length).toBe(before + 1);
    expect(window.location.search).toBe("?panel=chat&room=r2");
    replace.mockRestore();
  });

  it("rewrites the URL in place when the session on it is closed", () => {
    window.history.pushState(null, "", "/chat?panel=chat&session=s1");
    recordChatView({ roomId: "r1", sessionId: "s1" }, true);
    const before = window.history.length;
    recordChatView(room("r1"), false);
    expect(window.location.search).toBe("?panel=chat&room=r1");
    expect(window.history.length).toBe(before);
  });

  it("keeps the pane when the closed session's URL becomes its room", () => {
    window.history.pushState(null, "", "/chat?panel=chat&session=s1&pane=browser");
    recordChatView({ roomId: "r1", sessionId: "s1" }, true);
    recordChatView(room("r1"), false);
    expect(window.location.search).toBe("?panel=chat&room=r1&pane=browser");
  });

  it("does not rewrite a URL that no longer shows the closed session", () => {
    window.history.pushState(null, "", "/chat?panel=teams-issues");
    recordChatView({ roomId: "r1", sessionId: "s1" }, false);
    recordChatView(room("r1"), false);
    expect(window.location.search).toBe("?panel=teams-issues");
  });

  it("re-records the open view for the rail's Messages button", () => {
    recordChatView(room("r1"), false);
    window.history.pushState(null, "", "/chat?panel=teams-issues");
    recordMessagesNavigation();
    expect(window.location.search).toBe("?panel=chat&room=r1");
  });

  it("writes nothing for the rail's Messages button while an unrecorded view is open", () => {
    recordChatView(room("r1"), false);
    window.history.pushState(null, "", "/chat?panel=teams-issues");
    setUnrecordedView(true);
    recordMessagesNavigation();
    expect(window.location.search).toBe("?panel=teams-issues");
    expect(currentChatViewSearch()).toBeNull();
  });

  it("does not name a bare entry after the view it is about to push", () => {
    setCurrentEntryNamer(currentChatViewSearch);
    const replace = vi.spyOn(window.history, "replaceState");
    recordChatView(room("r1"), true);
    expect(replace).not.toHaveBeenCalled();
    expect(window.location.search).toBe("?panel=chat&room=r1");
    replace.mockRestore();
    setCurrentEntryNamer(null);
  });

  it("replaces, not pushes, a bare /chat entry for the rail's Messages button", () => {
    recordChatView(room("r1"), false);
    const before = window.history.length;
    recordMessagesNavigation();
    expect(window.location.search).toBe("?panel=chat&room=r1");
    expect(window.history.length).toBe(before);
  });
});
