import { describe, expect, it } from "vitest";

import { fitDcvDisplay } from "../dcvSizing";

describe("fitDcvDisplay", () => {
  it("fits a 16:9 remote browser into a smaller panel without cropping", () => {
    expect(fitDcvDisplay(920, 600, 1920, 1080)).toEqual({
      scale: 920 / 1920,
      offsetX: 0,
      offsetY: 41.25,
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
