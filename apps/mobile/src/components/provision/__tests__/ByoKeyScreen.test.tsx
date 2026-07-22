import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";

const mockPut = jest.fn();
const mockPost = jest.fn();

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok"), signOut: jest.fn() }),
}));

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  createApiClient: () => ({ put: mockPut, post: mockPost }),
}));

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

import { ByoKeyScreen } from "../ByoKeyScreen";

// NOTE: this @testing-library/react-native version's `fireEvent.press` /
// `fireEvent.changeText` are ASYNC (they internally `await act(...)` around
// the handler call) and MUST be awaited. An un-awaited fireEvent starts an
// act() scope without waiting for it to pop before the next statement runs;
// firing several in a row (or querying newly-rendered text right after one)
// races that scope and silently drops the resulting state update — which is
// what made every interaction in this file a no-op before this fix.
describe("ByoKeyScreen", () => {
  beforeEach(() => {
    mockPut.mockReset();
    mockPost.mockReset();
  });

  it("saves the key, syncs the provider choice, and calls onConnected", async () => {
    mockPut.mockResolvedValue({});
    mockPost.mockResolvedValue({});
    const onConnected = jest.fn();

    await act(async () => {
      render(<ByoKeyScreen onConnected={onConnected} />);
    });

    await fireEvent.press(screen.getByLabelText("Anthropic"));
    await fireEvent.changeText(screen.getByPlaceholderText("sk-ant-…"), "sk-ant-secret");
    await fireEvent.press(screen.getByText("Save key"));

    await waitFor(() =>
      expect(mockPut).toHaveBeenCalledWith("/settings/keys/anthropic", {
        api_key: "sk-ant-secret",
      }),
    );
    expect(mockPost).toHaveBeenCalledWith("/users/sync", {
      provider_choice: "byo_key",
      byo_provider: "anthropic",
    });
    await waitFor(() => expect(onConnected).toHaveBeenCalled());
  });

  it("defaults to openai and disables Save until a key is entered", async () => {
    mockPut.mockResolvedValue({});
    mockPost.mockResolvedValue({});

    await act(async () => {
      render(<ByoKeyScreen onConnected={jest.fn()} />);
    });

    await fireEvent.press(screen.getByText("Save key"));
    expect(mockPut).not.toHaveBeenCalled();

    await fireEvent.changeText(screen.getByPlaceholderText("sk-proj-…"), "sk-proj-abc");
    await fireEvent.press(screen.getByText("Save key"));

    await waitFor(() =>
      expect(mockPut).toHaveBeenCalledWith("/settings/keys/openai", {
        api_key: "sk-proj-abc",
      }),
    );
  });

  it("shows an error message when the save fails and does not call onConnected", async () => {
    mockPut.mockRejectedValue(new Error("key rejected"));
    const onConnected = jest.fn();

    await act(async () => {
      render(<ByoKeyScreen onConnected={onConnected} />);
    });

    await fireEvent.changeText(screen.getByPlaceholderText("sk-proj-…"), "sk-proj-bad");
    await fireEvent.press(screen.getByText("Save key"));

    await waitFor(() => expect(screen.getByText("key rejected")).toBeTruthy());
    expect(onConnected).not.toHaveBeenCalled();
    expect(mockPost).not.toHaveBeenCalled();
  });
});
