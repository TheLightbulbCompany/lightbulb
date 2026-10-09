import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";

const mockPut = jest.fn();
const mockPost = jest.fn();

jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok"), signOut: jest.fn() }),
}));

jest.mock("@lightbulb/shared", () => ({
  ...jest.requireActual("@lightbulb/shared"),
  createApiClient: () => ({ put: mockPut, post: mockPost }),
}));

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

// The real class — the mock above spreads requireActual, so `instanceof
// ApiError` inside the component matches what these tests throw.
import { ApiError } from "@lightbulb/shared";
import { ByoKeyScreen } from "../ByoKeyScreen";

const ADMIN_ONLY_MESSAGE = "Ask your workspace admin to finish this on thelightbulb.company.";

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

  // The secure keyboard can cover the Save button on smaller devices (the
  // card is vertically centered), so the whole surface must sit inside a
  // KeyboardAvoidingView — the OTA-safe, core-RN fix.
  it("wraps the form in a KeyboardAvoidingView so the keyboard can't cover Save", async () => {
    await act(async () => {
      render(<ByoKeyScreen onConnected={jest.fn()} />);
    });

    // The KeyboardAvoidingView carries this testID; getByTestId throws if it's
    // absent, so its presence is the assertion (a plain View wrapper wouldn't
    // lift the centered card above the secure keyboard).
    expect(screen.getByTestId("byo-keyboard-avoider")).toBeTruthy();
  });

  it("saves the key, commits the provider choice, and calls onConnected", async () => {
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
    // /billing/provider — NOT /users/sync, which the backend accepts and then
    // silently ignores, leaving byo_provider unset and the gate re-firing.
    expect(mockPost).toHaveBeenCalledWith("/billing/provider", {
      provider_choice: "byo_key",
      byo_provider: "anthropic",
    });
    // Order is required: change_provider 409s with byo_key_missing unless the
    // key PUT already landed.
    expect(mockPut.mock.invocationCallOrder[0]).toBeLessThan(
      mockPost.mock.invocationCallOrder[0],
    );
    await waitFor(() => expect(onConnected).toHaveBeenCalledTimes(1));
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
    expect(mockPost).toHaveBeenCalledWith("/billing/provider", {
      provider_choice: "byo_key",
      byo_provider: "openai",
    });
  });

  it("shows an error message when the key save fails and does not call onConnected", async () => {
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

  // REGRESSION GUARD. The provider write is what clears the
  // byo_provider_required gate; calling onConnected() without it re-polls
  // into the same gate and renders this screen again — an inescapable loop.
  it("never calls onConnected when the provider write fails", async () => {
    mockPut.mockResolvedValue({});
    mockPost.mockRejectedValue(new Error("network down"));
    const onConnected = jest.fn();

    await act(async () => {
      render(<ByoKeyScreen onConnected={onConnected} />);
    });

    await fireEvent.changeText(screen.getByPlaceholderText("sk-proj-…"), "sk-proj-abc");
    await fireEvent.press(screen.getByText("Save key"));

    await waitFor(() => expect(screen.getByText("network down")).toBeTruthy());
    expect(mockPut).toHaveBeenCalledTimes(1);
    expect(onConnected).not.toHaveBeenCalled();
  });

  // The gate reports admin_only=false for byo_provider_required, so a
  // non-admin org member reaches this screen — but both backend routes
  // require_org_admin, so one of them 403s. Status-mapped, so a 403 from
  // either call reads the same.
  it("tells a non-admin org member to ask their admin on a 403", async () => {
    mockPut.mockResolvedValue({});
    mockPost.mockRejectedValue(
      new ApiError(403, { detail: "Organization admin access required" }),
    );
    const onConnected = jest.fn();

    await act(async () => {
      render(<ByoKeyScreen onConnected={onConnected} />);
    });

    await fireEvent.changeText(screen.getByPlaceholderText("sk-proj-…"), "sk-proj-abc");
    await fireEvent.press(screen.getByText("Save key"));

    await waitFor(() => expect(screen.getByText(ADMIN_ONLY_MESSAGE)).toBeTruthy());
    expect(screen.queryByText("Organization admin access required")).toBeNull();
    expect(onConnected).not.toHaveBeenCalled();
  });

  // change_provider's 409s carry a structured {code, message} detail the
  // shared client does NOT lift onto ApiError.message (it only does that for
  // string details), so the raw message would render as "API 409".
  it("surfaces the message from a structured 409 detail", async () => {
    mockPut.mockResolvedValue({});
    mockPost.mockRejectedValue(
      new ApiError(
        409,
        {
          detail: {
            code: "subscription_inactive",
            message: "Subscription is not active. Manage it in /settings/billing.",
          },
        },
        "API 409",
      ),
    );
    const onConnected = jest.fn();

    await act(async () => {
      render(<ByoKeyScreen onConnected={onConnected} />);
    });

    await fireEvent.changeText(screen.getByPlaceholderText("sk-proj-…"), "sk-proj-abc");
    await fireEvent.press(screen.getByText("Save key"));

    await waitFor(() =>
      expect(
        screen.getByText("Subscription is not active. Manage it in /settings/billing."),
      ).toBeTruthy(),
    );
    expect(onConnected).not.toHaveBeenCalled();
  });

  it("leaves Save usable so a retry can resume after a transient failure", async () => {
    mockPut.mockResolvedValue({});
    mockPost.mockRejectedValueOnce(new Error("network down")).mockResolvedValueOnce({});
    const onConnected = jest.fn();

    await act(async () => {
      render(<ByoKeyScreen onConnected={onConnected} />);
    });

    await fireEvent.changeText(screen.getByPlaceholderText("sk-proj-…"), "sk-proj-abc");
    await fireEvent.press(screen.getByText("Save key"));
    await waitFor(() => expect(screen.getByText("network down")).toBeTruthy());
    expect(onConnected).not.toHaveBeenCalled();

    // Button is back (not stuck on the spinner) and still enabled.
    const save = screen.getByLabelText("Save key");
    expect(save.props.accessibilityState?.disabled).toBe(false);

    await fireEvent.press(save);
    await waitFor(() => expect(onConnected).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledTimes(2);
  });
});
