import { expect, it } from "vitest";
import { markSessionRead, markSessionUnread, sessionUnread, type LocalSession } from "@lightbulb/shared";
import { readSessionSeen, readSidebarPrefs, writeSessionSeen, writeSidebarPrefs } from "../sidebarPrefs";

it("defaults to recent activity, and reads the old auto-written alpha default as recent", () => {
  window.localStorage.clear();
  expect(readSidebarPrefs("org").sort).toBe("recent");
  window.localStorage.setItem("lightbulb.messaging.sidebar.org", JSON.stringify({ collapsed: [], sort: "alpha" }));
  expect(readSidebarPrefs("org").sort).toBe("recent");
  window.localStorage.setItem("lightbulb.messaging.sidebar.org", JSON.stringify({ collapsed: [], sort: "name" }));
  expect(readSidebarPrefs("org").sort).toBe("name");
});

it("keeps two members' unread baselines apart on one browser", () => {
  window.localStorage.clear();
  const session = { sessionId: "s1", status: "idle", latestEventSeq: 7 } as LocalSession;
  const dana = "dana:org", sam = "sam:org";
  writeSessionSeen(dana, { s1: 7 });
  writeSessionSeen(sam, { s1: 7 });
  // Dana marks it unread: only her dot shows.
  writeSessionSeen(dana, markSessionUnread(readSessionSeen(dana), session));
  expect(sessionUnread(session, readSessionSeen(dana))).toBe("finished");
  expect(sessionUnread(session, readSessionSeen(sam))).toBeNull();
  // Dana marks it read again; Sam marks it unread: still apart.
  writeSessionSeen(dana, markSessionRead(readSessionSeen(dana), session));
  writeSessionSeen(sam, markSessionUnread(readSessionSeen(sam), session));
  expect(sessionUnread(session, readSessionSeen(dana))).toBeNull();
  expect(sessionUnread(session, readSessionSeen(sam))).toBe("finished");
});

it("round-trips collapsed groups and sort", () => {
  window.localStorage.clear();
  writeSidebarPrefs("org", { collapsed: ["x"], sort: "name" });
  expect(readSidebarPrefs("org")).toEqual({ collapsed: ["x"], sort: "name" });
});
