import { TERMINAL_HTML, XTERM_VERSION } from "../terminalPage.generated";

// The app's tsconfig has no Node types: the two Node modules this reads files with are typed here.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { readFileSync } = require("fs") as { readFileSync: (path: string, encoding: "utf8") => string };
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { join, dirname } = require("path") as { join: (...parts: string[]) => string; dirname: (path: string) => string };
const here = dirname(expect.getState().testPath!);
const scripts = join(here, "../../../../scripts");

describe("the generated terminal page", () => {
  it("matches what the generator builds from the installed xterm (run scripts/build-terminal-page.js)", () => {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const { build } = require(join(scripts, "build-terminal-page.js")) as { build: () => string };
    const installed = JSON.parse(readFileSync(join(scripts, "../../frontend/node_modules/@xterm/xterm/package.json"), "utf8")) as { version: string };
    expect(XTERM_VERSION).toBe(installed.version);
    expect(build()).toBe(readFileSync(join(here, "../terminalPage.generated.ts"), "utf8"));
  });

  it("is one whole page that can load nothing from the network", () => {
    expect(TERMINAL_HTML).toContain("default-src 'none'");
    expect(TERMINAL_HTML).not.toMatch(/<script[^>]+src=|<link[^>]+href=|@import/);
    expect(TERMINAL_HTML).toContain("window.lb = function");
  });
});

/** terminal-page.js against a stand-in for xterm that parses writes later, in order, as xterm does. */
function page() {
  const log: string[] = [];
  const posted: { t: string; d?: string }[] = [];
  const queue: { data: string; done?: () => void }[] = [];
  let onData: (d: string) => void = () => {};
  const listeners: Record<string, () => void> = {};
  const textarea = { attrs: {} as Record<string, string>, setAttribute(name: string, value: string) { this.attrs[name] = value; },
    addEventListener(type: string, handler: () => void) { listeners[`textarea:${type}`] = handler; } };
  const term = {
    cols: 80, rows: 24, options: { disableStdin: false }, textarea,
    buffer: { active: { viewportY: 0, baseY: 0 } },
    modes: { applicationCursorKeysMode: false },
    open: () => {},
    write(data: string | Uint8Array, done?: () => void) { queue.push({ data: typeof data === "string" ? data : String.fromCharCode(...data), done }); },
    reset() { log.push("reset"); this.buffer.active = { viewportY: 0, baseY: 0 }; },
    resize(cols: number, rows: number) { this.cols = cols; this.rows = rows; log.push(`resize ${cols}x${rows}`); },
    scrollToLine(line: number) { log.push(`scrollToLine ${line}`); this.buffer.active.viewportY = line; },
    scrollLines: (n: number) => log.push(`scrollLines ${n}`),
    onData(handler: (d: string) => void) { onData = handler; },
    input(d: string) { if (!this.options.disableStdin) onData(d); },
    paste(d: string) { if (!this.options.disableStdin) onData(d); },
    focus: () => log.push("focus"), blur: () => log.push("blur"),
  };
  const element = (id: string) => ({ style: {} as Record<string, string>, addEventListener: (type: string, handler: () => void) => { listeners[`${id}:${type}`] = handler; }, querySelector: () => null, clientWidth: 0, clientHeight: 0, scrollTop: 0, scrollHeight: 0 });
  const window = {
    Terminal: function Terminal() { return term; },
    ReactNativeWebView: { postMessage: (m: string) => posted.push(JSON.parse(m)) },
    addEventListener: () => {},
    lb: (_message: unknown) => {},
  };
  const document = { getElementById: element, activeElement: null as unknown };
  new Function("window", "document", "atob", "requestAnimationFrame", "cancelAnimationFrame", readFileSync(join(scripts, "terminal-page.js"), "utf8"))(window, document, atob, () => 0, () => {});
  /** xterm's turn: parse what is queued, in order. */
  const parse = () => { for (let w = queue.shift(); w; w = queue.shift()) { if (w.data) log.push(`write ${w.data}`); w.done?.(); } };
  log.length = 0; // starting read-only blurs once
  return { window, term, document, log, posted, parse, listeners };
}
const b64 = (text: string) => btoa(text);
const step = (data: string[], more: object = {}) => ({ fresh: false, again: false, resize: false, cols: 80, rows: 24, back: false, data: data.map(b64), ...more });

describe("the terminal page's script", () => {
  it("says when it is ready and starts read-only, with no keyboard", () => {
    const { posted, term } = page();
    expect(posted).toEqual([{ t: "ready" }]);
    expect(term.options.disableStdin).toBe(true);
    expect(term.textarea.attrs).toMatchObject({ inputmode: "none", autocorrect: "off", autocapitalize: "off", spellcheck: "false" });
  });

  it("resets behind the writes queued before it, never ahead of them", () => {
    const { window, log, parse } = page();
    window.lb({ t: "draw", steps: [step(["late history"]), step(["history", "snapshot"], { fresh: true, resize: true, cols: 120, rows: 40 })] });
    // Nothing is parsed yet, and a reset now would leave "late history" to draw into the new picture.
    expect(log).toEqual([]);
    parse();
    expect(log).toEqual(["write late history", "reset", "resize 120x40", "write history", "write snapshot"]);
  });

  it("puts a reader who had scrolled up back on their line after the picture starts over", () => {
    const { window, term, log, parse } = page();
    term.buffer.active = { viewportY: 12, baseY: 40 };
    window.lb({ t: "draw", steps: [step(["history"], { fresh: true }), step(["snapshot"], { back: true })] });
    parse();
    expect(log).toEqual(["reset", "write history", "write snapshot", "scrollToLine 12"]);
  });

  it("leaves a reader at the tail alone", () => {
    const { window, log, parse } = page();
    window.lb({ t: "draw", steps: [step(["snapshot"], { fresh: true, back: true })] });
    parse();
    expect(log).toEqual(["reset", "write snapshot"]);
  });

  it("hands back keys only once typing is allowed, arrows in the mode the program asked for", () => {
    const { window, term, posted } = page();
    window.lb({ t: "key", key: "esc" });
    expect(posted).toHaveLength(1);
    window.lb({ t: "canType", can: true });
    expect(term.textarea.attrs.inputmode).toBe("text");
    window.lb({ t: "key", key: "esc" });
    window.lb({ t: "key", key: "tab" });
    window.lb({ t: "key", key: "up" });
    term.modes.applicationCursorKeysMode = true;
    window.lb({ t: "key", key: "left" });
    window.lb({ t: "paste", d: "pasted" });
    expect(posted.slice(1).map((m) => m.d)).toEqual(["\x1b", "\t", "\x1b[A", "\x1bOD", "pasted"]);
  });

  it("focuses on a tap anywhere in the pane for a person who may type, and sends nothing", () => {
    const { window, log, posted, listeners } = page();
    window.lb({ t: "canType", can: true });
    listeners["box:click"]();
    expect(log).toEqual(["focus"]);
    expect(posted).toEqual([{ t: "ready" }]);
  });

  it("stays unfocused for a viewer: a tap in the pane does nothing, and a tap on the grid lets go at once", () => {
    const { log, listeners } = page();
    listeners["box:click"]();
    expect(log).toEqual([]);
    // xterm focused its textarea on the tap.
    listeners["textarea:focus"]();
    expect(log).toEqual(["blur"]);
  });

  it("shows and hides the keyboard", () => {
    const { window, term, document, log } = page();
    window.lb({ t: "keyboard" });
    document.activeElement = term.textarea;
    window.lb({ t: "keyboard" });
    expect(log).toEqual(["focus", "blur"]);
  });
});
