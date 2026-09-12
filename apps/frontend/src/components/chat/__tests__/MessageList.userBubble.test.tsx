/**
 * User bubbles must render for every shape chat.history returns.
 *
 * OpenClaw persists a plain-text turn with `content` as a BARE STRING
 * (`buildPersistedUserTurnMessage`, src/sessions/user-turn-transcript.ts) and
 * chat.history returns it verbatim. The renderer only understood block arrays
 * (falling back to a top-level `text` field), so a restored user message
 * painted an empty bubble — and the filter one layer up dropped it outright.
 * That is why the server-dispatched onboarding kickoff never appeared above
 * Friday's first tool call.
 */

import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MessageList } from "../MessageList";
import type { HistoryMessage, RenderGroup } from "@/hooks/internal/types";

const KICKOFF =
  "I'm starting a subscription coffee roastery that ships single-origin beans to " +
  "home brewers — it's for home brewers who care about origin and freshness. Dig " +
  "into the business and market, write it up, and put my three best next moves in " +
  "the backlog — don't start any of them yet, just tell me which to tackle first. " +
  "I'm watching.";

function userGroup(message: HistoryMessage): RenderGroup[] {
  return [{ side: "user", key: "g1", items: [{ kind: "message", key: "m1", message }] }];
}

function renderUser(message: HistoryMessage) {
  render(
    <MessageList
      renderGroups={userGroup(message)}
      isStreaming={false}
      agentName="Friday"
    />,
  );
}

describe("MessageList — user bubble content shapes", () => {
  it("renders the onboarding kickoff when content is a bare string", () => {
    renderUser({ role: "user", content: KICKOFF, timestamp: 1785009807010 });
    expect(screen.getByText(/subscription coffee roastery/)).toBeInTheDocument();
    expect(screen.getByText(/I'm watching\./)).toBeInTheDocument();
  });

  it("renders a user message carried on the top-level text field", () => {
    renderUser({ role: "user", text: "hello from the text field" });
    expect(screen.getByText(/hello from the text field/)).toBeInTheDocument();
  });

  it("still renders the block-array shape", () => {
    renderUser({ role: "user", content: [{ type: "text", text: "hello from blocks" }] });
    expect(screen.getByText(/hello from blocks/)).toBeInTheDocument();
  });
});
