import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import type { RoomSummary } from "@lightbulb/shared";
import { RoomIntro } from "../RoomIntro";

const room = { roomId: "ext", kind: "channel", name: "lightbulb-acme", shared: true } as RoomSummary;

it("tells a new user the shared room is their line to the Lightbulb team", () => {
  render(<RoomIntro room={room} roomName="lightbulb-acme" participants={[]} />);
  expect(screen.getByText("This project is shared with the Lightbulb team — ask us anything here.")).toBeInTheDocument();
});

it("keeps the ordinary channel intro for unshared rooms", () => {
  render(<RoomIntro room={{ ...room, shared: false }} roomName="launch" participants={[]} />);
  expect(screen.getByText("Mention an agent to bring one in.")).toBeInTheDocument();
});
