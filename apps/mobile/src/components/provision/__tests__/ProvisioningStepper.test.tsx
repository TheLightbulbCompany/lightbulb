import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { useEffect } from "react";
import { AccessibilityInfo, Alert, Text } from "react-native";
import { PROVISION_IDEAS, PROVISION_TIMEOUT_MS } from "@isol8/shared";

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

// The promoted stepper replaces the whole tab shell, so it carries a sign-out
// exit (same rule as ProvisionGate's Interstitial) — which means this
// component now calls useAuth and needs Clerk stubbed.
const mockSignOut = jest.fn();
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ signOut: mockSignOut }),
}));

import { ProvisioningStepper, activeStepIndex } from "../ProvisioningStepper";

type StepperStatus = React.ComponentProps<typeof ProvisioningStepper>["status"];

// The step rail is derived from `phase`/`substatus` alone (no gateway-health
// RPC — see the component's module docstring), so it is directly unit-testable.
describe("activeStepIndex", () => {
  it("provisioning with no substatus → container (0)", () => {
    expect(activeStepIndex("provisioning", undefined)).toBe(0);
  });

  it("provisioning + substatus task_running → gateway (1)", () => {
    expect(activeStepIndex("provisioning", "task_running")).toBe(1);
  });

  it("starting → agent (2)", () => {
    expect(activeStepIndex("starting", undefined)).toBe(2);
  });

  it("unknown / early phase stays conservatively on the first step", () => {
    expect(activeStepIndex(undefined, undefined)).toBe(0);
    // An unrecognized phase must not inherit the substatus shortcut.
    expect(activeStepIndex("something-new", "task_running")).toBe(0);
  });
});

