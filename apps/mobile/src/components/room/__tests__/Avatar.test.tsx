import { render, screen } from "@testing-library/react-native";

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
});
