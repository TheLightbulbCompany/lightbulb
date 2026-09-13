import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { ParticipantAvatar } from "../ParticipantAvatar";

it.each([
  ["running", "Agent working"],
  ["attention", "Agent needs attention"],
  ["paused", "Agent paused"],
] as const)("labels %s activity without relying on color", (agentActivity, label) => {
  render(<ParticipantAvatar participant={{ kind: "agent", id: "a-1", displayName: "Friday", mentionHandle: "friday", agentActivity }} size="sm" />);
  expect(screen.getByRole("img", { name: "Friday" }).parentElement).toHaveAttribute("data-agent-state", agentActivity);
  expect(screen.getByText(label)).toHaveClass("sr-only");
});

it.each(["idle", "terminated", "unknown", undefined] as const)("does not invent activity for %s", (agentActivity) => {
  render(<ParticipantAvatar participant={{ kind: "agent", id: "a-1", displayName: "Friday", mentionHandle: null, agentActivity }} />);
  expect(screen.getByRole("img", { name: "Friday" }).parentElement).not.toHaveAttribute("data-agent-state");
  expect(screen.queryByText(/^Agent /)).not.toBeInTheDocument();
});

it("never presents a human as an active agent", () => {
  render(<ParticipantAvatar participant={{ kind: "human", id: "h-1", displayName: "Alice", mentionHandle: null, agentActivity: "running" }} />);
  expect(screen.getByRole("img", { name: "Alice" }).parentElement).not.toHaveAttribute("data-agent-state");
  expect(screen.queryByText(/^Agent /)).not.toBeInTheDocument();
});

it("shows an agent with a blue, always-on presence dot", () => {
  render(<ParticipantAvatar participant={{ kind: "agent", id: "a-1", displayName: "Friday", mentionHandle: null, agentActivity: undefined }} />);
  expect(screen.getByRole("img", { name: "Friday" }).parentElement).toHaveAttribute("data-presence", "agent");
  expect(screen.getByText("Agent, always available")).toHaveClass("sr-only");
});

it("gives an online human a green presence dot, not the agent one", () => {
  render(<ParticipantAvatar participant={{ kind: "human", id: "h-1", displayName: "Alice", mentionHandle: null, online: true }} />);
  expect(screen.getByRole("img", { name: "Alice" }).parentElement).toHaveAttribute("data-presence", "online");
  expect(screen.getByText("Online")).toHaveClass("sr-only");
});

it("shows no dot for an offline human", () => {
  render(<ParticipantAvatar participant={{ kind: "human", id: "h-1", displayName: "Alice", mentionHandle: null, online: false }} />);
  expect(screen.getByRole("img", { name: "Alice" }).parentElement).not.toHaveAttribute("data-presence");
});

it("suppresses the presence dot for both kinds when showPresence is off", () => {
  render(<ParticipantAvatar participant={{ kind: "agent", id: "a-1", displayName: "Friday", mentionHandle: null, agentActivity: undefined }} showPresence={false} />);
  expect(screen.getByRole("img", { name: "Friday" }).parentElement).not.toHaveAttribute("data-presence");
  render(<ParticipantAvatar participant={{ kind: "human", id: "h-1", displayName: "Alice", mentionHandle: null, online: true }} showPresence={false} />);
  expect(screen.getByRole("img", { name: "Alice" }).parentElement).not.toHaveAttribute("data-presence");
});
