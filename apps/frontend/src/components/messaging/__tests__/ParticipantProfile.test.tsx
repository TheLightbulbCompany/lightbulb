import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { ParticipantProfile } from "../ParticipantProfile";
import type { RoomParticipant } from "../types";

const person: RoomParticipant = { kind: "human", id: "alice", displayName: "Alice", mentionHandle: null, role: "member", listenMode: null, joinedAt: "2026-09-10T12:00:00Z", joinedSeq: 0 };

it.each([person, { ...person, kind: "agent" as const, displayName: "Friday", mentionHandle: "friday" }])("opens authorized profile details and messages $displayName", async (participant) => {
  const onMessage = vi.fn().mockResolvedValue(undefined);
  render(<ParticipantProfile participant={participant} currentUserId="viewer" onMessage={onMessage} />);
  const trigger = screen.getByRole("button", { name: participant.displayName });
  await userEvent.click(trigger);
  const profile = screen.getByRole("dialog", { name: participant.displayName });
  expect(profile).toHaveTextContent("Role hereMember");
  expect(profile).toHaveTextContent("Joined");
  expect(profile.querySelector("time")).toHaveAttribute("datetime", participant.joinedAt);
  if (participant.mentionHandle) expect(profile).toHaveTextContent("@friday");
  expect(profile).not.toHaveTextContent(/Online|Offline|Email/);
  expect(onMessage).not.toHaveBeenCalled();
  await userEvent.click(within(profile).getByRole("button", { name: "Message" }));
  expect(onMessage).toHaveBeenCalledWith(participant);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it("keeps failed DM requests visible for retry without repeating a pending request", async () => {
  let reject!: (error: Error) => void;
  const onMessage = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
  render(<ParticipantProfile participant={person} currentUserId="viewer" onMessage={onMessage} />);
  await userEvent.click(screen.getByRole("button", { name: "Alice" }));
  await userEvent.click(screen.getByRole("button", { name: "Message" }));
  expect(screen.getByRole("button", { name: "Opening chat…" })).toBeDisabled();
  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  await act(async () => reject(new Error("Unavailable")));
  expect(screen.getByRole("alert")).toHaveTextContent("Could not open this chat. Try again.");
  onMessage.mockResolvedValue(undefined);
  await userEvent.click(screen.getByRole("button", { name: "Message" }));
  expect(onMessage).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("opens your own profile without a self-DM and restores focus on Escape", async () => {
  const onMessage = vi.fn();
  render(<ParticipantProfile participant={person} currentUserId="alice" onMessage={onMessage} />);
  const trigger = screen.getByRole("button", { name: "Alice" });
  await userEvent.click(trigger);
  expect(screen.getByRole("dialog")).toHaveTextContent("You");
  expect(screen.queryByRole("button", { name: "Message" })).not.toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  expect(trigger).toHaveFocus();
  expect(onMessage).not.toHaveBeenCalled();
});
