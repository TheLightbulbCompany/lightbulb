import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));
import { RoomHeader } from "../RoomHeader";
import type { RoomSummary } from "@lightbulb/shared";
import type { RoomParticipant } from "../types";

const room: RoomSummary = {
  roomId: "room-1", name: "Roadmap", kind: "channel", roomRevision: 3, lastMessageSeq: 9,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-11T10:00:00Z",
  topic: "Ship v2", starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0,
};
const people: RoomParticipant[] = ["Aden", "Bo", "Cy", "Dee", "Eve"].map((name, index) => ({
  kind: "human", id: `u${index}`, displayName: name, mentionHandle: null, role: "member",
  listenMode: null, joinedAt: "2026-09-01T00:00:00Z", joinedSeq: 0,
}));
const props = {
  room, roomName: "Roadmap", counterparts: people, participantCount: 6, canEditRoom: true,
  canLeave: true, actionable: true, search: <span />,
  onOpenDetails: vi.fn(),
};

it("has no pinned-messages control (pins are gone)", () => {
  render(<RoomHeader {...props} />);
  expect(screen.queryByRole("button", { name: /pinned/ })).not.toBeInTheDocument();
});

// Options and the view switches are 32-36px square and Search is 42px wide
// (h-9 px-4) -- all under the 44px touch floor at <=859px. jsdom has no `css:
// true` here, so the media-query rule can't be observed via computed style;
// assert the DOM structure the selector needs (the buttons live inside
// .room-audience) plus the rule's source text instead.
it("marks a shared channel", () => {
  render(<RoomHeader {...props} room={{ ...room, shared: true }} />);
  expect(screen.getByText("Shared")).toHaveAttribute("title", "Shared between this workspace and Lightbulb");
});

it("people and Go live sit in the header; ⋯ holds mute, copy link, archive, leave, delete", async () => {
  const onOpenDetails = vi.fn();
  render(<RoomHeader {...props} onOpenDetails={onOpenDetails} room={{ ...room, kind: "group-dm" }}
    onSetPrefs={vi.fn()} onLeaveRoom={vi.fn()} onSetArchived={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "People, 6 here" }));
  expect(onOpenDetails).toHaveBeenCalledWith("members");
  await userEvent.click(screen.getByRole("button", { name: "More" }));
  for (const name of ["Mute", "Copy link", "Archive group chat", "Leave"]) expect(await screen.findByRole("menuitem", { name })).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  // name ▾ keeps the details, not the rare actions.
  await userEvent.click(screen.getByRole("button", { name: /Roadmap/ }));
  expect(await screen.findByRole("menuitem", { name: "Details" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Mute" })).toBeNull();
});
