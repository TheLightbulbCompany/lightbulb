import { render, screen, userEvent, waitFor } from "@testing-library/react-native";

const mockPost = jest.fn();
const mockUpdate = jest.fn(async () => undefined);
const mockSignOut = jest.fn();
const mockUser = {
  id: "user_1",
  unsafeMetadata: {},
  update: mockUpdate,
};

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok"), signOut: mockSignOut }),
  useUser: () => ({ user: mockUser }),
}));

// useSafeAreaInsets throws without a provider.
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  createApiClient: () => ({ post: mockPost }),
}));

import { FirstRunFunnel } from "../FirstRunFunnel";

// NOTE: fireEvent.press does not reach Pressable through this screen's
// ScrollView — userEvent.press (the recommended API) does.
describe("FirstRunFunnel", () => {
  beforeEach(() => {
    mockPost.mockReset().mockResolvedValue({ status: "staged" });
    mockUpdate.mockClear();
    mockSignOut.mockClear();
  });

  it("submits the new-business idea and stamps briefSubmitted", async () => {
    const u = userEvent.setup();
    await render(<FirstRunFunnel />);
    expect(screen.getByText("Let’s get started.")).toBeTruthy();

    await u.press(screen.getByRole("button", { name: "Start something new" }));
    await u.type(
      screen.getByPlaceholderText("e.g. A social media agency for small restaurants"),
      "A tea subscription box",
    );
    await u.press(screen.getByRole("button", { name: "Continue →" }));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/onboarding/brief", {
        path: "new",
        input: "A tea subscription box",
        detail: undefined,
      }),
    );
    await waitFor(() =>
      expect(mockUpdate).toHaveBeenCalledWith({
        unsafeMetadata: { firstRun: { briefSubmitted: true, path: "new" } },
      }),
    );
  });

  it('submits the "you pick" path with no input at all', async () => {
    const u = userEvent.setup();
    await render(<FirstRunFunnel />);
    await u.press(screen.getByRole("button", { name: "Start something new" }));
    await u.press(
      screen.getByRole("button", { name: "I don’t know yet — you pick" }),
    );

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/onboarding/brief", {
        path: "new",
        input: undefined,
        detail: undefined,
      }),
    );
  });

  it("submits the grow path with the website", async () => {
    const u = userEvent.setup();
    await render(<FirstRunFunnel />);
    await u.press(screen.getByRole("button", { name: "Grow my business" }));
    await u.type(screen.getByPlaceholderText("yourcompany.com"), "example.com");
    await u.press(screen.getByRole("button", { name: "Continue →" }));

    await waitFor(() =>
      expect(mockPost).toHaveBeenCalledWith("/onboarding/brief", {
        path: "grow",
        input: "example.com",
        detail: undefined,
      }),
    );
  });

  // The funnel is a committed front door with no skip; a persistently-failing
  // brief must still leave a wrong-account user an exit. A non-409 failure
  // lands straight on the error branch (no retry delay), which now carries the
  // same confirm-then-signOut escape every provision screen has.
  it("offers a Sign out escape on the error branch that calls Clerk signOut", async () => {
    mockPost.mockReset().mockRejectedValue(new Error("brief failed"));
    const { Alert } = jest.requireActual("react-native");
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});
    const u = userEvent.setup();

    await render(<FirstRunFunnel />);
    await u.press(screen.getByRole("button", { name: "Grow my business" }));
    await u.type(screen.getByPlaceholderText("yourcompany.com"), "example.com");
    await u.press(screen.getByRole("button", { name: "Continue →" }));

    const signOutBtn = await screen.findByRole("button", { name: "Sign out" });
    await u.press(signOutBtn);

    // Confirm dialog fires; the destructive button drives Clerk signOut.
    const [title, , buttons] = alertSpy.mock.calls[0] as [
      string,
      string,
      { text: string; style?: string; onPress?: () => void }[],
    ];
    expect(title).toBe("Sign out");
    buttons.find((b) => b.text === "Sign out")?.onPress?.();
    expect(mockSignOut).toHaveBeenCalledTimes(1);

    alertSpy.mockRestore();
  });
});
