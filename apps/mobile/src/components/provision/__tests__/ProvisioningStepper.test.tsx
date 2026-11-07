import { render, screen } from "@testing-library/react-native";
import { useEffect } from "react";
import { AccessibilityInfo, Text } from "react-native";

jest.mock("react-native-safe-area-context", () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

import { ProvisioningStepper, activeStepIndex } from "../ProvisioningStepper";

type StepperStatus = React.ComponentProps<typeof ProvisioningStepper>["status"];

// The step count is derived from `phase`/`substatus` alone (no gateway-health RPC), so it is directly unit-testable.
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
  });
});

let childMounts = 0;
function MountCountingChild() {
  useEffect(() => {
    childMounts += 1;
  }, []);
  return <Text>APP</Text>;
}

describe("ProvisioningStepper", () => {
  beforeEach(() => {
    childMounts = 0;
    // usePulse runs an infinite Animated loop unless reduce motion is on; nothing here asserts animation.
    jest.spyOn(AccessibilityInfo, "isReduceMotionEnabled").mockResolvedValue(true);
  });
  afterEach(() => jest.restoreAllMocks());

  const tree = (status: StepperStatus) => (
    <ProvisioningStepper status={status}>
      <MountCountingChild />
    </ProvisioningStepper>
  );

  it("a start never takes over the screen; the chat stays open under the banner", async () => {
    await render(tree({ kind: "starting", phase: "provisioning", substatus: "task_running" }));
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.getByText(/Setting up, step 2 of \d\. You can write to Lightbulb now/)).toBeTruthy();
  });

  it.each([
    ["ready", { kind: "ready", onboarded: true }],
    ["parked", { kind: "parked" }],
    ["loading", { kind: "loading" }],
  ])("is a plain pass-through for %s: children only, no banner", async (_name, status) => {
    await render(tree(status as StepperStatus));
    expect(screen.getByText("APP")).toBeTruthy();
    expect(screen.queryByText(/Setting up/)).toBeNull();
  });

  it("does not remount children across loading → parked → starting → ready", async () => {
    const { rerender } = await render(tree({ kind: "loading" }));
    await rerender(tree({ kind: "parked" }));
    await rerender(tree({ kind: "starting", phase: "provisioning" }));
    expect(screen.getByText(/Setting up, step 1/)).toBeTruthy();
    await rerender(tree({ kind: "ready", onboarded: true }));
    expect(screen.queryByText(/Setting up/)).toBeNull();
    expect(childMounts).toBe(1);
  });
});
