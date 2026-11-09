import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { RoomSummary } from "@lightbulb/shared";

const mockGet = jest.fn();
const mockJoin = jest.fn();
const mockOpen = jest.fn();
const mockPush = jest.fn();
const mockLive = { roomId: null as string | null, joining: null as string | null, setOpen: mockOpen, join: mockJoin };
const mockApi = { get: mockGet };
jest.mock("@lightbulb/shared", () => ({ createApiClient: () => mockApi }));
jest.mock("../../../lib/teamsData", () => ({ useTeamsConfig: () => null }));
jest.mock("../../../providers/live", () => ({ useLive: () => mockLive }));
jest.mock("../../LogoMark", () => ({ LogoMark: () => null }));
jest.mock("expo-router", () => ({
  router: { push: (...args: unknown[]) => mockPush(...args), navigate: jest.fn() },
  useFocusEffect: (fn: () => void) => require("react").useEffect(fn, [fn]),
}));

import { assistantRoom, timelineSummary, VoiceHome } from "../VoiceHome";

const room = (roomId: string, agent: string, firstRun = false) => ({
  roomId, kind: "dm", firstRun, members: [{ kind: "agent", id: agent }],
} as RoomSummary);
beforeEach(() => {
  jest.clearAllMocks();
  mockLive.roomId = null;
  mockLive.joining = null;
  mockGet.mockResolvedValue({ items: [] });
});

it("opens voice and typed input in the same assistant DM, never a specialist DM", async () => {
  await render(<VoiceHome rooms={[room("specialist", "worker"), room("owner", "root")]} agentId="root" />);
  await waitFor(() => expect(mockGet).toHaveBeenCalledWith("/voice/timeline"));
  await fireEvent.press(screen.getByRole("button", { name: "Talk to Lightbulb" }));
  expect(mockJoin).toHaveBeenCalledWith("owner", "Lightbulb");
  await fireEvent.press(screen.getByText("Type to Lightbulb"));
  expect(mockPush).toHaveBeenCalledWith("/(app)/room/owner");
  expect(assistantRoom([room("specialist", "worker")])).toBeUndefined();
});

it("reopens an existing Live and protects a live conversation in another room", async () => {
  mockLive.roomId = "owner";
  const { rerender } = await render(<VoiceHome rooms={[room("owner", "root", true)]} />);
  await fireEvent.press(screen.getByRole("button", { name: "Open live conversation with Lightbulb" }));
  expect(mockOpen).toHaveBeenCalledWith(true);
  expect(mockJoin).not.toHaveBeenCalled();
  mockLive.roomId = "meeting";
  await rerender(<VoiceHome rooms={[room("owner", "root", true)]} />);
  expect(screen.getByRole("button", { name: "Talk to Lightbulb" })).toBeDisabled();
});

it("shows real recorded activity when the call summary is absent and excludes guessed steps", async () => {
  const item = { type: "call" as const, roomId: "call", at: 0, callerName: "Ada", steps: [{ text: "Appointment moved to Tuesday" }, { text: "Maybe booked", guessed: true }] };
  mockGet.mockResolvedValue({ items: [item] });
  await render(<VoiceHome rooms={[]} />);
  expect(await screen.findByText("Appointment moved to Tuesday")).toBeTruthy();
  expect(timelineSummary({ ...item, summary: "Confirmed with Ada" })).toBe("Confirmed with Ada");
  expect(timelineSummary({ ...item, steps: [{ text: "Maybe booked", guessed: true }] })).toBe("Open conversation");
});

it("retries a failed timeline fetch", async () => {
  mockGet.mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ items: [] });
  await render(<VoiceHome rooms={[]} />);
  await fireEvent.press(await screen.findByText("Couldn’t load recent activity. Retry"));
  expect(await screen.findByText("Calls and meetings will appear here.")).toBeTruthy();
});