describe("ProvisioningStepper time-box", () => {
  beforeEach(() => {
    // Modern fake timers also fake Date.now(), which is what the component's
    // elapsed clock reads — so advancing timers advances elapsed.
    jest.useFakeTimers();
    mockSignOut.mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  const renderStepper = (
    status: StepperStatus,
    onRetry: () => void = jest.fn(),
  ) =>
    render(
      <ProvisioningStepper status={status} onRetry={onRetry}>
        <Text>APP</Text>
      </ProvisioningStepper>,
    );

  it("keeps the thin banner over live children for a quick start", async () => {
    await renderStepper({ kind: "starting", phase: "provisioning" });

    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();

    // Still under PROMOTE_MS (10s) — a short start never flashes a takeover.
    await act(async () => {
      jest.advanceTimersByTime(9_000);
    });

    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();
  });

  it("promotes a slow start to the full-screen stepper past PROMOTE_MS", async () => {
    await renderStepper({
      kind: "starting",
      phase: "provisioning",
      substatus: "task_running",
    });

    await act(async () => {
      jest.advanceTimersByTime(10_000);
    });

    expect(screen.getByTestId("provision-dot-rail")).toBeTruthy();
    expect(screen.getByTestId("provision-title")).toBeTruthy();
    // substatus task_running → step 1 (gateway).
    expect(screen.getByText("Connecting to your agent's brain…")).toBeTruthy();
    // Promotion is BLOCKING: banner and live children are both gone.
    expect(screen.queryByText("Your workspace is starting…")).toBeNull();
    expect(screen.queryByText("APP")).toBeNull();
  });

  it("shows the running elapsed counter once promoted", async () => {
    await renderStepper({ kind: "starting", phase: "starting" });

    await act(async () => {
      jest.advanceTimersByTime(65_000);
    });

    expect(screen.getByTestId("provision-elapsed")).toBeTruthy();
    expect(screen.getByText("1:05")).toBeTruthy();
    // phase "starting" → the last step.
    expect(
      screen.getByText("Putting the finishing touches on your agent…"),
    ).toBeTruthy();
  });

  it("never promotes a parked wake, however long it runs", async () => {
    await renderStepper({ kind: "parked" });

    await act(async () => {
      jest.advanceTimersByTime(60_000);
    });

    expect(screen.getByText("Waking your agents…")).toBeTruthy();
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();
  });

  // This component hosts EVERY non-blocking state now (see its module
  // docstring), so the overwhelmingly common one — a healthy container — has
  // to be an inert pass-through.
  it("is a plain pass-through for ready: children only, no banner, no stepper", async () => {
    await renderStepper({ kind: "ready", onboarded: true });

    await act(async () => {
      jest.advanceTimersByTime(60_000);
    });

    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("Your workspace is starting…")).toBeNull();
    expect(screen.queryByText("Waking your agents…")).toBeNull();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();
  });

  // REGRESSION GUARD (review Important #1). A mount-scoped clock survived
  // parked → starting (same element type ⇒ no remount), so a free-tier wake
  // arrived at `starting` with the 10s promotion budget already spent and took
  // the full screen at t=0 — blanking live chat for the whole wake. The clock
  // must measure only the CURRENT contiguous `starting` run.
  it("gives a parked wake a FRESH promotion budget when it flips to starting", async () => {
    const { rerender } = await renderStepper({ kind: "parked" });

    // Long enough that an unreset clock would be well past PROMOTE_MS.
    await act(async () => {
      jest.advanceTimersByTime(12_000);
    });
    expect(screen.getByText("Waking your agents…")).toBeTruthy();

    // The wake lands: same element type, so this UPDATES the component rather
    // than remounting it — which is exactly how the bug survived.
    await rerender(
      <ProvisioningStepper
        status={{ kind: "starting", phase: "provisioning" }}
        onRetry={jest.fn()}
      >
        <Text>APP</Text>
      </ProvisioningStepper>,
    );

    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();

    // 9s into the NEW run — still banner, still live chat underneath.
    await act(async () => {
      jest.advanceTimersByTime(9_000);
    });
    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();

    // A wake that genuinely drags still promotes — 10s after it BEGAN.
    await act(async () => {
      jest.advanceTimersByTime(2_000);
    });
    expect(screen.getByTestId("provision-dot-rail")).toBeTruthy();
  });

  it("restarts the clock for a starting run that follows a ready spell", async () => {
    const { rerender } = await renderStepper({ kind: "ready", onboarded: true });

    await act(async () => {
      jest.advanceTimersByTime(120_000);
    });

    await rerender(
      <ProvisioningStepper
        status={{ kind: "starting", phase: "provisioning" }}
        onRetry={jest.fn()}
      >
        <Text>APP</Text>
      </ProvisioningStepper>,
    );

    // Two minutes of `ready` must not count toward the promotion budget.
    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();
  });
});

// REGRESSION GUARD (review Important #2). `children` here is the whole tab
// navigator (`<FirstRunGate><Tabs>…` from (app)/_layout.tsx). A gate that
// swapped its child between this component and a bare <View> changed the
// element TYPE on every non-blocking transition, so React tore the navigator
// down and rebuilt it — losing composer drafts, message lists and scroll
// position on each cold start. One element type for all five non-blocking
// states is what fixes that, and this is the property that proves it.
let childMounts = 0;
function MountCountingChild() {
  useEffect(() => {
    childMounts += 1;
  }, []);
  return <Text>APP</Text>;
}

describe("ProvisioningStepper children continuity", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    childMounts = 0;
    mockSignOut.mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  const tree = (status: StepperStatus) => (
    <ProvisioningStepper status={status} onRetry={jest.fn()}>
      <MountCountingChild />
    </ProvisioningStepper>
  );

  it("does not remount children across starting → ready", async () => {
    const { rerender } = await render(
      tree({ kind: "starting", phase: "provisioning" }),
    );

    expect(childMounts).toBe(1);
    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();

    await rerender(tree({ kind: "ready", onboarded: true }));

    // Same mount — the banner slot merely became `null`.
    expect(childMounts).toBe(1);
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText("Your workspace is starting…")).toBeNull();
  });

  it("does not remount children across loading → parked → starting", async () => {
    const { rerender } = await render(tree({ kind: "loading" }));
    expect(childMounts).toBe(1);

    await rerender(tree({ kind: "parked" }));
    expect(screen.getByText("Waking your agents…")).toBeTruthy();
    expect(childMounts).toBe(1);

    await rerender(tree({ kind: "starting", phase: "provisioning" }));
    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();
    expect(childMounts).toBe(1);
  });
});

