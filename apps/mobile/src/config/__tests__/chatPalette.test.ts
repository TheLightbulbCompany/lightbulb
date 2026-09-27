import { chatPalette } from "../theme";

function channel(byte: number): number {
  const c = byte / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}
function luminance(hex: string): number {
  const n = parseInt(hex.slice(1), 16);
  return 0.2126 * channel((n >> 16) & 0xff) + 0.7152 * channel((n >> 8) & 0xff) + 0.0722 * channel(n & 0xff);
}
function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

const THEMES = ["light", "dark"] as const;
const TEXT = ["fg", "fgSoft", "fgMute", "link", "alert", "live", "warn"] as const;
const PLANES = ["pane", "sidebar", "raised", "selected", "rail"] as const;

describe("chatPalette", () => {
  it("mirrors web [data-chat-theme] exactly", () => {
    expect(chatPalette.light).toMatchObject({
      rail: "#DEDEDE", sidebar: "#EFEFEF", pane: "#FFFFFF", raised: "#F7F7F7", edge: "#E2E2E2",
      selected: "#E4E4E6", hover: "#F2F2F3", fg: "#1A1A1C", fgSoft: "#52525A", fgMute: "#606069",
      link: "#1E52CC", onLink: "#FFFFFF", alert: "#A83C29", live: "#186D48", warn: "#52525A",
      agentTint: "#6E6E78", onAgentTint: "#FFFFFF",
      ground: "#EFEFEF", surface: "#FFFFFF", chip: "#E4E4E6", onFg: "#FFFFFF",
    });
    expect(chatPalette.dark).toMatchObject({
      rail: "#101012", sidebar: "#171719", pane: "#1E1E21", raised: "#27272B", edge: "#323236",
      selected: "#2E2E33", hover: "#26262A", fg: "#ECECEE", fgSoft: "#B4B4B8", fgMute: "#97979C",
      link: "#8FB0FF", onLink: "#0C121A", alert: "#E7806F", live: "#4BC08A", warn: "#B4B4B8",
      agentTint: "#9B9BA2", onAgentTint: "#101012",
      ground: "#101012", surface: "#1E1E21", chip: "#27272B", onFg: "#101012",
    });
  });

  it.each(THEMES)("%s clears AA for every text token on every plane", (scheme) => {
    const p = chatPalette[scheme];
    const failures = TEXT.flatMap((t) =>
      PLANES.filter((plane) => contrast(p[t], p[plane]) < 4.5).map((plane) => `${t} on ${plane}`),
    );
    expect(failures).toEqual([]);
  });

  // Mirrors the web test's coverage guard (chatThemeContrast.test.ts): a
  // silent narrowing of THEMES/TEXT/PLANES above would still pass the
  // contrast check above with fewer pairs checked, so pin the count too.
  it("checks every pair — a silent drop in coverage is the failure mode here", () => {
    expect(THEMES.length * TEXT.length * PLANES.length).toBe(70);
  });

  it.each(THEMES)("%s: text on a mine bubble and on an agent tile clears AA", (scheme) => {
    const p = chatPalette[scheme];
    expect(contrast(p.onFg, p.fg)).toBeGreaterThanOrEqual(4.5);
    expect(contrast(p.onAgentTint, p.agentTint)).toBeGreaterThanOrEqual(4.5);
  });
});
