import { render, screen, userEvent } from "@testing-library/react-native";
import { Text } from "react-native";

// NOTE (mobile onboarding-parity Slice 2): FirstRunGate no longer probes
// /billing/account — it reads the SAME /container/status poll ProvisionGate
// already runs, via useContainerStatusContext(). Mocking conventions follow
// FirstRunFunnel.test.tsx (the funnel renders for real in the "eligible"
// cases below, so it needs the same @clerk/clerk-expo / safe-area-context /
// @isol8/shared mocks that file uses). Not run to green as part of this
// task per controller policy — full suite runs once at branch end.
const mockUpdate = jest.fn(async () => undefined);
type MockUser = { id: string; unsafeMetadata: Record<string, unknown>; update: typeof mockUpdate } | null;
let mockUser: MockUser = { id: "user_1", unsafeMetadata: {}, update: mockUpdate };

const mockUseContainerStatusContext = jest.fn();

jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ getToken: jest.fn(async () => "tok") }),
  useUser: () => ({ user: mockUser }),
}));

// useSafeAreaInsets (used by FirstRunFunnel) throws without a provider.
jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

jest.mock("@isol8/shared", () => ({
  ...jest.requireActual("@isol8/shared"),
  createApiClient: () => ({ get: jest.fn(), post: jest.fn() }),
}));

// Full replace (not a requireActual spread) is safe here: FirstRunGate only
// imports useContainerStatusContext from this module, never the Context
// object itself.
jest.mock("../../../lib/containerStatusContext", () => ({
  useContainerStatusContext: () => mockUseContainerStatusContext(),
}));

import { FirstRunGate } from "../FirstRunGate";

