import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { AppState } from "react-native";

const mockBack = jest.fn();
const mockOpen = jest.fn(() => new Promise(() => {}));
jest.mock("expo-router", () => ({
  router: { back: (...a: unknown[]) => mockBack(...a) },
  Stack: { Screen: () => null },
  useLocalSearchParams: () => ({ agentId: "agent-1", name: "Friday" }),
}));
jest.mock("@clerk/clerk-expo", () => ({ useAuth: () => ({ getToken: async () => "t" }) }));
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => ({ onEvent: () => () => {} }) }));
jest.mock("../../../../lib/voiceRtc", () => ({ nativeVoiceRtc: { open: () => mockOpen() } }));

import Call from "../[agentId]";

describe("call screen", () => {
  beforeEach(() => { mockBack.mockClear(); mockOpen.mockClear(); });

  it("does not start a call on mount; the Call button does", async () => {
    await render(<Call />);
    expect(mockOpen).not.toHaveBeenCalled();
    await fireEvent.press(screen.getByLabelText("Call Friday"));
    expect(mockOpen).toHaveBeenCalledTimes(1);
  });

  it("pops exactly once after Hang up", async () => {
    await render(<Call />);
    await fireEvent.press(screen.getByLabelText("Call Friday"));
    await fireEvent.press(screen.getByLabelText("Hang up"));
    await new Promise((r) => setTimeout(r, 50));
    expect(mockBack).toHaveBeenCalledTimes(1);
  });

  it("hangs up when the app goes to the background", async () => {
    let handler: (s: string) => void = () => {};
    const spy = jest.spyOn(AppState, "addEventListener").mockImplementation(((_: string, h: (s: string) => void) => { handler = h; return { remove: jest.fn() }; }) as never);
    await render(<Call />);
    await fireEvent.press(screen.getByLabelText("Call Friday"));
    await act(async () => { handler("background"); await new Promise((r) => setTimeout(r, 50)); });
    expect(mockBack).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });
});
