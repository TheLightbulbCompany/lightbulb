import { describe, it, expect } from "vitest";
import { onStep, emitStep } from "../getStartedEvents";

// Light guard: the three real producer sites (chat send, task create, hire
// success) can't be unit-tested without their heavy host components, so this
// asserts the bus they all call delivers the right step ids in order.
describe("producers emit the right step", () => {
  it("emits chat/task/hire", () => {
    const seen: string[] = [];
    const off = onStep((id) => seen.push(id));
    emitStep("chat");
    emitStep("task");
    emitStep("hire");
    off();
    expect(seen).toEqual(["chat", "task", "hire"]);
  });
});
