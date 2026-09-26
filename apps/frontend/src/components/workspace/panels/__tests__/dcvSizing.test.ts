import { describe, expect, it } from "vitest";

import { fitDcvDisplay } from "../dcvSizing";

describe("fitDcvDisplay", () => {
  it("fills the width of a taller panel, top-aligned (no band above)", () => {
    expect(fitDcvDisplay(920, 600, 1920, 1080)).toEqual({
      scale: 920 / 1920,
      offsetX: 0,
      offsetY: 0,
    });
  });

  it("centers a narrow remote display within a wide panel", () => {
    expect(fitDcvDisplay(1200, 600, 800, 1200)).toEqual({
      scale: 0.5,
      offsetX: 400,
      offsetY: 0,
    });
  });
});
