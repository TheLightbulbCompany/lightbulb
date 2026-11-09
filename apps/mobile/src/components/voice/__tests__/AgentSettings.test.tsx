import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";

const mockGet = jest.fn();
const mockPut = jest.fn();
const mockApi = { get: mockGet, put: mockPut };
jest.mock("@lightbulb/shared", () => ({ createApiClient: () => mockApi }));
jest.mock("../../../lib/teamsData", () => ({ useTeamsConfig: () => null }));
jest.mock("expo-router", () => ({ useFocusEffect: (fn: () => void) => require("react").useEffect(fn, [fn]) }));
import { AgentSettings } from "../AgentSettings";

const config = { name: "Nova", greeting: "Hello", instructions: "Be helpful", live: true, paused: false, services: [{ name: "Haircut", minutes: 30 }], allow: { book: true, answer: true, futurePermission: false }, handoff: { number: "+15555555555", onlyOpenHours: true } };
beforeEach(() => {
  jest.clearAllMocks();
  mockGet.mockResolvedValue({ config, agents: [{ id: "existing-worker", name: "Founders custom name" }] });
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


it("selects an existing worker while preserving founder configuration", async () => {
  await render(<AgentSettings />);
  await screen.findByDisplayValue("Nova");
  await fireEvent.press(screen.getByLabelText("Receptionist: Founders custom name"));
  await fireEvent.press(screen.getByRole("button", { name: "Save agent settings" }));
  await screen.findByText("Saved");
  expect(mockPut).toHaveBeenCalledWith("/voice/agent", { config: { ...config, frontAgentId: "existing-worker" } });
});


it("saves and reloads a receptionist before phone setup without creating phone settings", async () => {
  const agents = [{ id: "existing-worker", name: "Founders custom name" }];
  mockGet.mockResolvedValue({ config: null, agents, frontAgentId: "starter" });
  const first = await render(<AgentSettings />);
  await fireEvent.press(await screen.findByLabelText("Receptionist: Founders custom name"));
  await fireEvent.press(screen.getByRole("button", { name: "Save agent settings" }));
  await screen.findByText("Saved");
  const saved = { frontAgentId: "existing-worker", onboardingStep: 1 };
  expect(mockPut).toHaveBeenCalledWith("/voice/agent", { config: saved });
  expect(screen.getByText(/Set up your phone assistant on the web/)).toBeTruthy();
  expect(screen.queryByLabelText("Name callers hear")).toBeNull();
  await first.unmount();
  mockGet.mockResolvedValue({ config: saved, agents, frontAgentId: "existing-worker" });
  await render(<AgentSettings />);
  expect((await screen.findByLabelText("Receptionist: Founders custom name")).props.accessibilityState.checked).toBe(true);
  expect(screen.getByText(/Set up your phone assistant on the web/)).toBeTruthy();
  expect(screen.queryByLabelText("Name callers hear")).toBeNull();
});
