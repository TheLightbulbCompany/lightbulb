import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { Text } from "react-native";
import { PROVISION_TIMEOUT_MS } from "@isol8/shared";

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

import { ProvisioningStepper, activeStepIndex } from "../ProvisioningStepper";

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
    // mount-stamped elapsed clock reads — so advancing timers advances elapsed.
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  const renderStepper = async (
    status: React.ComponentProps<typeof ProvisioningStepper>["status"],
    onRetry: () => void = jest.fn(),
  ) => {
    await render(
      <ProvisioningStepper status={status} onRetry={onRetry}>
        <Text>APP</Text>
      </ProvisioningStepper>,
    );
  };

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
});

describe("ProvisioningStepper soft timeout", () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
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
});