describe("FirstRunGate", () => {
  beforeEach(() => {
    mockUser = { id: "user_1", unsafeMetadata: {}, update: mockUpdate };
    mockUpdate.mockClear();
    mockUseContainerStatusContext.mockReset();
  });

  it("renders children when there is no Clerk user yet (auth still loading)", async () => {
    mockUser = null;
    mockUseContainerStatusContext.mockReturnValue({ kind: "ready", onboarded: false });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("Let’s get started.")).toBeNull();
  });

  it("renders children when no ContainerStatusContext provider is above it (context null)", async () => {
    mockUseContainerStatusContext.mockReturnValue(null);

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
  });

  it("renders children while loading (transient, not yet ready)", async () => {
    mockUseContainerStatusContext.mockReturnValue({ kind: "loading" });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
  });

  it("renders children while starting (cold start — the latch only arms at ready)", async () => {
    mockUseContainerStatusContext.mockReturnValue({ kind: "starting", phase: "agent" });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
  });

  it("renders children when the container status is unreachable — never traps the user on a transient error", async () => {
    mockUseContainerStatusContext.mockReturnValue({
      kind: "unreachable",
      message: "network down",
    });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("Let’s get started.")).toBeNull();
  });

  // Before this slice, eligibility came from a /billing/account probe whose
  // status is always non-"trial_local" for an org owner (they never have a
  // trial row at all) — so org owners never reached the funnel. This is the
  // parity fix: the decision now only reads the per-CONTAINER `onboarded`
  // fact (same signal the web's ProvisioningStepper uses), which an org
  // owner's ready container carries exactly like a personal owner's would.
  // The "subscription_status: active" that would sit on this owner's real
  // billing row plays no part in the decision any more — FirstRunGate
  // doesn't read billing at all now.
  it("shows the funnel for a ready container with onboarded:false, for an org owner exactly like any other owner", async () => {
    mockUseContainerStatusContext.mockReturnValue({ kind: "ready", onboarded: false });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("Let’s get started.")).toBeTruthy();
    expect(screen.queryByText("APP")).toBeNull();
  });

  // PINNED DECISION — do not "fix" this into showing the funnel. The backend
  // stamps `onboarded` on every 200, including provisioning bodies, and web
  // does open its funnel during a cold start. Mobile deliberately doesn't:
  // web's funnel check runs BEFORE its stepper, while mobile's stepper WRAPS
  // this gate — so a funnel opened pre-ready gets unmounted the moment
  // ProvisioningStepper promotes at PROMOTE_MS, discarding the in-progress
  // brief. That is the same failure this file's Important-3 guard exists to
  // prevent, just smaller. The cost is a pre-ready glimpse of the tab shell,
  // during which the container isn't up and chat doesn't work anyway; the
  // door closes as soon as `ready` lands (covered by the ready case above).
  it("does NOT open the funnel for a starting body carrying onboarded:false before any ready", async () => {
    mockUseContainerStatusContext.mockReturnValue({
      kind: "starting",
      phase: "provisioning",
      onboarded: false,
    });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("Let’s get started.")).toBeNull();
  });

  it("renders children for a ready container with onboarded:true", async () => {
    mockUseContainerStatusContext.mockReturnValue({ kind: "ready", onboarded: true });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("Let’s get started.")).toBeNull();
  });

  it("renders children for a ready container with onboarded left undefined (unknown → don't show, don't block)", async () => {
    mockUseContainerStatusContext.mockReturnValue({ kind: "ready" });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("Let’s get started.")).toBeNull();
  });

  // REGRESSION GUARD (whole-branch review, Important #3). Eligibility used to
  // be re-derived from the LIVE polled status, so any transition off `ready`
  // unmounted the funnel mid-typing and rendered the tab shell instead: a
  // free-tier idle reap (`parked`), the wake after it (`starting`), or ONE
  // failed request (`unreachable` — a network blip on resume, a transient
  // token refresh, any 5xx). That threw away the in-progress brief AND let
  // the user past a door that is supposed to have no skip. The latch arms at
  // the first `ready` and nothing disarms it, so none of these touch it.
  it("keeps an in-progress funnel — and what the user already typed — across parked / starting / unreachable", async () => {
    mockUseContainerStatusContext.mockReturnValue({ kind: "ready", onboarded: false });

    // userEvent, not fireEvent: presses don't reach this screen's Pressables
    // through its ScrollView (see FirstRunFunnel.test.tsx).
    const u = userEvent.setup();
    const { rerender } = await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );
    expect(screen.getByText("Let’s get started.")).toBeTruthy();

    // Get the user mid-brief — this is the state the bug destroyed.
    await u.press(screen.getByRole("button", { name: "Start something new" }));
    await u.type(
      screen.getByPlaceholderText("e.g. A social media agency for small restaurants"),
      "tea box",
    );
    expect(screen.getByDisplayValue("tea box")).toBeTruthy();

    const transients = [
      { kind: "parked" },
      { kind: "starting", phase: "provisioning" },
      { kind: "unreachable", message: "network down" },
      { kind: "loading" },
      null,
    ];

    for (const transient of transients) {
      mockUseContainerStatusContext.mockReturnValue(transient);
      await rerender(
        <FirstRunGate>
          <Text>APP</Text>
        </FirstRunGate>,
      );
      // Still the funnel, still on the same step, still holding the draft —
      // a remount would have reset `input` to "" even if the funnel itself
      // came back.
      expect(screen.getByDisplayValue("tea box")).toBeTruthy();
      expect(screen.queryByText("APP")).toBeNull();
    }
  });

  // The latch must still be a latch and not a one-way door: a poll that says
  // the container IS onboarded (someone else on the org finished the funnel,
  // or this user's submit landed) has to close it.
  it("drops the funnel once a poll reports onboarded:true", async () => {
    mockUseContainerStatusContext.mockReturnValue({ kind: "ready", onboarded: false });

    const { rerender } = await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );
    expect(screen.getByText("Let’s get started.")).toBeTruthy();

    mockUseContainerStatusContext.mockReturnValue({ kind: "ready", onboarded: true });
    await rerender(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("Let’s get started.")).toBeNull();
  });

  // The blocking states never reach this component in the app (ProvisionGate
  // replaces the whole tree for them, so `children` — this gate — never
  // mounts), and they can't arm the latch either: it only moves on a 200
  // body's `onboarded`. Pinned so a future "read the status directly" edit
  // can't quietly put the funnel in front of a gated user.
  it.each([["blocked"], ["needsSignup"], ["teamPending"]])(
    "never shows the funnel for a %s status",
    async (kind) => {
      mockUseContainerStatusContext.mockReturnValue({ kind });

      await render(
        <FirstRunGate>
          <Text>APP</Text>
        </FirstRunGate>,
      );

      expect(screen.getByText("APP")).toBeTruthy();
      expect(screen.queryByText("Let’s get started.")).toBeNull();
    },
  );

  it("keeps the briefSubmitted local latch: suppresses the funnel even when onboarded:false", async () => {
    mockUser = {
      id: "user_1",
      unsafeMetadata: { firstRun: { briefSubmitted: true } },
      update: mockUpdate,
    };
    mockUseContainerStatusContext.mockReturnValue({ kind: "ready", onboarded: false });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("Let’s get started.")).toBeNull();
  });
});
