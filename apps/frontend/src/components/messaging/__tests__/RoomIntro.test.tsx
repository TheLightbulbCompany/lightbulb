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

it("W12-10: with the super-agent on, a project opens on its goal, what to ask, and first asks that draft", () => {
  const onPickAsk = vi.fn();
  const project = { ...room, shared: false, name: "walk-12", topic: "Find a sharper tagline for the bakery website. Keep it short." } as RoomSummary;
  render(<RoomIntro room={project} roomName="walk-12" participants={[]} superAgent fresh onPickAsk={onPickAsk} />);
  expect(screen.getByText(/^Find a sharper tagline for the bakery website/)).toBeInTheDocument();
  expect(screen.queryByText(/This is the beginning|Mention/)).toBeNull();
  const asks = screen.getAllByRole("button");
  expect(asks).toHaveLength(3);
  fireEvent.click(asks[0]);
  expect(onPickAsk).toHaveBeenCalledWith("Make a plan for: Find a sharper tagline for the bakery website");
});

it("first asks only while nobody has asked, and none without a goal", () => {
  const project = { ...room, shared: false, topic: "Grow signups" } as RoomSummary;
  const { rerender } = render(<RoomIntro room={project} roomName="p" participants={[]} superAgent onPickAsk={vi.fn()} />);
  expect(screen.queryByRole("button")).toBeNull();
  rerender(<RoomIntro room={{ ...project, topic: null }} roomName="p" participants={[]} superAgent fresh onPickAsk={vi.fn()} />);
  expect(screen.queryByRole("button")).toBeNull();
  expect(starterAsks("x".repeat(200))[0].length).toBeLessThan(100);
});
