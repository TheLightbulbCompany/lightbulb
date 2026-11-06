import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { LocalSession } from "@lightbulb/shared";
import { RoomComposer, type RoomComposerProps } from "../RoomComposer";
import { composerBox, composerValue, typeInComposer } from "@/test-utils/composer";

const session = {
  sessionId: "s1", deviceId: "d1", humanUserId: "u1", provider: "claude", generation: "g1",
  status: "idle", providerVersion: "1", controlUnavailableReason: null, activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  latestEventSeq: 0, cwdName: "lightbulb", title: "Launch check",
} satisfies LocalSession;
const sessionTargets: NonNullable<RoomComposerProps["sessionTargets"]> = [
  { candidate: { session, handle: "claude-launch-check", collided: false }, ownerName: "Aden" },
];

function Harness({ onSubmit = vi.fn(), members = [] }: Partial<RoomComposerProps>) {
  const [text, setText] = useState("");
  return <RoomComposer text={text} onTextChange={setText} members={members} sessionTargets={sessionTargets}
    clientMessageId="11111111-1111-4111-8111-111111111111" sendState="idle" onSubmit={onSubmit} />;
}

afterEach(cleanup);


it("lists a session after room members and inserts its typed handle", () => {
  render(<Harness members={[{ kind: "human", id: "u2", displayName: "Aden", mentionHandle: "aden", role: "member", listenMode: null, joinedAt: "2026-09-29", joinedSeq: 1 }]} />);
  const field = composerBox();
  typeInComposer("@");
  expect(screen.getAllByRole("option")[0]).toHaveAccessibleName(/Aden/);
  expect(screen.getAllByRole("option")[1]).toHaveAccessibleName(/Claude Code · Launch check/);
  expect(screen.getAllByRole("option")[1]).toHaveAccessibleName(/Aden/);
  fireEvent.click(screen.getAllByRole("option")[1]);
  expect(composerValue()).toBe("@claude-launch-check ");
});

it("sends session ids alongside ordinary agent mentions", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} members={[{ kind: "agent", id: "friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "mentions", joinedAt: "2026-09-29", joinedSeq: 1 }]} />);
  const field = composerBox();
  typeInComposer("@friday @claude-launch-check please look");
  fireEvent.keyDown(field, { key: "Enter" });
  expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({
    mentions: [{ kind: "agent", id: "friday" }], sessionMentions: ["s1"],
  }));
});

it("finds a session by its title", () => {
  render(<Harness />);
  const field = composerBox();
  typeInComposer("@Launch");
  expect(screen.getByRole("option", { name: /Launch check/ })).toBeTruthy();
  typeInComposer("@code");
  expect(screen.getByRole("option", { name: /Claude Code/ })).toBeTruthy();
});

it("holds a stale session handle until the sender chooses to send it as text", () => {
  const onSubmit = vi.fn();
  render(<Harness onSubmit={onSubmit} />);
  const field = composerBox();
  typeInComposer("@claude-gone ping");
  fireEvent.keyDown(field, { key: "Enter" });
  expect(onSubmit).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("@claude-gone no longer matches a shared session. Remove it or send anyway.");
  fireEvent.click(screen.getByRole("button", { name: "Send anyway" }));
  expect(onSubmit).toHaveBeenCalledWith(expect.not.objectContaining({ sessionMentions: expect.anything() }));
});
