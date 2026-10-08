/** The colours of the session link's social card (app/s/opengraph-image.tsx). An image has no
 *  stylesheet, so it cannot read the tokens: these are the same values as --terminal-bg,
 *  --terminal-fg, the dark --fg-mute, --plane-edge and --live, and the brand's --bulb, in globals.css. */
export const SHARE_CARD = {
  bg: "#16161a",
  fg: "#e6e6e9",
  mute: "#97979C",
  edge: "#323236",
  live: "#4BC08A",
  bulb: "#F0C34E",
} as const;
