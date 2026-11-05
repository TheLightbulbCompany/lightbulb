import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { useEffect } from "react";
import { AccessibilityInfo, Alert, Text } from "react-native";
import {
  PROVISION_IDEAS,
  PROVISION_STUCK_MS,
  PROVISION_TIMEOUT_MS,
} from "@lightbulb/shared";

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

// The promoted stepper replaces the whole tab shell, so it carries a sign-out
// exit (same rule as ProvisionGate's Interstitial) — which means this
// component now calls useAuth and needs Clerk stubbed.
const mockSignOut = jest.fn();
jest.mock("@clerk/expo", () => ({
  useAuth: () => ({ signOut: mockSignOut }),
}));

let mockSuperAgent = false;
jest.mock("../../../providers/live-events", () => ({ useSuperAgent: () => mockSuperAgent }));

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

/**
 * Hold every `usePulse` static for the cases that advance the clock.
 *
 * With reduce motion OFF (the RN jest preset's default) `usePulse` starts an
 * INFINITE `Animated.loop`, and the preset's NativeAnimatedModule mock ends
 * each timing step with a `setTimeout(…, 16)`. So every second of advanced
 * fake time costs ~60 loop iterations of timer + Animated work, and the cases
 * below advance tens of seconds — up to the 5-minute soft timeout — with a
 * banner or the ring mounted. None of them assert anything about animation
 * (they read text and testIDs), so all of that is pure cost; `usePulse`
 * short-circuits to a static opacity under reduce motion and schedules
 * nothing at all.
 */
function holdAnimationsStatic() {
  jest.spyOn(AccessibilityInfo, "isReduceMotionEnabled").mockResolvedValue(true);
}

