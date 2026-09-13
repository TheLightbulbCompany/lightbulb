import { colors } from "../theme";

/** `#rrggbb` -> `[r, g, b]`, 0-255 each. */
function rgb(hex: string): [number, number, number] {
  const value = Number.parseInt(hex.replace("#", ""), 16);
  return [(value >> 16) & 0xff, (value >> 8) & 0xff, value & 0xff];
}

/** Per-channel delta from `cream`, as a fraction of 255 (matches how the
 * token's own comment in theme.ts measures it). */
function deltaFromCream(hex: string): number[] {
  const [cr, cg, cb] = rgb(colors.cream);
  const [r, g, b] = rgb(hex);
  return [Math.abs(cr - r), Math.abs(cg - g), Math.abs(cb - b)].map((d) => d / 255);
}

it("resolves the row-pressed token to a real, visible step off cream (not the old ~2% tint)", () => {
  expect(colors.rowPressed).not.toBe("#fafafa");
  const delta = deltaFromCream(colors.rowPressed);
  for (const d of delta) expect(d).toBeGreaterThanOrEqual(0.08);
});

it("keeps a pressed row darker than a selected one, so selection still reads under a press", () => {
  const pressedDelta = Math.min(...deltaFromCream(colors.rowPressed));
  const activeDelta = Math.min(...deltaFromCream(colors.rowActive));
  expect(pressedDelta).toBeGreaterThan(activeDelta);
});
