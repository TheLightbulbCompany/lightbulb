import { act, render, screen, waitFor } from "@testing-library/react-native";

const mockCreate = jest.fn();
const mockSetActive = jest.fn();
const mockSignOut = jest.fn();
const mockReplace = jest.fn();
let mockParams: { ticket?: string; returnTo?: string } = {};

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ signOut: mockSignOut }),
  useSignIn: () => ({
    isLoaded: true,
    signIn: { create: mockCreate },
    setActive: mockSetActive,
  }),
}));
jest.mock("expo-router", () => ({
  useLocalSearchParams: () => mockParams,
  useRouter: () => ({ replace: mockReplace }),
}));

// eslint-disable-next-line import/first
import AuthHandoff from "../auth";

beforeEach(() => {
  jest.clearAllMocks();
  mockParams = { ticket: "one-time-ticket", returnTo: "/(app)/(tabs)/chats" };
});

it("activates the browser-authenticated Clerk session before entering the app", async () => {
  mockCreate.mockResolvedValue({ status: "complete", createdSessionId: "session_123" });

  await act(async () => {
    render(<AuthHandoff />);
  });

  await waitFor(() => {
    expect(mockCreate).toHaveBeenCalledWith({
      strategy: "ticket",
      ticket: "one-time-ticket",
    });
  });
  expect(mockSignOut).toHaveBeenCalledTimes(1);
  expect(mockSignOut.mock.invocationCallOrder[0]).toBeLessThan(
    mockCreate.mock.invocationCallOrder[0],
  );
  expect(mockSetActive).toHaveBeenCalledWith({ session: "session_123" });
  expect(mockReplace).toHaveBeenCalledWith("/(app)/(tabs)/chats");
});

it("rejects an external return destination", async () => {
  mockParams.returnTo = "https://attacker.example/steal";
  mockCreate.mockResolvedValue({ status: "complete", createdSessionId: "session_123" });

  await act(async () => {
    render(<AuthHandoff />);
  });

  await waitFor(() => expect(mockSetActive).toHaveBeenCalled());
  expect(mockReplace).toHaveBeenCalledWith("/");
});

it("keeps a failed ticket exchange on a recoverable sign-in screen", async () => {
  mockCreate.mockRejectedValue(new Error("expired ticket"));

  await act(async () => {
    render(<AuthHandoff />);
  });

  expect(await screen.findByText("Unable to finish signing you in.")).toBeTruthy();
  expect(mockSignOut).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Sign in in the app")).toBeTruthy();
});
