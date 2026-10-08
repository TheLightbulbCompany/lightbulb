/* The phone terminal's page: xterm and a message handler, nothing else. It runs inside the session
 * screen's WebView (build-terminal-page.js inlines it, with xterm, into terminalPage.generated.ts).
 * React Native decides what is written and when (src/lib/sessionTerminal.ts, over the shared
 * receiveScreen); this page writes it, scales the grid, scrolls, and hands every key back.
 * It never talks to the network (the page's CSP allows none) and never resizes the Mac. */
(function () {
  "use strict";
  const post = function (message) { window.ReactNativeWebView.postMessage(JSON.stringify(message)); };
  const box = document.getElementById("box");
  const wrap = document.getElementById("wrap");
  const host = document.getElementById("host");
  /** Readable never draws below 13 px x 0.75; the box scrolls sideways instead (the web's floor). */
  const MIN_SCALE = 0.75;
  const term = new window.Terminal({ disableStdin: true, cursorBlink: false, scrollback: 5000, fontSize: 13,
    fontFamily: "Menlo, 'SF Mono', monospace", theme: { background: "#16161a", foreground: "#e6e6e9" } });
  term.open(host);
  let mode = "fit";
  let following = true; // the prompt is on the last row: stay there until the reader scrolls away
  // The line, from the top, a reader who had scrolled up was on when the picture started over; -1: at the tail.
  let readingAt = -1;
  const decode = function (data) { return Uint8Array.from(atob(data), function (c) { return c.charCodeAt(0); }); };
  const atBottom = function () { return box.scrollTop + box.clientHeight >= box.scrollHeight - 1; };

  const fit = function () {
    const shown = host.querySelector(".xterm-screen");
    if (!shown || !shown.offsetWidth || !shown.offsetHeight || !box.clientWidth) return;
    const fill = box.clientWidth / shown.offsetWidth;
    // Fit: the whole width, however small (pinch to read). Readable: legible text, scroll sideways.
    const scale = mode === "fit" ? Math.min(1, fill) : Math.min(1, Math.max(MIN_SCALE, fill));
    // The wrapper has the scaled size: a transform leaves the layout size alone.
    wrap.style.width = Math.floor(shown.offsetWidth * scale) + "px";
    wrap.style.height = Math.floor(shown.offsetHeight * scale) + "px";
    host.style.width = shown.offsetWidth + "px";
    host.style.height = shown.offsetHeight + "px";
    host.style.transform = "scale(" + scale + ")";
    if (following) box.scrollTop = box.scrollHeight;
  };

  /** One instruction from React Native: `data` is base64 parts written in order. xterm parses writes
   *  later, in order, but resets and resizes at once, and a reset leaves what is queued in place
   *  (WriteBuffer, 5.5.0): so both wait their turn behind an empty write. */
  const draw = function (step) {
    if (step.fresh || step.resize) {
      term.write("", function () {
        if (step.fresh) {
          // A reset goes to the tail. Someone reading further up stays on their line, unless they
          // still wait at the tail of this run's last redraw for it.
          const at = term.buffer.active;
          if (!(step.again && readingAt >= 0 && at.viewportY >= at.baseY)) readingAt = at.viewportY < at.baseY ? at.viewportY : -1;
          term.reset();
        }
        if (term.cols !== step.cols || term.rows !== step.rows) term.resize(step.cols, step.rows);
      });
    }
    step.data.forEach(function (part, i) {
      const last = i === step.data.length - 1;
      term.write(decode(part), !last ? undefined : !step.back ? fit : function () {
        if (readingAt >= 0) {
          term.scrollToLine(readingAt);
          // On their line. Still at the tail: the picture is shorter than their line so far.
          if (term.buffer.active.viewportY < term.buffer.active.baseY) readingAt = -1;
        }
        fit();
      });
    });
  };

  const setCanType = function (can) {
    term.options.disableStdin = !can;
    // No keyboard for a viewer: a tap on the picture focuses xterm's textarea all the same.
    if (term.textarea) term.textarea.setAttribute("inputmode", can ? "text" : "none");
    if (!can) term.blur();
  };
  if (term.textarea) {
    ["autocorrect", "autocapitalize", "autocomplete"].forEach(function (name) { term.textarea.setAttribute(name, "off"); });
    term.textarea.setAttribute("spellcheck", "false");
  }
  setCanType(false);

  // Every key, and what the key bar and Paste send through `input` and `paste`, leaves here. Mouse
  // reports and xterm's own probe replies are dropped on the other side (shared sessionInput).
  term.onData(function (d) { post({ t: "data", d: d }); });

  const KEYS = { esc: "\x1b", tab: "\t" };
  const ARROWS = { up: "A", down: "B", right: "C", left: "D" };
  const key = function (name) {
    if (ARROWS[name]) term.input((term.modes.applicationCursorKeysMode ? "\x1bO" : "\x1b[") + ARROWS[name], true);
    else if (KEYS[name]) term.input(KEYS[name], true);
  };

  window.lb = function (message) {
    if (message.t === "draw") message.steps.forEach(draw);
    else if (message.t === "mode") { mode = message.mode; fit(); }
    else if (message.t === "canType") setCanType(message.can);
    else if (message.t === "key") key(message.key);
    else if (message.t === "paste") term.paste(message.d);
    else if (message.t === "keyboard") { if (document.activeElement === term.textarea) term.blur(); else term.focus(); }
  };

  // One finger moves the box while it can, then the terminal's own scrollback, both axes by hand:
  // the session's program turns mouse tracking on and xterm then drops touch scrolling. Stopped in
  // capture, above xterm, so nothing here ever reaches the Mac. Two fingers, or a page already
  // zoomed, are the browser's: pinch and pan.
  let touch = null; // { x, y, carry, v, at }
  let glide = 0;
  const scrollRows = function (dy) {
    const shown = host.querySelector(".xterm-screen");
    const rowPx = shown && shown.getBoundingClientRect().height / term.rows;
    if (!rowPx) return;
    if (dy < 0 ? box.scrollTop > 0 : !atBottom()) { box.scrollTop += dy; following = atBottom(); return; }
    touch.carry += dy / rowPx;
    const lines = touch.carry < 0 ? Math.ceil(touch.carry) : Math.floor(touch.carry);
    touch.carry -= lines;
    if (lines) term.scrollLines(lines);
  };
  const zoomed = function () { return !!window.visualViewport && window.visualViewport.scale > 1.01; };
  box.addEventListener("touchstart", function (e) {
    cancelAnimationFrame(glide);
    touch = e.touches.length === 1 && !zoomed() ? { x: e.touches[0].clientX, y: e.touches[0].clientY, carry: 0, v: 0, at: e.timeStamp } : null;
  }, { capture: true, passive: true });
  box.addEventListener("touchmove", function (e) {
    if (!touch || e.touches.length !== 1) { touch = null; return; }
    e.preventDefault(); e.stopPropagation();
    const dx = touch.x - e.touches[0].clientX;
    const dy = touch.y - e.touches[0].clientY;
    const dt = Math.max(1, e.timeStamp - touch.at);
    touch.v = 0.8 * (dy / dt) + 0.2 * touch.v;
    touch.x = e.touches[0].clientX; touch.y = e.touches[0].clientY; touch.at = e.timeStamp;
    box.scrollLeft += dx;
    scrollRows(dy);
  }, { capture: true, passive: false });
  box.addEventListener("touchend", function () {
    if (!touch) return;
    // The finger's speed carries on and dies away, as a native list does.
    const step = function () {
      if (!touch || Math.abs(touch.v) < 0.05) return;
      scrollRows(touch.v * 16);
      touch.v *= 0.95;
      glide = requestAnimationFrame(step);
    };
    glide = requestAnimationFrame(step);
  }, { capture: true, passive: true });

  window.addEventListener("resize", fit);
  post({ t: "ready" });
})();
