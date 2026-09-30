import { fireEvent, render, screen } from "@testing-library/react-native";

const mockBack = jest.fn();
jest.mock("expo-router", () => ({
  router: { back: (...a: unknown[]) => mockBack(...a) },
  Stack: { Screen: () => null },
  useLocalSearchParams: () => ({ agentId: "agent-1", name: "Friday" }),
}));
jest.mock("@clerk/clerk-expo", () => ({ useAuth: () => ({ getToken: async () => "t" }) }));
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => ({ onEvent: () => () => {} }) }));
jest.mock("../../../../lib/voiceRtc", () => ({ nativeVoiceRtc: { open: () => new Promise(() => {}) } }));

import Call from "../[agentId]";

describe("call screen", () => {
  beforeEach(() => mockBack.mockClear());
  it("pops exactly once after Hang up", async () => {
    await render(<Call />);
    await fireEvent.press(screen.getByLabelText("Hang up"));
    await new Promise((r) => setTimeout(r, 50));
    expect(mockBack).toHaveBeenCalledTimes(1);
  });
});
