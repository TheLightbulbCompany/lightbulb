import { expect, it } from "vitest";
import { conversationOnly, sessionScreenNotice } from "./sessionStates";
import type { LocalSession } from "./types";

const base: LocalSession = {
  sessionId: "s", deviceId: "mac", humanUserId: "alice", provider: "claude", generation: "g1", status: "idle",
  providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false },
  capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null,
};

it("shows the conversation only for a session that can never have a terminal", () => {
  for (const terminalReason of ["app", "sdk", "unknown"] as const) expect(conversationOnly({ ...base, terminalReason })).toBe(true);
  expect(conversationOnly({ ...base, status: "ended", terminalReason: "unknown" })).toBe(true);
  // A terminal session with no terminal right now (an older Mac app): a notice, never its conversation.
  expect(conversationOnly(base)).toBe(false);
  expect(conversationOnly({ ...base, terminalReason: "plain" })).toBe(false);
  expect(conversationOnly({ ...base, provider: "codex", terminalReason: "no-daemon" })).toBe(false);
  expect(conversationOnly({ ...base, terminal: "held", terminalReason: "unknown" })).toBe(false);
});

it("says what an app session is, and gives nobody terminal steps for any other", () => {
  const app = { title: "This session runs inside an app, so there is no terminal to show.", body: "This is its conversation." };
  expect(sessionScreenNotice({ ...base, terminalReason: "unknown" }, true, "Ada")).toEqual(app);
  expect(sessionScreenNotice({ ...base, terminalReason: "app" }, false, "Ada")).toEqual(app);
  for (const session of [base, { ...base, terminalReason: "plain" as const }, { ...base, provider: "codex" as const, terminalReason: "no-daemon" as const }]) {
    expect(sessionScreenNotice(session, true, "Ada").title).toBe("This session's screen is not shared.");
    expect(sessionScreenNotice(session, false, "Ada")).toEqual({ title: "Ada is working in this session, but its screen is not shared.", body: "You can still follow its status and what it changed." });
  }
});
