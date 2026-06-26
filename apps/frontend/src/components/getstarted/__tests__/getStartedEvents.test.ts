import { describe, it, expect, vi } from "vitest";
import { emitStep, onStep } from "../getStartedEvents";

describe("getStartedEvents", () => {
  it("delivers emitted step ids to subscribers and unsubscribes cleanly", () => {
    const cb = vi.fn();
    const off = onStep(cb);
    emitStep("chat");
    emitStep("hire");
    expect(cb).toHaveBeenCalledWith("chat");
    expect(cb).toHaveBeenCalledWith("hire");
    off();
    emitStep("task");
    expect(cb).toHaveBeenCalledTimes(2);
  });
});
