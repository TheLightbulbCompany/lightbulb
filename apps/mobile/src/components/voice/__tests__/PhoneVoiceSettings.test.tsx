import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { AppState, type AppStateStatus } from "react-native";

import { PhoneVoiceSettings } from "../PhoneVoiceSettings";

const mockLive = { roomId: null as string | null, joining: null as string | null };
jest.mock("../../../providers/live", () => ({ useLive: () => mockLive }));
jest.mock("../../../config/env", () => ({ API_URL: "https://api-dev.thelightbulb.company/api/v1" }));
jest.mock("@lightbulb/shared", () => jest.requireActual("../../../../../../packages/shared/src/voicePreferences"));
jest.mock("expo-router", () => ({ useFocusEffect: (fn: () => void) => jest.requireActual("react").useEffect(fn, [fn]) }));
jest.mock("react-native-webview", () => ({ WebView: "WebView" }));
beforeEach(() => { mockLive.roomId = mockLive.joining = null; });

it("opens only fixed same-model audio after a gesture and stops on voice or Live changes", async () => {
  const patch = jest.fn();
  const { rerender, unmount } = await render(<PhoneVoiceSettings config={{ voice: "Warm", spanish: true }} patch={patch} disabled={false} />);
  expect(screen.queryByLabelText("Voice sample player")).toBeNull();
  await fireEvent.press(screen.getByRole("button", { name: "Open voice sample player" }));
  const player = screen.getByLabelText("Voice sample player");
  expect(player.props.source.html).toContain('https://dev.thelightbulb.company/voice-samples/gpt-live-1-marin-en.wav');
  expect(player.props.source.html).not.toContain("autoplay");
  expect(player.props.mediaPlaybackRequiresUserAction).toBe(true);
  expect(player.props.onShouldStartLoadWithRequest({ url: "https://evil.example" })).toBe(false);
  expect(screen.getByLabelText("Loading voice sample")).toBeTruthy();
  await fireEvent(player, "message", { nativeEvent: { data: "ready" } });
  expect(screen.queryByLabelText("Loading voice sample")).toBeNull();
  await fireEvent(player, "message", { nativeEvent: { data: "error" } });
  expect(screen.getByRole("alert")).toBeTruthy();
  await fireEvent.press(screen.getByRole("radio", { name: "Spanish sample" }));
  expect(screen.queryByLabelText("Voice sample player")).toBeNull();
  await fireEvent.press(screen.getByRole("button", { name: "Open voice sample player" }));
  expect(screen.getByLabelText("Voice sample player").props.source.html).toContain('gpt-live-1-marin-es.wav');
  mockLive.roomId = "live-room";
  await rerender(<PhoneVoiceSettings config={{ voice: "willow", spanish: true }} patch={patch} disabled={false} />);
  expect(screen.queryByLabelText("Voice sample player")).toBeNull();
  expect(screen.getByText(/End your Live conversation/)).toBeTruthy();
  await unmount();
});

it("removes the player when the app backgrounds", async () => {
  const listeners: ((state: AppStateStatus) => void)[] = [];
  jest.spyOn(AppState, "addEventListener").mockImplementation((_type, listener) => {
    listeners.push(listener); return { remove: jest.fn() };
  });
  await render(<PhoneVoiceSettings config={{ voice: "quartz", spanish: false }} patch={jest.fn()} disabled={false} />);
  await fireEvent.press(screen.getByRole("button", { name: "Open voice sample player" }));
  await act(() => { listeners[0]("background"); });
  expect(screen.queryByLabelText("Voice sample player")).toBeNull();
});
