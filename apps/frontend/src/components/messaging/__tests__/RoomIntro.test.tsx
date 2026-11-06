import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { RoomSummary } from "@lightbulb/shared";
import { RoomIntro, starterAsks } from "../RoomIntro";

const room = { roomId: "ext", kind: "channel", name: "lightbulb-acme", shared: true } as RoomSummary;

it("tells a new user the shared room is their line to the Lightbulb team", () => {
  render(<RoomIntro room={room} roomName="lightbulb-acme" participants={[]} />);
  expect(screen.getByText("This project is shared with the Lightbulb team — ask us anything here.")).toBeInTheDocument();
});

it("keeps the ordinary channel intro for unshared rooms", () => {
  render(<RoomIntro room={{ ...room, shared: false }} roomName="launch" participants={[]} />);
  expect(screen.getByText("Mention an agent to bring one in.")).toBeInTheDocument();
});

it("W13-6: with the super-agent on, a project opens on its goal and three short chips that each draft a full ask", () => {
  const onPickAsk = vi.fn();
  const project = { ...room, shared: false, name: "walk-12", topic: "Find a sharper tagline for the bakery website. Keep it short." } as RoomSummary;
  render(<RoomIntro room={project} roomName="walk-12" participants={[]} superAgent fresh onPickAsk={onPickAsk} />);
  expect(screen.getByText(/^Find a sharper tagline for the bakery website/)).toBeInTheDocument();
  expect(screen.queryByText(/This is the beginning|Mention/)).toBeNull();
  expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Plan it", "First draft", "See how others do it"]);
  fireEvent.click(screen.getByRole("button", { name: "First draft" }));
  expect(onPickAsk).toHaveBeenCalledWith(expect.stringContaining('first draft for this goal: "Find a sharper tagline for the bakery website".'));
});

it("first asks only while nobody has asked; a project without a goal gets general ones", () => {
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
