/** xterm answers the program's own queries (device attributes, cursor position, colours, focus, mode reports)
 *  through `onData`. Those are the viewer's terminal talking, not the viewer: they never go to the Mac's session. */
const PROBE_REPLY = new RegExp([
  "\\x1b\\[[?>=]?[0-9;]*c",        // DA1 / DA2
  "\\x1b\\[\\??[0-9;]*R",          // CPR
  "\\x1b\\[\\??[0-9;]*n",          // DSR
  "\\x1b\\[\\??[0-9;]*\\$y",       // DECRQM
  "\\x1b\\[[IO]",                  // focus in / out
  "\\x1b\\](?:4;[0-9]+|1[0-9]);[^\\x07\\x1b]*(?:\\x07|\\x1b\\\\)", // OSC colour replies
  "\\x1bP[^\\x1b]*\\x1b\\\\",      // DCS replies (XTGETTCAP, DECRQSS)
].join("|"), "g");

export const dropProbeReplies = (data: string): string => data.replace(PROBE_REPLY, "");

/** With mouse tracking on, xterm writes every click, drag and wheel through `onData` as a report.
 *  A remote pointer in the owner's program is not typing: no report ever goes to the Mac. No key
 *  produces these (SGR `CSI < b;x;y M|m`, X10 `CSI M` + 3 characters, urxvt `CSI b;x;y M`). */
const MOUSE_REPORT = /\x1b\[(?:<\d+;\d+;\d+[Mm]|M[\s\S]{3}|\d+;\d+;\d+M)/g;
export const dropMouseReports = (data: string): string => data.replace(MOUSE_REPORT, "");

export const MAX_INPUT_BYTES = 4096;
/** Shift+Enter is a newline in Claude Code only as ESC CR; xterm would send a plain CR, which submits. */
export const SHIFT_ENTER = "\x1b\r";

/** Split encoded bytes into calls of at most MAX_INPUT_BYTES, in order. */
export function chunkInput(bytes: Uint8Array): Uint8Array[] {
  const out: Uint8Array[] = [];
  for (let i = 0; i < bytes.length; i += MAX_INPUT_BYTES) out.push(bytes.subarray(i, i + MAX_INPUT_BYTES));
  return out;
}

export const toBase64 = (bytes: Uint8Array): string => {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
};
