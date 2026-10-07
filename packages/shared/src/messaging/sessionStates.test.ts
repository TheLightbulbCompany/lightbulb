import { expect, it } from "vitest";
import { terminalStateReason } from "./sessionStates";
import type { LocalSession } from "./types";

const base: LocalSession = {
  sessionId: "s", deviceId: "mac", humanUserId: "alice", provider: "claude", generation: "g1", status: "idle",
  providerVersion: "test", activeTurnId: null, capabilities: { sendMode: "queue", steer: false, interrupt: false },
  capabilitiesVerified: true, latestEventSeq: 0, controlUnavailableReason: null,
};

it("names why a running session has no terminal, and nothing otherwise", () => {
  expect(terminalStateReason(base)).toBe("plain"); // an older Mac sends no reason: a plain Claude is `plain`
  expect(terminalStateReason({ ...base, provider: "codex" })).toBeNull();
  expect(terminalStateReason({ ...base, provider: "codex", terminalReason: "no-daemon" })).toBe("no-daemon");
  expect(terminalStateReason({ ...base, terminalReason: "unknown" })).toBeNull();
  expect(terminalStateReason({ ...base, terminal: "held", terminalReason: "plain" })).toBeNull();
  expect(terminalStateReason({ ...base, status: "ended" })).toBeNull();
  expect(terminalStateReason({ ...base, status: "offline" })).toBeNull();
});
