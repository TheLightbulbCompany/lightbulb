import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { RoomSummary } from "@lightbulb/shared";

vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ membership: { role: "org:admin" } }) }));

import { ProjectSettingsDialog } from "../ProjectSettingsDialog";

const room = { roomId: "dm1", name: null, kind: "dm", roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0,
  updatedAt: "2026-11-08T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0 } as RoomSummary;

it("with no People (the Lightbulb page's own DM), a People link opens General instead of an empty page", () => {
  render(<ProjectSettingsDialog room={room} roomName="Lightbulb" section="people" onSection={vi.fn()} canEdit actionable />);
  const nav = screen.getByRole("navigation", { name: "Settings sections" });
  expect(nav).not.toHaveTextContent("People");
  expect(screen.getByRole("button", { name: "General" })).toHaveAttribute("aria-current", "page");
  expect(screen.queryByRole("heading", { name: "People" })).toBeNull();
});
