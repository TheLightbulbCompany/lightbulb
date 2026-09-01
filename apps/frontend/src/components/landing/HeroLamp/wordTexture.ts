import { CanvasTexture, SRGBColorSpace } from "three";

const FONT_PX = 220;
/** The glow spills well past the glyphs; the box has to hold it. */
const PAD = 80;
/** Wide halo, tight halo, then the tube itself: [blur, lineWidth, alpha].
 *  Three passes are what reads as neon rather than a blurred word. */
const PASSES: [number, number, number][] = [
  [70, 7, 0.3],
  [24, 4, 0.45],
  [0, 2.5, 1],
];

/**
 * The footer's hollow cursive wordmark, redrawn as neon tubing — same
 * "no asset to fetch" idiom as makeGlowTexture. The face is the page's own
 * Lora italic, read off --font-lora-serif, so the hero word and the footer
 * word are the same letterforms.
 */
export function makeWordTexture(
  text: string,
  color: string,
): { texture: CanvasTexture; aspect: number; inkAspect: number } {
  const family =
    getComputedStyle(document.documentElement).getPropertyValue("--font-lora-serif").trim() || "Georgia, serif";
  const font = `italic 500 ${FONT_PX}px ${family}`;
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d")!;
  ctx.font = font;
  const m = ctx.measureText(text);
  canvas.width = Math.ceil(m.actualBoundingBoxLeft + m.actualBoundingBoxRight) + PAD * 2;
  canvas.height = Math.ceil(m.actualBoundingBoxAscent + m.actualBoundingBoxDescent) + PAD * 2;
  // Sizing the canvas resets every context value set above it.
  ctx.font = font;
  ctx.strokeStyle = color;
  ctx.shadowColor = color;
  ctx.lineJoin = "round";
  const x = PAD + m.actualBoundingBoxLeft;
  const y = PAD + m.actualBoundingBoxAscent;
  for (const [blur, lineWidth, alpha] of PASSES) {
    ctx.shadowBlur = blur;
    ctx.lineWidth = lineWidth;
    ctx.globalAlpha = alpha;
    ctx.strokeText(text, x, y);
  }
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  // `aspect` sizes the plane; `inkAspect` is the glyphs alone against the same
  // width, so the caller can budget the height the LETTERS take rather than the
  // height of the box the glow needs around them.
  return {
    texture,
    aspect: canvas.height / canvas.width,
    inkAspect: (m.actualBoundingBoxAscent + m.actualBoundingBoxDescent) / canvas.width,
  };
}
