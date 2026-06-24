// Ported from upstream Paperclip's color-contrast.ts
// (paperclip/ui/src/lib/color-contrast.ts) (MIT, (c) 2025 Paperclip AI).

/**
 * Shared color-contrast utilities for pill / badge / chip components.
 *
 * Uses WCAG 2.1 relative-luminance contrast ratios so text is always
 * readable, even on semi-transparent backgrounds composited over the
 * editorial cream page background. The Teams surface is single-theme
 * (cream/forest editorial) — there is no dark mode, so the page background
 * is always cream and the text pair is the editorial ink/cream palette.
 */

// Editorial page background (--cream, #faf7f2) — the surface every pill
// composites over.
const PAGE_BG = { r: 250, g: 247, b: 242 };

export function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const match = /^#?([0-9a-f]{3,6})$/i.exec(hex.trim());
  if (!match) return null;
  let value = match[1];
  if (value.length === 3) {
    value = value
      .split("")
      .map((c) => `${c}${c}`)
      .join("");
  }
  if (value.length !== 6) return null;
  return {
    r: parseInt(value.slice(0, 2), 16),
    g: parseInt(value.slice(2, 4), 16),
    b: parseInt(value.slice(4, 6), 16),
  };
}

function relativeLuminanceChannel(value: number): number {
  const normalized = value / 255;
  return normalized <= 0.03928
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance(r: number, g: number, b: number): number {
  return (
    0.2126 * relativeLuminanceChannel(r) +
    0.7152 * relativeLuminanceChannel(g) +
    0.0722 * relativeLuminanceChannel(b)
  );
}

function contrastRatio(l1: number, l2: number): number {
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

/**
 * Composite a foreground RGB at the given alpha over a background RGB.
 */
function composite(
  fg: { r: number; g: number; b: number },
  bg: { r: number; g: number; b: number },
  alpha: number,
): { r: number; g: number; b: number } {
  return {
    r: Math.round(alpha * fg.r + (1 - alpha) * bg.r),
    g: Math.round(alpha * fg.g + (1 - alpha) * bg.g),
    b: Math.round(alpha * fg.b + (1 - alpha) * bg.b),
  };
}

// Editorial text pair: --cream (#faf7f2) for dark backgrounds, --ink
// (#1a1a1a) for light ones. Replaces the upstream slate/zinc pair so chips
// read in the Teams editorial palette rather than cold grays.
const TEXT_CREAM = "#faf7f2"; // --cream
const TEXT_INK = "#1a1a1a"; // --ink

// Luminance of the editorial text pair, precomputed for the contrast picks.
const CREAM_LUM = relativeLuminance(250, 247, 242);
const INK_LUM = relativeLuminance(26, 26, 26);

/**
 * Pick a readable text color for a solid background.
 * Uses WCAG contrast ratios to choose between the cream and ink text colors.
 */
export function pickTextColorForSolidBg(hexColor: string): string {
  const rgb = hexToRgb(hexColor);
  if (!rgb) return TEXT_CREAM;
  const bgLum = relativeLuminance(rgb.r, rgb.g, rgb.b);
  return contrastRatio(bgLum, CREAM_LUM) >= contrastRatio(bgLum, INK_LUM)
    ? TEXT_CREAM
    : TEXT_INK;
}

/**
 * Pick a readable text color for a semi-transparent pill background.
 *
 * Composites `rgba(hexColor, alpha)` over the editorial cream page
 * background and then picks the editorial text color with the better
 * WCAG contrast ratio.
 */
export function pickTextColorForPillBg(hexColor: string, alpha = 0.22): string {
  const fg = hexToRgb(hexColor);
  if (!fg) return TEXT_INK;
  const effectiveBg = composite(fg, PAGE_BG, alpha);
  const bgLum = relativeLuminance(effectiveBg.r, effectiveBg.g, effectiveBg.b);
  return contrastRatio(bgLum, CREAM_LUM) >= contrastRatio(bgLum, INK_LUM)
    ? TEXT_CREAM
    : TEXT_INK;
}
