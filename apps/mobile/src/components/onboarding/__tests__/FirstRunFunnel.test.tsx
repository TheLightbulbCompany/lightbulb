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
    // Reset to a clean first-time user so tests that seed stale prior-cycle
    // firstRun flags don't leak into the others (mockUser is shared).
    mockUser.unsafeMetadata = {};
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
    // briefSubmitted is a write-only optimistic pre-poll paint cache (spec
    // PR-6, BOOTSTRAP) — the kickoff itself is server-dispatched now, so
    // there is no started/completed client latch to write any more.
    await waitFor(() =>
      expect(mockUpdate).toHaveBeenCalledWith({
        unsafeMetadata: {
          firstRun: {
            briefSubmitted: true,
            path: "new",
          },
        },
      }),
    );
  });

  it("re-arms briefSubmitted from a prior provision (account wipe / reprovision)", async () => {
    // Cross-provision staleness: the user completed a PREVIOUS first-run
    // cycle, then their account/container was wiped and rebuilt. Clerk
    // metadata is per-user forever, so `briefSubmitted` from that prior
    // cycle survives — while the gate (keying off the fresh container's
    // onboarded:false / funnel_state on the backend) correctly re-shows
    // this funnel. A brief submit re-arms `briefSubmitted` for the NEW
    // cycle. The kickoff itself is server-owned now (spec PR-6) and fenced
    // on the backend's own onboarding_generation, so there is no
    // started/completed client latch left to reset here.
    mockUser.unsafeMetadata = {
      firstRun: { briefSubmitted: true },
    };
    const u = userEvent.setup();
    await render(<FirstRunFunnel />);

    await u.press(screen.getByRole("button", { name: "Grow my business" }));
    await u.type(screen.getByPlaceholderText("yourcompany.com"), "example.com");
    await u.press(screen.getByRole("button", { name: "Continue →" }));

    await waitFor(() =>
      expect(mockUpdate).toHaveBeenCalledWith({
        unsafeMetadata: {
          firstRun: expect.objectContaining({
            briefSubmitted: true,
          }),
        },
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
  it("posts exactly once against a current backend", async () => {
    // The endpoint is durable intake: it records the answer and returns, so
    // there is nothing for the client to wait out. The loop this replaces gave
    // up after ~16s while the Paperclip org seed takes ~33s, so every user who
    // typed fast enough saw an error for a signup that was working.
    const u = userEvent.setup();
    await render(<FirstRunFunnel />);
    await u.press(screen.getByRole("button", { name: "Grow my business" }));
    await u.type(screen.getByPlaceholderText("yourcompany.com"), "example.com");
    await u.press(screen.getByRole("button", { name: "Continue →" }));

    await waitFor(() => expect(mockUpdate).toHaveBeenCalled());
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it("does not dismiss the funnel on a 2xx body with no recognised status", async () => {
    // Deploy-skew guard: an OLDER backend 202s "team workspace provisioning"
    // while Paperclip warms, and the api client resolves any 2xx. Dismissing
    // on that would discard the answer, so the body is still checked. The
    // skew shim retries first, then the error branch takes over — and
    // briefSubmitted must NOT be stamped, or the funnel is gone for good with
    // nothing saved.
    jest.useFakeTimers();
    try {
      mockPost.mockReset().mockResolvedValue({ status: "provisioning" });
      const u = userEvent.setup({ advanceTimers: jest.advanceTimersByTime });
      await render(<FirstRunFunnel />);
      await u.press(screen.getByRole("button", { name: "Grow my business" }));
      await u.type(screen.getByPlaceholderText("yourcompany.com"), "example.com");
      await u.press(screen.getByRole("button", { name: "Continue →" }));

      await waitFor(() => expect(mockPost).toHaveBeenCalled());
      await jest.advanceTimersByTimeAsync(20000);

      // "Retry" only renders on the error branch (the copy around it is split
      // across Text nodes, so match the link rather than the sentence).
      expect(await screen.findByText("Retry")).toBeTruthy();
      expect(mockUpdate).not.toHaveBeenCalled();
    } finally {
      jest.useRealTimers();
    }
  });

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
