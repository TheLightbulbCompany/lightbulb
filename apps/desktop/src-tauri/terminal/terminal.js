// The session's terminal in the app's panel: frames to and from the holder over this page's own WebSocket.
// Frame: [u8 kind][u32 BE length][payload]. Kinds: 1 HELLO, 2 OUTPUT, 3 INPUT, 4 RESIZE, 5 EXIT, 8 SNAPSHOT,
// 9 DETACHED, 10 PROMOTED.
const HELLO = 1, OUTPUT = 2, INPUT = 3, RESIZE = 4, EXIT = 5, SNAPSHOT = 8, DETACHED = 9, PROMOTED = 10;
const THEME = { background: "#16161a", foreground: "#e6e6e9" };
const term = new Terminal({ macOptionIsMeta: true, scrollback: 5000, fontSize: 13,
  fontFamily: "Menlo, 'SF Mono', monospace", theme: THEME });
// A teammate typing (S8): the cursor in their colour, their name in the bar, for the `forMs` the
// app gives; the owner's own keys clear it at once. The colour is the web's `personColor` (FNV-1a
// over the user id, the same order), with globals.css's dark --person-* values: keep them in step.
const PERSON = ["#67D4E4", "#E39AF0", "#7FD69A", "#E3C35A", "#93B4FA", "#B3A8FA"];
const personColor = (id) => { let h = 0x811c9dc5; for (let i = 0; i < id.length; i++) h = Math.imul(h ^ id.charCodeAt(i), 0x01000193); return PERSON[(h >>> 0) % PERSON.length]; };
const who = document.getElementById("who");
let typistTimer = 0;
function typist(t) {
  clearTimeout(typistTimer);
  const color = t ? personColor(t.userId) : undefined;
  term.options.theme = color ? { ...THEME, cursor: color } : THEME;
  who.textContent = t ? `${t.name} typing` : ""; who.style.color = color || "";
  if (t) typistTimer = setTimeout(() => typist(null), t.forMs);
}
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
term.open(document.getElementById("t"));
const note = (text) => { const n = document.getElementById("note"); n.textContent = text; n.style.display = "block"; };
const key = new URLSearchParams(location.search).get("key");
const ws = new WebSocket(`ws://${location.host}/ws?key=${encodeURIComponent(key)}`);
ws.binaryType = "arraybuffer";
const enc = new TextEncoder();
function send(kind, payload = new Uint8Array()) {
  if (ws.readyState !== WebSocket.OPEN) return;
  if (kind === INPUT) typist(null); // the owner's keys: the plain cursor at once
  const frame = new Uint8Array(5 + payload.length);
  frame[0] = kind; new DataView(frame.buffer).setUint32(1, payload.length); frame.set(payload, 5);
  ws.send(frame);
}
// Joined: a terminal elsewhere (`lightbulb claude`) owns the session's size. This page draws at
// that size, types too, and never resizes the session.
let joined = false;
const box = document.getElementById("t");
const wrap = document.getElementById("wrap");
// How a joined grid (sw x sh) sits in the panel (w x h): scale k, shift dy. It fits the WIDTH, so
// text is the same size in every session. A grid a little too tall shrinks to fit (down to 85%);
// a much taller one keeps its size and its last rows, where the prompt is, and loses the top.
function fitScale(w, h, sw, sh) {
  if (!(w > 0 && h > 0 && sw > 0 && sh > 0)) return { k: 1, dy: 0 };
  const wide = Math.min(1, w / sw);
  const tall = h / sh;
  const k = tall >= wide * 0.85 ? Math.min(wide, tall) : wide;
  return { k, dy: Math.min(0, h - sh * k) };
}
function scale() {
  const screen = box.querySelector(".xterm-screen");
  if (!screen) return;
  if (!joined) { box.style.transform = ""; return; }
  const { k, dy } = fitScale(wrap.clientWidth, wrap.clientHeight, screen.offsetWidth, screen.offsetHeight);
  box.style.transformOrigin = "top left";
  box.style.transform = `translateY(${dy}px) scale(${k})`;
}
function fixed(cols, rows) {
  term.resize(Math.max(1, cols), Math.max(1, rows));
  scale();
  // xterm lays the new grid out on its next frame: measure again once it has.
  requestAnimationFrame(scale);
}
function resize() {
  if (joined) { scale(); return; }
  fit.fit();
  const p = new Uint8Array(4); const v = new DataView(p.buffer);
  v.setUint16(0, term.cols); v.setUint16(2, term.rows);
  send(RESIZE, p);
}
// Shift+Enter inserts a newline in Claude Code only as ESC CR (what Option+Enter sends);
// xterm.js would send a plain CR, which submits the prompt.
term.attachCustomKeyEventHandler((e) => {
  if (e.type === "keydown" && e.key === "Enter" && e.shiftKey && !e.metaKey && !e.ctrlKey && !e.altKey) {
    send(INPUT, enc.encode("\x1b\r"));
    return false;
  }
  return true;
});
term.onData((d) => send(INPUT, enc.encode(d)));
term.onBinary((d) => send(INPUT, Uint8Array.from(d, (c) => c.charCodeAt(0))));
ws.onopen = () => { resize(); };
const end = document.getElementById("end");
// A Claude background session shown through `claude attach`: Lightbulb never stops it, so no End here.
// Closing the panel closes this terminal; the session keeps running.
const attach = new URLSearchParams(location.search).get("attach") === "1";
if (attach) end.hidden = true;
let pending = new Uint8Array();
ws.onmessage = (event) => {
  if (typeof event.data === "string") {
    let said = {};
    try { said = JSON.parse(event.data); } catch { /* not ours to fix */ }
    typist(said.typist && typeof said.typist.userId === "string" ? said.typist : null);
    return;
  }
  const data = new Uint8Array(event.data);
  const buffered = new Uint8Array(pending.length + data.length); buffered.set(pending); buffered.set(data, pending.length);
  pending = buffered;
  while (pending.length >= 5) {
    const length = new DataView(pending.buffer, pending.byteOffset).getUint32(1);
    if (pending.length < 5 + length) break;
    const kind = pending[0], payload = pending.slice(5, 5 + length);
    pending = pending.slice(5 + length);
    if (kind === HELLO) {
      let facts = {};
      try { facts = JSON.parse(new TextDecoder().decode(payload)); } catch { /* not ours to fix */ }
      if (facts.joined === true) { joined = true; fixed(facts.cols, facts.rows); }
    } else if (kind === RESIZE && joined && payload.length === 4) {
      const v = new DataView(payload.buffer, payload.byteOffset);
      fixed(v.getUint16(0), v.getUint16(2));
    } else if (kind === PROMOTED) { joined = false; resize(); }
    else if (kind === OUTPUT || kind === SNAPSHOT) term.write(payload.slice(8));
    else if (kind === DETACHED) note("This session is open in another terminal.");
    else if (kind === EXIT) { note(attach ? "This terminal closed." : "This session has ended."); end.hidden = true; }
  }
};
ws.onclose = () => { if (document.getElementById("note").style.display !== "block") note("Disconnected from this session. Open it again from Sessions."); };
addEventListener("resize", resize);
// End, like closing a terminal: two clicks, so a stray one never ends a session.
let armed = 0;
end.addEventListener("click", () => {
  if (Date.now() - armed < 3000) {
    if (ws.readyState === WebSocket.OPEN) ws.send("end");
    end.disabled = true; end.textContent = "Ending…";
    return;
  }
  armed = Date.now(); end.textContent = "Click again to end";
  setTimeout(() => { if (!end.disabled) end.textContent = "End session"; }, 3000);
  term.focus();
});
