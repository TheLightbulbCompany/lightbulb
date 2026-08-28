import { CanvasTexture, SRGBColorSpace } from "three";

/** The laptop's screen: the visitor's idea, set in Lora, cream on midnight.
 *  Redrawn only when the text changes — one 2D paint per keystroke. */
export function makeIdeaTexture(midnight: string, cream: string) {
  const W = 1024,
    H = 700,
    // The plane carrying this texture is a little wider than the bezel's
    // opening, so the first ~70px of it sit under the frame. At a 72 pad the
    // empty state's caret was ON that edge and never read; 110 puts every
    // mark inside the glass.
    PAD = 110,
    LINE = 125; // four lines: 110 + 4·125 = 610 ≤ 700
  const canvas = document.createElement("canvas");
  canvas.width = W;
  canvas.height = H;
  const ctx = canvas.getContext("2d")!;
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  texture.flipY = false; // her screen plane is rotated π about X

  function wrap(text: string, max: number): string[] {
    const words = text.split(/\s+/).filter(Boolean);
    const lines: string[] = [];
    let line = "";
    for (const w of words) {
      const t = line ? `${line} ${w}` : w;
      if (ctx.measureText(t).width > max && line) {
        lines.push(line);
        line = w;
      } else line = t;
    }
    if (line) lines.push(line);
    return lines.slice(-4); // the last four lines fit the screen
  }

  function draw(text: string) {
    ctx.fillStyle = midnight;
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = cream;
    // 100px: the screen is ~160px wide on a 1440 display, so this is ~15px there.
    ctx.font = "500 100px Lora, Georgia, serif";
    ctx.textBaseline = "top";
    const lines = text ? wrap(text, W - PAD * 2) : [];
    let y = PAD;
    for (const l of lines) {
      ctx.fillText(l, PAD, y);
      y += LINE;
    }
    // caret after the last line (or at the start when empty)
    const last = lines[lines.length - 1] ?? "";
    const cx = PAD + (last ? ctx.measureText(last).width + 8 : 0);
    const cy = lines.length ? y - LINE : PAD;
    ctx.fillRect(cx, cy + 8, 6, 104);
    texture.needsUpdate = true;
  }
  draw("");
  return { texture, draw };
}
