import { render, screen } from "@testing-library/react-native";
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

  it("renders children while starting (cold start, not yet ready)", async () => {
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

  // Parity check the other direction: the decision has NO owner-type branch
  // left at all (that's the point of this slice), so the same fixture shape
  // that used to be the ONLY eligible case (a personal trial_local owner)
  // must still reach the funnel post-rewrite — this isn't a special case
  // any more, just the same `ready && onboarded:false` check as everyone else.
  it("still shows the funnel for a personal trial_local-shaped owner (no regression from the old billing-probe behavior)", async () => {
    mockUseContainerStatusContext.mockReturnValue({ kind: "ready", onboarded: false });

    await render(
      <FirstRunGate>
        <Text>APP</Text>
      </FirstRunGate>,
    );

    expect(screen.getByText("Let’s get started.")).toBeTruthy();
    expect(screen.queryByText("APP")).toBeNull();
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
