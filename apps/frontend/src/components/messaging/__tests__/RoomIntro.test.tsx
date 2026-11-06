import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { RoomSummary } from "@lightbulb/shared";
import { RoomIntro, starterAsks } from "../RoomIntro";

const room = { roomId: "ext", kind: "channel", name: "lightbulb-acme", shared: true } as RoomSummary;

it("tells a new user the shared room is their line to the Lightbulb team", () => {
  render(<RoomIntro room={room} roomName="lightbulb-acme" participants={[]} />);
  expect(screen.getByText("This channel is shared with the Lightbulb team — ask us anything here.")).toBeInTheDocument();
});

it("keeps the ordinary channel intro for unshared rooms", () => {
  render(<RoomIntro room={{ ...room, shared: false }} roomName="launch" participants={[]} />);
  expect(screen.getByText("Mention an agent to bring one in.")).toBeInTheDocument();
});

it("W13-6: with the super-agent on, a channel opens on its goal and three short chips that each draft a full ask", () => {
  const onPickAsk = vi.fn();
  const project = { ...room, shared: false, name: "walk-12", topic: "Find a sharper tagline for the bakery website. Keep it short." } as RoomSummary;
  render(<RoomIntro room={project} roomName="walk-12" participants={[]} superAgent fresh onPickAsk={onPickAsk} />);
  expect(screen.getByText(/^Find a sharper tagline for the bakery website/)).toBeInTheDocument();
  expect(screen.queryByText(/This is the beginning|Mention/)).toBeNull();
  expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Plan it", "First draft", "See how others do it"]);
  fireEvent.click(screen.getByRole("button", { name: "First draft" }));
  expect(onPickAsk).toHaveBeenCalledWith(expect.stringContaining('first draft for this goal: "Find a sharper tagline for the bakery website".'));
});

it("first asks only while nobody has asked; a channel without a goal gets general ones", () => {
  const project = { ...room, shared: false, topic: "Grow signups" } as RoomSummary;
  const { rerender } = render(<RoomIntro room={project} roomName="p" participants={[]} superAgent onPickAsk={vi.fn()} />);
  expect(screen.queryByRole("button")).toBeNull();
  rerender(<RoomIntro room={{ ...project, topic: null }} roomName="p" participants={[]} superAgent fresh onPickAsk={vi.fn()} />);
  expect(screen.getAllByRole("button")).toHaveLength(3);
  expect(starterAsks(null).every((a) => !a.ask.includes('"'))).toBe(true);
  const long = starterAsks("x".repeat(400));
  expect(new Set(long.map((a) => a.label)).size).toBe(3);
  expect(long[0].ask.length).toBeLessThan(300);
});

it("a DM with Lightbulb opens like a project, says chat, and the first-run DM takes no asks on top of its first moves", () => {
  const lightbulb = { kind: "agent", id: "lb", displayName: "Lightbulb", mentionHandle: "lightbulb", role: "member", listenMode: "active", joinedAt: "", joinedSeq: 0 } as const;
  const dm = { ...room, kind: "dm", shared: false, topic: null, members: [lightbulb] } as unknown as RoomSummary;
  const { rerender } = render(<RoomIntro room={dm} roomName="Lightbulb" participants={[lightbulb]} superAgent fresh onPickAsk={vi.fn()} />);
  expect(screen.getByText("Ask for anything this chat needs; Lightbulb hands it to the right specialist.")).toBeInTheDocument();
  expect(screen.getAllByRole("button")).toHaveLength(3);
  rerender(<RoomIntro room={{ ...dm, firstRun: true }} roomName="Lightbulb" participants={[lightbulb]} superAgent fresh onPickAsk={vi.fn()} />);
  expect(screen.queryByRole("button")).toBeNull();
  rerender(<RoomIntro room={{ ...dm, members: [] }} roomName="Sam" participants={[]} superAgent fresh onPickAsk={vi.fn()} />);
  expect(screen.getByText("Messages here are private to the two of you.")).toBeInTheDocument();
});

it("a specialist's DM is not a project: Lightbulb has to be in it", () => {
  const growth = { kind: "agent", id: "g", displayName: "Growth", mentionHandle: "growth", role: "member", listenMode: "active", joinedAt: "", joinedSeq: 0 } as const;
  const dm = { ...room, kind: "dm", shared: false, topic: null, members: [growth] } as unknown as RoomSummary;
  render(<RoomIntro room={dm} roomName="Growth" participants={[growth]} superAgent fresh onPickAsk={vi.fn()} />);
  expect(screen.getByText("Growth replies to everything you send here.")).toBeInTheDocument();
});
