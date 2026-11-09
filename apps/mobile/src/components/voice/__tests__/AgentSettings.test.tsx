import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

import { AgentSettings } from "../AgentSettings";

const mockGet = jest.fn();
const mockPut = jest.fn();
const mockApi = { get: mockGet, put: mockPut };
jest.mock("@lightbulb/shared", () => ({ ...jest.requireActual("../../../../../../packages/shared/src/voicePreferences"), createApiClient: () => mockApi }));
jest.mock("../../../providers/live", () => ({ useLive: () => ({ roomId: null, joining: null }) }));
jest.mock("react-native-webview", () => ({ WebView: "WebView" }));
jest.mock("../../../lib/teamsData", () => ({ useTeamsConfig: () => null }));
jest.mock("expo-router", () => ({ useFocusEffect: (fn: () => void) => jest.requireActual("react").useEffect(fn, [fn]) }));

const config = { name: "Nova", greeting: "Hello", instructions: "Be helpful", live: true, paused: false, services: [{ name: "Haircut", minutes: 30 }], allow: { book: true, answer: true, futurePermission: false }, handoff: { number: "+15555555555", onlyOpenHours: true } };
beforeEach(() => {
  jest.clearAllMocks();
  mockGet.mockResolvedValue({ config });
  mockPut.mockImplementation(async (_url, body) => body);
});

it("saves changed fields while preserving the existing services and unknown configuration", async () => {
  await render(<AgentSettings />);
  await screen.findByDisplayValue("Nova");
  await fireEvent.changeText(screen.getByLabelText("Name callers hear"), "Office assistant");
  await fireEvent(screen.getByLabelText("Book appointments"), "valueChange", false);
  await fireEvent.press(screen.getByRole("button", { name: "Save agent settings" }));
  await screen.findByText("Saved");
  expect(mockPut).toHaveBeenCalledWith("/voice/agent", { config: { ...config, name: "Office assistant", allow: { ...config.allow, book: false } } });
});

it("retains unsaved changes after a permission failure and allows retry", async () => {
  mockPut.mockRejectedValueOnce({ status: 403 });
  await render(<AgentSettings />);
  await screen.findByDisplayValue("Nova");
  await fireEvent.changeText(screen.getByLabelText("Instructions"), "Use our calendar");
  await fireEvent.press(screen.getByRole("button", { name: "Save agent settings" }));
  await screen.findByText("Only a workspace admin can change agent settings.");
  expect(screen.getByDisplayValue("Use our calendar")).toBeTruthy();
  await fireEvent.press(screen.getByRole("button", { name: "Save agent settings" }));
  await waitFor(() => expect(mockPut).toHaveBeenCalledTimes(2));
});

it("does not write an incomplete document for a workspace awaiting phone setup", async () => {
  mockGet.mockResolvedValue({ config: null });
  await render(<AgentSettings />);
  expect(await screen.findByText(/Set up your phone assistant on the web/)).toBeTruthy();
  expect(mockPut).not.toHaveBeenCalled();
});


it("retains Warm on unrelated edits and saves chosen phone voice and Spanish", async () => {
  mockGet.mockResolvedValue({ config: { ...config, voice: "Warm", spanish: false } });
  await render(<AgentSettings />);
  await screen.findByDisplayValue("Nova");
  expect(screen.getByRole("radio", { name: "Marin (default)" }).props.accessibilityState.selected).toBe(true);
  await fireEvent.changeText(screen.getByLabelText("Instructions"), "Brief replies");
  await fireEvent.press(screen.getByRole("button", { name: "Save agent settings" }));
  await screen.findByText("Saved");
  expect(mockPut).toHaveBeenLastCalledWith("/voice/agent", { config: { ...config, voice: "Warm", spanish: false, instructions: "Brief replies" } });
  await fireEvent.press(screen.getByRole("radio", { name: "Quartz" }));
  await fireEvent(screen.getByLabelText("Also speaks Spanish"), "valueChange", true);
  await fireEvent.press(screen.getByRole("button", { name: "Save agent settings" }));
  await screen.findByText("Saved");
  expect(mockPut).toHaveBeenLastCalledWith("/voice/agent", { config: { ...config, instructions: "Brief replies", voice: "quartz", spanish: true } });
});
