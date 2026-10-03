// The session's terminal in the app's panel: frames to and from the holder over this page's own WebSocket.
// Frame: [u8 kind][u32 BE length][payload]. Kinds: 2 OUTPUT, 3 INPUT, 4 RESIZE, 5 EXIT, 8 SNAPSHOT, 9 DETACHED.
const OUTPUT = 2, INPUT = 3, RESIZE = 4, EXIT = 5, SNAPSHOT = 8, DETACHED = 9;
const term = new Terminal({ macOptionIsMeta: true, scrollback: 5000, fontSize: 13,
  fontFamily: "Menlo, 'SF Mono', monospace", theme: { background: "#16161a", foreground: "#e6e6e9" } });
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
  const frame = new Uint8Array(5 + payload.length);
  frame[0] = kind; new DataView(frame.buffer).setUint32(1, payload.length); frame.set(payload, 5);
  ws.send(frame);
}
function resize() {
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
ws.onopen = () => { resize(); term.focus(); };
const end = document.getElementById("end");
let pending = new Uint8Array();
ws.onmessage = (event) => {
  const data = new Uint8Array(event.data);
  const joined = new Uint8Array(pending.length + data.length); joined.set(pending); joined.set(data, pending.length);
  pending = joined;
  while (pending.length >= 5) {
    const length = new DataView(pending.buffer, pending.byteOffset).getUint32(1);
    if (pending.length < 5 + length) break;
    const kind = pending[0], payload = pending.slice(5, 5 + length);
    pending = pending.slice(5 + length);
    if (kind === OUTPUT || kind === SNAPSHOT) term.write(payload.slice(8));
    else if (kind === DETACHED) note("This session is open in another terminal.");
    else if (kind === EXIT) { note("This session has ended."); end.hidden = true; }
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
