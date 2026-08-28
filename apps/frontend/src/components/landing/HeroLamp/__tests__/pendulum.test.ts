import { describe, expect, it } from "vitest";
import { IMPULSE_CLAMP, SCROLL_IMPULSE, createPendulum } from "../pendulum";

describe("pendulum", () => {
  it("starts settled at rest", () => {
    const p = createPendulum();
    expect(p.angle).toBe(0);
    expect(p.settled()).toBe(true);
  });

  it("an impulse produces motion that decays back to rest", () => {
    const p = createPendulum();
    p.impulse(0.004);
    p.step();
    expect(p.settled()).toBe(false);
    let maxAngle = 0;
    for (let i = 0; i < 2000; i += 1) {
      p.step();
      maxAngle = Math.max(maxAngle, Math.abs(p.angle));
    }
    expect(maxAngle).toBeGreaterThan(0.01);
    expect(p.settled()).toBe(true);
    expect(p.angle).toBe(0);
  });

  it("clamps impulses so a fast scroll cannot fling the lamp", () => {
    const p = createPendulum();
    p.impulse(10);
    p.step();
    // one clamped kick must stay well under a quarter turn
    let maxAngle = 0;
    for (let i = 0; i < 400; i += 1) {
      p.step();
      maxAngle = Math.max(maxAngle, Math.abs(p.angle));
    }
    expect(maxAngle).toBeLessThan(0.08);
    expect(IMPULSE_CLAMP).toBe(0.006);
    expect(SCROLL_IMPULSE).toBe(0.00016);
  });

  it("reset returns to rest immediately", () => {
    const p = createPendulum();
    p.impulse(0.005);
    p.step();
    p.reset();
    expect(p.angle).toBe(0);
    expect(p.settled()).toBe(true);
  });
});
