import { expect, it } from "vitest";
import { meetingStatus } from "../meetingStatus";

it("a passed calendar slot cannot prove a join", () => {
  const pending = meetingStatus({ status: "bot_created", startAt: 1 }, 10_000);
  expect(pending.label).toBe("Join not confirmed");
  expect(pending.joined).toBe(false);
  expect(meetingStatus({ startAt: 1 }, 10_000).label).toBe("Status unavailable");
});

it("distinguishes provider joining, lobby, and confirmed presence", () => {
  expect(meetingStatus({ status: "bot_created", lastBotStatus: "joining_call" }).label).toBe("Joining");
  expect(meetingStatus({ status: "bot_created", lastBotStatus: "in_waiting_room" }).detail).toMatch(/Let Lightbulb in/);
  expect(meetingStatus({ status: "in_call", lastBotStatus: "in_waiting_room" }).label).toBe("Live");
});

it("an ended record needs observed activity to claim attendance", () => {
  expect(meetingStatus({ status: "ended" }).detail).toMatch(/confirmation is unavailable/);
  expect(meetingStatus({ status: "ended", inCallAt: 1 }).detail).toBe("Lightbulb joined this meeting.");
});

it("failure and cancellation remain visible even with prior attendance", () => {
  expect(meetingStatus({ status: "bot_failed" }).label).toBe("Couldn't join");
  expect(meetingStatus({ status: "bot_failed", inCallAt: 1 }).label).toBe("Connection ended");
  expect(meetingStatus({ status: "cancelled", inCallAt: 1 }).label).toBe("Cancelled");
});