describe("ProvisioningStepper time-box", () => {
  beforeEach(() => {
    // Modern fake timers also fake Date.now(), which is what the component's
    // elapsed clock reads — so advancing timers advances elapsed.
    jest.useFakeTimers();
    holdAnimationsStatic();
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

  it("super-agent on: a slow start never takes over the screen; the chat stays open under the banner", async () => {
    mockSuperAgent = true;
    try {
      await renderStepper({ kind: "starting", phase: "provisioning", substatus: "task_running" });
      await act(async () => {
        jest.advanceTimersByTime(60_000);
      });
      expect(screen.getByText("APP")).toBeTruthy();
      expect(screen.queryByTestId("provision-dot-rail")).toBeNull();
      expect(screen.getByText(/Setting up, step 2 of \d\. You can write to Lightbulb now/)).toBeTruthy();
    } finally { mockSuperAgent = false; }
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

  it("shows a parked workspace as a plain pass-through, however long it stays parked", async () => {
    await renderStepper({ kind: "parked" });

    await act(async () => {
      jest.advanceTimersByTime(60_000);
    });

    expect(screen.queryByText("Waking your agents…")).toBeNull();
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

  // REGRESSION GUARD (whole-branch review, Important #1). A free-tier wake
  // reads `parked` for exactly ONE poll and then sits in `starting` for the
  // 30-90s of ECS placement / ENI attach / gateway handshake: the wake
  // dispatch writes status="provisioning" immediately and the backend maps
  // every non-running status to phase="provisioning". Promoting anywhere in
  // that window unmounts `<FirstRunGate><Tabs>…` — composer draft, message
  // list, scroll position and navigation stack all gone, and the user lands
  // back on the Chat tab when it remounts. A per-run clock alone only bought
  // 10 more seconds; the fix is web's `reachedReady` latch — once this mount
  // has seen ready or parked, the full-screen stepper is off the table.
  it("never promotes the starting run that follows a parked wake, however long it drags", async () => {
    const { rerender } = await renderStepper({ kind: "parked" });

    await act(async () => {
      jest.advanceTimersByTime(12_000);
    });
    expect(screen.queryByText("Waking your agents…")).toBeNull();

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

    // A whole realistic wake later — thin banner, live children, still.
    await act(async () => {
      jest.advanceTimersByTime(90_000);
    });
    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();
  });

  // Same latch, the other entry point: an idle scale-to-zero restart, or the
  // documented false `starting` that /container/status reports when the
  // answering backend task holds no pool connection. Both arrive over a chat
  // the user was just using, so neither may take the screen.
  it("never promotes a starting run once this mount has seen ready", async () => {
    const { rerender } = await renderStepper({ kind: "ready", onboarded: true });

    await rerender(
      <ProvisioningStepper
        status={{ kind: "starting", phase: "provisioning" }}
        onRetry={jest.fn()}
      >
        <Text>APP</Text>
      </ProvisioningStepper>,
    );

    await act(async () => {
      jest.advanceTimersByTime(120_000);
    });

    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();
  });

  // The clock is scoped to the current contiguous run of ONE kind, and this
  // is the transition where that still matters: `loading` is where a first
  // cold start begins, and a mount-scoped clock would hand the following
  // `starting` run a budget that was already spent on the initial probe.
  // (The parked → starting case above no longer proves this — the latch stops
  // that one from promoting at all.)
  it("spends the promotion budget on the starting run alone, not on the loading probe before it", async () => {
    const { rerender } = await renderStepper({ kind: "loading" });

    // Long enough that an unreset clock would be well past PROMOTE_MS.
    await act(async () => {
      jest.advanceTimersByTime(12_000);
    });

    await rerender(
      <ProvisioningStepper
        status={{ kind: "starting", phase: "provisioning" }}
        onRetry={jest.fn()}
      >
        <Text>APP</Text>
      </ProvisioningStepper>,
    );

    // 9s into the NEW run — still banner, still live children.
    await act(async () => {
      jest.advanceTimersByTime(9_000);
    });
    expect(screen.getByText("Your workspace is starting…")).toBeTruthy();
    expect(screen.queryByTestId("provision-dot-rail")).toBeNull();

    // A FIRST cold start that genuinely drags does still earn the stepper —
    // nothing is mounted behind it to lose, and it is the only surface with a
    // phase, a clock and a support exit.
    await act(async () => {
      jest.advanceTimersByTime(2_000);
    });
    expect(screen.getByTestId("provision-dot-rail")).toBeTruthy();
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
    expect(screen.queryByText("Waking your agents…")).toBeNull();
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
    // The soft-timeout case advances the full 5-minute PROVISION_TIMEOUT_MS
    // with the ring pulsing — see holdAnimationsStatic.
    holdAnimationsStatic();
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

/**
 * THE COVERAGE HOLE this PR fills.
 *
 * `bootstrap.state` was surfaced on the `ready` variant alone, and a container
 * whose gateway never registers composes to `starting` — so the failure notice
 * was unreachable in exactly the case it was written for. A dev owner sat on
 * this screen for 21 hours with the reason arriving on every poll and read by
 * nothing; four prod owners are wedged the same way.
 */
describe("ProvisioningStepper bounds the wait honestly", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    holdAnimationsStatic();
    mockSignOut.mockReset();
  });

  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  const renderColdStart = async (onRetry: () => void = jest.fn()) =>
    await render(
      <ProvisioningStepper
        status={{ kind: "starting", phase: "starting" }}
        onRetry={onRetry}
      >
        <Text>APP</Text>
      </ProvisioningStepper>,
    );

  it("escalates to a second tier at 15 minutes", async () => {
    await renderColdStart();

    await act(async () => {
      jest.advanceTimersByTime(PROVISION_TIMEOUT_MS + 1_000);
    });
    expect(screen.getByText("This is taking longer than usual.")).toBeTruthy();

    await act(async () => {
      jest.advanceTimersByTime(PROVISION_STUCK_MS - PROVISION_TIMEOUT_MS);
    });
    expect(screen.queryByText("This is taking longer than usual.")).toBeNull();
    expect(
      screen.getByText(/something is stuck on our side/),
    ).toBeTruthy();
  });

  // Web's equivalent button cleared its own escalation flag AND zeroed the
  // clock, so a diligent owner deleted their own escalation and 21 hours kept
  // reading as five minutes. Mobile's Retry only refetches — pin that.
  it("Retry re-polls without erasing the escalation or the clock", async () => {
    const onRetry = jest.fn();
    await renderColdStart(onRetry);

    await act(async () => {
      jest.advanceTimersByTime(PROVISION_STUCK_MS + 1_000);
    });
    expect(screen.getByText(/something is stuck on our side/)).toBeTruthy();

    await fireEvent.press(screen.getByLabelText("Retry"));

    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/something is stuck on our side/)).toBeTruthy();
    expect(screen.queryByText("0:00")).toBeNull();
  });

  // Inherited from the shared formatter (web's PR), pinned here because this
  // is the screen that rendered "1260:37 · cold starts can take a few minutes".
  it("rolls the counter to h:mm:ss instead of a four-digit minute count", async () => {
    await renderColdStart();

    await act(async () => {
      jest.advanceTimersByTime(3_600_000 + 5_000);
    });

    expect(screen.getByTestId("provision-elapsed")).toHaveTextContent(/^1:00:0\d$/);
  });
});
