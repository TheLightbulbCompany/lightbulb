import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import * as WebBrowser from "expo-web-browser";

const mockGet = jest.fn();
const mockPost = jest.fn();

jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok"), signOut: jest.fn() }),
}));

// Real useChatGPTOAuth (jest.requireActual) — only the API client factory is
// stubbed, so this test exercises the actual shared hook logic, same as
// FirstRunGate.test.tsx's established pattern.
jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ get: mockGet, post: mockPost }),
}));

jest.mock("expo-web-browser", () => ({
  openBrowserAsync: jest.fn(async () => ({ type: "dismiss" })),
}));

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

import { ChatGPTConnectScreen } from "../ChatGPTConnectScreen";

describe("ChatGPTConnectScreen", () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    (WebBrowser.openBrowserAsync as jest.Mock).mockClear();
  });

  it("shows the device code and opens the verification URL on press", async () => {
    mockGet.mockResolvedValue({ connected: false });
    mockPost.mockImplementation(async (endpoint: string) => {
      if (endpoint === "/oauth/chatgpt/start") {
        return {
          user_code: "ABCD-1234",
          verification_uri: "https://chatgpt.com/auth/device",
          expires_in: 900,
          interval: 5,
        };
      }
      // Poll never completes in this test — keeps state stably "pending".
      return { status: "pending" };
    });

    await act(async () => {
      render(<ChatGPTConnectScreen onConnected={jest.fn()} />);
    });

    await waitFor(() => expect(screen.getByText("ABCD-1234")).toBeTruthy());

    fireEvent.press(screen.getByText("Open sign-in page"));
    expect(WebBrowser.openBrowserAsync).toHaveBeenCalledWith(
      "https://chatgpt.com/auth/device",
    );
  });

  it("makes the code long-press-copyable and surfaces the expiry countdown", async () => {
    mockGet.mockResolvedValue({ connected: false });
    mockPost.mockImplementation(async (endpoint: string) => {
      if (endpoint === "/oauth/chatgpt/start") {
        return {
          user_code: "ABCD-1234",
          verification_uri: "https://chatgpt.com/auth/device",
          expires_in: 900,
          interval: 5,
        };
      }
      return { status: "pending" };
    });

    await act(async () => {
      render(<ChatGPTConnectScreen onConnected={jest.fn()} />);
    });

    await waitFor(() => expect(screen.getByText("ABCD-1234")).toBeTruthy());

    // Fix 1: long-press → OS "Copy" works without a clipboard binary (the
    // verification page opens in a modal covering this screen, so the code
    // must be liftable rather than retyped).
    expect(screen.getByText("ABCD-1234").props.selectable).toBe(true);

    // Fix 2: the ~15-min lifetime is visible as an M:SS countdown derived from
    // state.expiresAt, so the code doesn't silently die mid-flow.
    expect(screen.getByText(/Code expires in \d{1,2}:\d{2}/)).toBeTruthy();
  });

  it("calls onConnected once the poll reports completion", async () => {
    mockGet.mockResolvedValue({ connected: false });
    mockPost
      .mockResolvedValueOnce({
        user_code: "ABCD-1234",
        verification_uri: "https://chatgpt.com/auth/device",
        expires_in: 900,
        interval: 0.05,
      })
      .mockResolvedValueOnce({ status: "pending" })
      .mockResolvedValueOnce({ status: "completed", account_id: "acct_1" });
    const onConnected = jest.fn();

    await act(async () => {
      render(<ChatGPTConnectScreen onConnected={onConnected} />);
    });

    await waitFor(() => expect(onConnected).toHaveBeenCalled());
  });

  it("renders an error with a retry button that re-fires start()", async () => {
    mockGet.mockResolvedValue({ connected: false });
    mockPost.mockRejectedValue(new Error("OpenAI device-code failed"));

    await act(async () => {
      render(<ChatGPTConnectScreen onConnected={jest.fn()} />);
    });

    await waitFor(() =>
      expect(screen.getByText(/Connection failed/)).toBeTruthy(),
    );

    mockPost.mockReset();
    mockPost.mockResolvedValueOnce({
      user_code: "RETRY-999",
      verification_uri: "https://chatgpt.com/auth/device",
      expires_in: 900,
      interval: 5,
    });
    await act(async () => {
      fireEvent.press(screen.getByText("Try again"));
    });

    await waitFor(() => expect(screen.getByText("RETRY-999")).toBeTruthy());
  });
});