describe("ProvisioningStepper rotating ideas", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    mockSignOut.mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  // Asserted on the REDUCE-MOTION path deliberately: it bypasses `Animated`
  // entirely and swaps the text outright, whereas the crossfade path's
  // `useNativeDriver: true` completion callback never fires under RN's jest
  // native-animated mock, so the index would never advance there.
  it("rotates the idea line on the shared cadence under reduce motion", async () => {
    jest
      .spyOn(AccessibilityInfo, "isReduceMotionEnabled")
      .mockResolvedValue(true);

    await render(
      <ProvisioningStepper
        status={{ kind: "starting", phase: "starting" }}
        onRetry={jest.fn()}
      >
        <Text>APP</Text>
      </ProvisioningStepper>,
    );

    // Promote — the ideas only exist on the full-screen stepper.
    await act(async () => {
      jest.advanceTimersByTime(10_000);
    });
    // Let the reduce-motion probe settle before the first rotation tick.
    await act(async () => {});

    expect(screen.getByText(PROVISION_IDEAS[0])).toBeTruthy();

    await act(async () => {
      jest.advanceTimersByTime(3_200);
    });

    expect(screen.getByText(PROVISION_IDEAS[1])).toBeTruthy();
    expect(screen.queryByText(PROVISION_IDEAS[0])).toBeNull();
  });
});

describe("ProvisioningStepper blocking-state exits", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    mockSignOut.mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it("surfaces the 'taking longer than usual' block with a Retry that calls onRetry", async () => {
    const onRetry = jest.fn();

    await render(
      <ProvisioningStepper
        status={{ kind: "starting", phase: "starting" }}
        onRetry={onRetry}
      >
        <Text>APP</Text>
      </ProvisioningStepper>,
    );

    // Under budget: promoted, but no soft-timeout block yet.
    await act(async () => {
      jest.advanceTimersByTime(30_000);
    });
    expect(screen.getByTestId("provision-dot-rail")).toBeTruthy();
    expect(screen.queryByTestId("provision-timeout")).toBeNull();

    await act(async () => {
      jest.advanceTimersByTime(PROVISION_TIMEOUT_MS - 30_000 + 1_000);
    });

    expect(screen.getByTestId("provision-timeout")).toBeTruthy();
    expect(screen.getByText("This is taking longer than usual.")).toBeTruthy();
    expect(screen.getByText("Contact support")).toBeTruthy();

    // RNTL v14's fireEvent is async (internal act()) — must be awaited or the
    // resulting act scope races the assertion below.
    await fireEvent.press(screen.getByText("Retry"));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  // The promoted stepper unmounts `children`, so it IS the whole app for as
  // long as it shows — a wrong-account user needs the same exit the gate's
  // Interstitial gives them. Retry + a mailto is not an exit.
  it("offers a sign-out exit on the promoted stepper", async () => {
    const alertSpy = jest.spyOn(Alert, "alert").mockImplementation(() => {});

    await render(
      <ProvisioningStepper
        status={{ kind: "starting", phase: "starting" }}
        onRetry={jest.fn()}
      >
        <Text>APP</Text>
      </ProvisioningStepper>,
    );

    // Not on the non-blocking banner — live chat is still reachable there.
    expect(screen.queryByText("Sign out")).toBeNull();

    await act(async () => {
      jest.advanceTimersByTime(10_000);
    });

    await fireEvent.press(screen.getByText("Sign out"));

    // Confirmed, not instant — mirrors the gate's Interstitial.
    expect(alertSpy).toHaveBeenCalledTimes(1);
    expect(mockSignOut).not.toHaveBeenCalled();

    const buttons = alertSpy.mock.calls[0]?.[2] ?? [];
    buttons.find((button) => button.text === "Sign out")?.onPress?.();
    expect(mockSignOut).toHaveBeenCalledTimes(1);
  });
});
