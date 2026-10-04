// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import {
  chatViewSearch,
  planChatHistoryWrite,
  recordChatView,
  recordMessagesNavigation,
  resetChatView,
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

  it("never replaces with the same view it pushes", () => {
    expect(planChatHistoryWrite(room("r1"), room("r1"), "")).toEqual({ push: "panel=chat&room=r1" });
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

  it("re-records the open view for the rail's Messages button", () => {
    recordChatView(room("r1"), false);
    window.history.pushState(null, "", "/chat?panel=teams-issues");
    recordMessagesNavigation();
    expect(window.location.search).toBe("?panel=chat&room=r1");
  });
});
