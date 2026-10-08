import { expect, it } from "vitest";
import { sessionScreenNotice, terminalStateReason } from "./sessionStates";
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

// ISOA-731: every state of the "no screen" family, for both readers.
it("gives a teammate no terminal instructions in any variant", () => {
  const variants: LocalSession[] = [base, { ...base, terminalReason: "plain" }, { ...base, provider: "codex", terminalReason: "no-daemon" },
    { ...base, provider: "codex" }, { ...base, terminalReason: "app" }, { ...base, terminalReason: "sdk" }, { ...base, terminalReason: "unknown" }];
  for (const session of variants) {
    const notice = sessionScreenNotice(session, false, "Ada");
    expect(notice.title).toBe("Ada is working in this session, but its screen is not shared.");
    expect(notice.how).toBeUndefined();
    expect(notice.settings).toBeUndefined();
    expect(`${notice.title} ${notice.body}`).not.toMatch(/←|terminal|press|codex/i);
  }
});

it("gives the owner the meaning first, and a step only where one exists", () => {
  const plain = sessionScreenNotice(base, true, "Ada");
  expect(plain.title).toBe("This session's screen is not shared yet.");
  expect(plain.how).toBe("To share it, go to the terminal window where it runs and press the left arrow key (←) once. Teammates can then watch it live.");
  expect(plain.settings).toBe("To share every new session without this step, turn on Share every session live in Settings.");
  const codex = sessionScreenNotice({ ...base, provider: "codex", terminalReason: "no-daemon" }, true, "Ada");
  expect(codex.how).toBe("To share it, close it and start it again by typing lightbulb codex in your terminal. Teammates can then watch it live.");
  expect(codex.settings).toBeUndefined();
  const unknown = sessionScreenNotice({ ...base, terminalReason: "unknown" }, true, "Ada");
  expect(unknown).toEqual({ title: "This session's screen is not shared.", body: "Lightbulb cannot show this session's screen. Teammates still see its status and what it changed." });
});
