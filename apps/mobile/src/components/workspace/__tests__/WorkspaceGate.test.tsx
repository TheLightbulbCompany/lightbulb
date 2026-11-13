import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { AppState, Linking, Text } from "react-native";

// F4 (mobile UX audit 2026-09-23): the app never selected a Clerk org, so a
// session without one reached Home and dead-ended on the server's 409.
let mockOrgId: string | null = null;
let mockMemberships: { organization: { id: string; name: string } }[] = [];
const mockSetActive = jest.fn(async (_params: { organization: string }) => undefined);
const mockRevalidate = jest.fn(async () => undefined);
const mockResetSwrCache = jest.fn();

jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ orgId: mockOrgId, signOut: jest.fn() }),
  useOrganizationList: () => ({
    isLoaded: true,
    setActive: mockSetActive,
    userMemberships: { data: mockMemberships, isLoading: false, revalidate: mockRevalidate },
  }),
}));
jest.mock("@lightbulb/shared", () => ({ resetSwrCache: () => mockResetSwrCache() }));
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

import { WEB_ORIGIN } from "../../../config/domain";
import { WorkspaceGate } from "../WorkspaceGate";

const membership = (id: string, name: string) => ({ organization: { id, name } });

function renderGate() {
  return render(
    <WorkspaceGate>
      <Text>APP</Text>
    </WorkspaceGate>,
  );
}

let onAppStateChange: ((state: string) => void) | undefined;

beforeEach(() => {
  jest.spyOn(AppState, "addEventListener").mockImplementation((_type, handler) => {
    onAppStateChange = handler as (state: string) => void;
    return { remove: jest.fn() } as unknown as ReturnType<typeof AppState.addEventListener>;
  });
  mockOrgId = null;
  mockMemberships = [];
  mockSetActive.mockClear();
  mockRevalidate.mockClear();
  mockResetSwrCache.mockClear();
});

it("renders the app untouched when the session already has a workspace", async () => {
  mockOrgId = "org_1";
  mockMemberships = [membership("org_1", "Acme"), membership("org_2", "Beta")];
  await renderGate();
  expect(screen.getByText("APP")).toBeTruthy();
  expect(mockSetActive).not.toHaveBeenCalled();
});

it("with no workspace, offers to create one on the web — and says nothing about paying", async () => {
  const openURL = jest.spyOn(Linking, "openURL").mockResolvedValue(true);
  await renderGate();
  expect(screen.queryByText("APP")).toBeNull();
  expect(screen.getByText("Create a workspace")).toBeTruthy();
  expect(screen.getByText(/Joining a team that already uses Lightbulb\?.*join link/s)).toBeTruthy();
  expect(screen.getByText(/Invited by email\? Open that email on this phone and tap its link\./)).toBeTruthy();
  // App Store 3.1.1: no pricing, plan, trial or billing words on this screen.
  expect(screen.queryByText(/pric|plan|billing|subscri|trial|pay|\$/i)).toBeNull();

  fireEvent.press(screen.getByText("Continue on thelightbulb.company"));
  expect(openURL).toHaveBeenCalledWith(`${WEB_ORIGIN}/onboarding`);
});

it("with exactly one workspace, activates it silently", async () => {
  mockMemberships = [membership("org_1", "Acme")];
  await renderGate();
  await waitFor(() => expect(mockSetActive).toHaveBeenCalledWith({ organization: "org_1" }));
  expect(mockSetActive).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("APP")).toBeNull();
  expect(screen.queryByText("Acme")).toBeNull();
});

it("if the silent activation fails, shows the one workspace to tap instead of spinning", async () => {
  mockMemberships = [membership("org_1", "Acme")];
  mockSetActive.mockRejectedValueOnce(new Error("offline"));
  await renderGate();
  expect(await screen.findByText("Acme")).toBeTruthy();
});

it("with two or more, lists them and activates the one picked, clearing cached reads", async () => {
  mockMemberships = [membership("org_1", "Acme"), membership("org_2", "Beta")];
  await renderGate();
  expect(screen.getByText("Acme")).toBeTruthy();
  expect(mockSetActive).not.toHaveBeenCalled();

  await act(async () => {
    fireEvent.press(screen.getByText("Beta"));
  });
  expect(mockSetActive).toHaveBeenCalledWith({ organization: "org_2" });
  expect(mockResetSwrCache).toHaveBeenCalledTimes(1);
});

it("re-reads memberships when the app returns to the foreground", async () => {
  await renderGate();
  await act(async () => onAppStateChange?.("active"));
  expect(mockRevalidate).toHaveBeenCalled();
});

