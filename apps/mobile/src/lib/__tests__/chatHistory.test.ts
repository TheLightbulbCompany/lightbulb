// Regression: chat.history user rows must survive projection in every wire
// shape OpenClaw emits — in particular the BARE STRING `content` it persists
// for any plain-text turn (`buildPersistedUserTurnMessage`,
// src/sessions/user-turn-transcript.ts). Reading a non-array `content` as
// "empty" dropped the user's own words from the transcript, which is how the
// server-dispatched onboarding kickoff went missing above Friday's first turn.

import { projectHistoryToChatItems, shouldHideHistoryMessage } from "../chatHistory";
import type { HistoryMessage } from "../chatHistory";

const KICKOFF =
  "I'm starting a subscription coffee roastery that ships single-origin beans to " +
  "home brewers — it's for home brewers who care about origin and freshness. Dig " +
  "into the business and market, write it up, and put my three best next moves in " +
  "the backlog — don't start any of them yet, just tell me which to tackle first. " +
  "I'm watching.";

describe("shouldHideHistoryMessage — user content shapes", () => {
  it("keeps a user message whose content is a bare string", () => {
    expect(shouldHideHistoryMessage({ role: "user", content: KICKOFF })).toBe(false);
  });

  it("hides a user message whose bare-string content is blank", () => {
    expect(shouldHideHistoryMessage({ role: "user", content: "   " })).toBe(true);
  });

  it("keeps a user message carried on the top-level text field", () => {
    expect(shouldHideHistoryMessage({ role: "user", text: "hello" })).toBe(false);
  });

  it("still hides an exec wrapper delivered as a bare string", () => {
    const wrapper =
      "Exec finished (gateway id=abc, session=x, code 0)\nContinue the task if needed, then reply to the user";
    expect(shouldHideHistoryMessage({ role: "user", content: wrapper })).toBe(true);
  });
});

describe("projectHistoryToChatItems", () => {
  it("emits the onboarding kickoff bubble ahead of the assistant turn", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: KICKOFF, timestamp: 1785009807010 },
      {
        role: "assistant",
        content: [{ type: "text", text: "On it." }],
        timestamp: 1785009818955,
      },
    ];
    const { items } = projectHistoryToChatItems({ messages });
    expect(items[0].role).toBe("user");
    expect(items[0].text).toContain("subscription coffee roastery");
  });
});
