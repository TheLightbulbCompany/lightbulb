import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { SessionListRow } from "../SessionListRow";

const session: LocalSession = { sessionId: "s1", deviceId: "mac", humanUserId: "alice", provider: "claude", providerVersion: "2", generation: "g", status: "busy", activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0, title: "Load test" };

it("shows a session's unread chat on its row, and nothing when there is none", () => {
  const { rerender } = render(<ul><SessionListRow entry={{ session: { ...session, chatUnread: 3 }, ownerName: "Alice", own: false }} current={false} /></ul>);
  expect(screen.getByRole("button", { name: /, 3 unread in chat$/ })).toBeInTheDocument();
  expect(document.querySelector(".session-chat-unread")).toHaveTextContent("3");
  rerender(<ul><SessionListRow entry={{ session: { ...session, chatUnread: 0 }, ownerName: "Alice", own: false }} current={false} /></ul>);
  expect(screen.getByRole("button").getAttribute("aria-label")).not.toMatch(/unread in chat/);
  expect(document.querySelector(".session-chat-unread")).toBeNull();
});
