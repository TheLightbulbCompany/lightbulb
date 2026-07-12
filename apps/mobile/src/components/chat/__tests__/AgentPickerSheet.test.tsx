import { fireEvent, render, screen } from "@testing-library/react-native";

import { AgentPickerSheet } from "../AgentPickerSheet";

// never[] is assignable to the Agent[] prop; agentDisplayName reads id/name/identity.
const agents = [
  { id: "agent_a", name: "Friday", identity: { emoji: "🤖" } },
  { id: "agent_b", name: "Atlas" },
] as never[];

describe("AgentPickerSheet", () => {
  it("renders each agent and fires onPick with the chosen one", async () => {
    const onPick = jest.fn();
    await render(
      <AgentPickerSheet visible agents={agents} onClose={() => {}} onPick={onPick} />,
    );
    expect(screen.getByText("Friday")).toBeTruthy();
    fireEvent.press(screen.getByText("Atlas"));
    expect(onPick).toHaveBeenCalledTimes(1);
    expect(onPick).toHaveBeenCalledWith(agents[1]);
  });

  it("closes when the scrim is pressed", async () => {
    const onClose = jest.fn();
    await render(
      <AgentPickerSheet visible agents={agents} onClose={onClose} onPick={() => {}} />,
    );
    fireEvent.press(screen.getByLabelText("Close agent picker"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
