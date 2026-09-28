import { render, screen } from "@testing-library/react-native";
import { StyleSheet } from "react-native";

import { chatPalette } from "../../../config/theme";
import { Avatar } from "../Avatar";

describe("Avatar presence dot", () => {
  it("always shows an agent as reachable, blue", async () => {
    await render(<Avatar name="Friday" kind="agent" showPresence />);
    expect(screen.getByLabelText("Agent, always available")).toBeTruthy();
  });

  it("shows an online human as green", async () => {
    await render(<Avatar name="Alice" kind="human" online showPresence />);
    expect(screen.getByLabelText("Online")).toBeTruthy();
  });

  it("shows no dot for an offline (or unknown) human", async () => {
    await render(<Avatar name="Alice" kind="human" showPresence />);
    expect(screen.queryByLabelText("Online")).toBeNull();
  });

  it("suppresses the dot for both kinds when showPresence is off", async () => {
    await render(<Avatar name="Friday" kind="agent" />);
    expect(screen.queryByLabelText("Agent, always available")).toBeNull();
  });

  it("shows a terminal badge for a human who is sharing when presence is on", async () => {
    await render(<Avatar name="Alice Ray" kind="human" sharing showPresence />);
    expect(screen.getByLabelText("Sharing a terminal session")).toBeTruthy();
  });

  it("hides the badge for agents and quiet faces", async () => {
    await render(<Avatar name="Friday" kind="agent" sharing showPresence />);
    expect(screen.queryByLabelText("Sharing a terminal session")).toBeNull();
    await render(<Avatar name="Bob" kind="human" sharing />);
    expect(screen.queryByLabelText("Sharing a terminal session")).toBeNull();
  });
});

describe("Avatar redesign", () => {
  it("fills an agent tile with the agent tint and draws the initial in its foreground", async () => {
    await render(<Avatar name="Growth" kind="agent" size={56} palette={chatPalette.dark} />);
    const initial = screen.getByText("G");
    expect(StyleSheet.flatten(initial.props.style).color).toBe(chatPalette.dark.onAgentTint);
    const tile = screen.getByTestId("avatar-tile");
    expect(StyleSheet.flatten(tile.props.style)).toMatchObject({ backgroundColor: chatPalette.dark.agentTint, borderRadius: 12 });
  });

  it.each([
    ["running", "Working", "live"],
    ["attention", "Needs attention", "alert"],
    ["paused", "Paused", "warn"],
  ] as const)("rings %s in the %s colour", async (ring, label, token) => {
    await render(<Avatar name="CEO" kind="agent" ring={ring} palette={chatPalette.light} />);
    const node = screen.getByLabelText(label);
    expect(StyleSheet.flatten(node.props.style).borderColor).toBe(chatPalette.light[token]);
  });

  it("draws no ring by default", async () => {
    await render(<Avatar name="CEO" kind="agent" />);
    expect(screen.queryByLabelText("Working")).toBeNull();
  });

  it("renders a coding tool's mark instead of initials", async () => {
    await render(<Avatar name="Claude" kind="agent" vendor="claude" size={52} />);
    expect(screen.getByTestId("session-tool-claude", { includeHiddenElements: true })).toBeTruthy();
    expect(screen.queryByText("C")).toBeNull();
  });

  it("keeps rounded squares, never circles", async () => {
    await render(<Avatar name="Ann Lee" kind="human" size={28} />);
    expect(StyleSheet.flatten(screen.getByTestId("avatar-tile").props.style).borderRadius).toBe(8);
  });
});
