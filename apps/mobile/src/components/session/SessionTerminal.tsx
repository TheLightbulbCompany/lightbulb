import { ApiError, type LiveEvent, type LocalSession } from "@lightbulb/shared";
import * as Clipboard from "expo-clipboard";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { SymbolView, type SFSymbol } from "expo-symbols";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { WebView, type WebViewMessageEvent } from "react-native-webview";

import { fonts, spacing } from "../../config/theme";
import { LocalStore } from "../../lib/localStore";
import { createScreenFeed, createTypist, STALE_AFTER_MS, WATCH_EVERY_MS, type TerminalStep } from "../../lib/sessionTerminal";
import { TERMINAL_HTML } from "./terminalPage.generated";

/** The terminal is always dark, in both themes: the web's --terminal-bg / --terminal-fg. */
const BG = "#16161a";
const FG = "#e6e6e9";
const FG_MUTE = "#9a9aa3";
const EDGE = "#2c2c33";
/** The size switch and the status line are chrome in a fixed frame: capped. */
const MAX_SCALE = 1.3;
/** The key bar is a keyboard row: nine keys share the width on every phone, so its labels do not grow
 *  with the text size (the system keyboard's own do not either). */
const KEY_SCALE = 1;
/** Fit or Readable, remembered on this phone as the web remembers it in the browser. */
const sizeStore = new LocalStore<{ size?: TerminalSize }>("session-terminal");

export type TerminalSize = "fit" | "readable";
type PageMessage =
  | { t: "draw"; steps: TerminalStep[] } | { t: "mode"; mode: TerminalSize } | { t: "canType"; can: boolean }
  | { t: "key"; key: string } | { t: "paste"; d: string } | { t: "keyboard" };
const KEYS: { key: string; label: string; name: string }[] = [
  { key: "esc", label: "esc", name: "Escape" }, { key: "tab", label: "tab", name: "Tab" },
  { key: "left", label: "←", name: "Left arrow" }, { key: "down", label: "↓", name: "Down arrow" },
  { key: "up", label: "↑", name: "Up arrow" }, { key: "right", label: "→", name: "Right arrow" },
];

/**
 * A session's real terminal on the phone: xterm in a WebView, fed the same frames the web draws
 * (spec 2026-10-07 §7). This side orders the frames (`createScreenFeed`, over the shared
 * `receiveScreen`) and the page writes them. Read-only unless `input` is given and the person is
 * `admitted`; then the iOS keyboard types straight into the terminal, with a bar for the keys a
 * phone lacks. The viewer never resizes the Mac: the grid is the Mac's own cols x rows, scaled.
 * It watches only while `active` (the screen is focused and the app is not in the background).
 */
export function SessionTerminal({ session, onEvent, watch, onLive, onCanType, input, admitted = false, kept = false, active, bottomInset = 0 }: {
  session: Pick<LocalSession, "sessionId" | "generation">;
  /** The session ended and its Mac reports the final screen: asked for once, as a new viewer does. */
  kept?: boolean;
  input?: (data: string, epoch: number) => Promise<unknown>;
  admitted?: boolean;
  active: boolean;
  /** The home indicator's inset, when nothing is drawn under this view. */
  bottomInset?: number;
  onEvent: (handler: (event: LiveEvent) => void) => () => void;
  watch: (sessionId: string, fresh?: boolean) => Promise<unknown>;
  onLive: (live: boolean) => void;
  onCanType?: (canType: boolean) => void;
}) {
  const web = useRef<WebView>(null);
  const [live, setLiveState] = useState(false);
  const [seen, setSeen] = useState(false);
  const [silent, setSilent] = useState(false);
  const [size, setSizeState] = useState<TerminalSize>(() => (sizeStore.read().size === "readable" ? "readable" : "fit"));
  const setSize = (next: TerminalSize) => { setSizeState(next); sizeStore.write({ size: next }); };
  const [ctrl, setCtrl] = useState(false);
  const [refusedFor, setRefusedFor] = useState<string | null>(null);
  // Counts the page's lives: iOS can end a WebView's process, and the page that replaces it is empty.
  const [run, setRun] = useState(0);
  const key = `${session.sessionId}:${session.generation}`;
  // Read-only until this page has a picture: before the first snapshot the feed has no screen epoch
  // (-1, which the input route refuses), so keys typed during "Connecting…" would be lost.
  const canType = !!input && admitted && refusedFor !== key && seen;
  useEffect(() => { onCanType?.(canType); }, [canType, onCanType]);
  const setLive = useCallback((next: boolean) => { setLiveState(next); onLive(next); }, [onLive]);

  // Nothing reaches the page before it says it is ready; what came first waits here, in order.
  const page = useRef<{ ready: boolean; queued: PageMessage[] }>({ ready: false, queued: [] });
  const tell = useCallback((message: PageMessage) => {
    if (!page.current.ready) { page.current.queued.push(message); return; }
    web.current?.injectJavaScript(`window.lb(${JSON.stringify(message)});true;`);
  }, []);
  // Again for every new page (`run`): it starts read-only, in Fit.
  useEffect(() => { tell({ t: "canType", can: canType }); }, [canType, run, tell]);
  useEffect(() => { tell({ t: "mode", mode: size }); }, [size, run, tell]);

  // The newest `watch` and whether the screen is in front, for the feed's repair ask.
  const watching = useRef({ watch, active });
  watching.current = { watch, active };
  const feed = useMemo(() => createScreenFeed({ draw: (steps) => tell({ t: "draw", steps }), onLive: setLive, onSeen: () => setSeen(true),
    // Not while away: nothing is watched then, and the return asks fresh anyway. The watch effect shows a refusal.
    onLost: () => { if (watching.current.active) void watching.current.watch(session.sessionId, true).catch(() => {}); } }),
    // A restarted session (a new generation) is a new picture.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [key, run, tell, setLive]);
  useEffect(() => {
    const off = onEvent((event) => {
      if (event.type !== "workspace.session.screen") return;
      const frame = event.payload;
      if (frame.sessionId === session.sessionId && frame.generation === session.generation) feed.receive(frame);
    });
    return () => { off(); feed.dispose(); onLive(false); };
  }, [feed, onEvent, onLive, session.sessionId, session.generation]);

  // Watching: a fresh ask on open, a renewal every 15 s, nothing while the screen is away (the lease
  // just lapses), and a fresh ask on return. A fresh ask costs every viewer a history burst, so a
  // re-render that only changes `watch` renews instead.
  const screenKey = `${session.sessionId}:${kept}`;
  const asked = useRef("");
  const refused = useRef("");
  // The backend refused a new viewer: this session is full (LOCAL_SESSION_VIEWERS_FULL), with its message.
  const [full, setFull] = useState<string | null>(null);
  useEffect(() => {
    // Away: a seat may free meanwhile, so a full session is asked again on return. No access stays refused.
    if (!active) { asked.current = ""; if (full !== null) { refused.current = ""; setFull(null); } return; }
    if (refused.current === screenKey) return;
    const ping = (fresh: boolean) => void watch(session.sessionId, fresh).catch((e) => {
      if (!(e instanceof ApiError)) return;
      // The session already has its most live viewers: say so, and stop asking (no retry loop).
      const isFull = e.status === 409 && (e.body as { code?: string } | null)?.code === "LOCAL_SESSION_VIEWERS_FULL";
      if (isFull || e.status === 403 || e.status === 404) { refused.current = screenKey; setLive(false); }
      if (isFull) setFull(e.message);
    });
    ping(asked.current !== screenKey);
    asked.current = screenKey;
    const timer = setInterval(() => { if (refused.current !== screenKey) ping(false); }, WATCH_EVERY_MS);
    return () => clearInterval(timer);
    // `full` is read only on the way out (away), never a reason to ask again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, run, session.sessionId, screenKey, watch, setLive]);
  // No frame for 30 s of watching: a Mac whose app predates terminal frames never sends one.
  useEffect(() => {
    if (seen || !active) { setSilent(false); return; }
    const timer = setTimeout(() => setSilent(true), STALE_AFTER_MS);
    return () => clearTimeout(timer);
  }, [seen, active, key]);

  const typing = useRef({ canType, input });
  typing.current = { canType, input };
  const typist = useMemo(() => createTypist({
    send: (data, epoch) => typing.current.input?.(data, epoch) ?? Promise.resolve(),
    epoch: feed.epoch, canType: () => typing.current.canType, onRefused: () => setRefusedFor(key),
  }), [feed, key]);
  // The bar's Ctrl is the one truth: a typist made anew (the page restarted) holds what the bar shows.
  useEffect(() => { typist.setCtrl(ctrl); }, [typist, ctrl]);
  const onMessage = useCallback((event: WebViewMessageEvent) => {
    let message: { t?: string; d?: string };
    try { message = JSON.parse(event.nativeEvent.data); } catch { return; }
    if (message.t === "ready") {
      page.current.ready = true;
      page.current.queued.splice(0).forEach(tell);
    } else if (message.t === "data" && typeof message.d === "string") {
      if (typist.type(message.d)) setCtrl(false);
    }
  }, [tell, typist]);
  // A new page starts empty: a new feed, and a fresh ask for the picture.
  const reloaded = useCallback(() => { page.current = { ready: false, queued: [] }; asked.current = ""; setSeen(false); setLive(false); setRun((n) => n + 1); }, [setLive]);
  const paste = useCallback(() => void Clipboard.getStringAsync().then((text) => { if (text) tell({ t: "paste", d: text }); }).catch(() => {}), [tell]);

  return (
    <View style={[styles.frame, { paddingBottom: bottomInset }]} accessibilityLabel={canType ? "Live terminal" : "Live terminal (view only)"}>
      <View style={styles.sizes} accessibilityRole="radiogroup" accessibilityLabel="Terminal size">
        {!live && <Text style={styles.wait} maxFontSizeMultiplier={MAX_SCALE} accessibilityLiveRegion="polite" numberOfLines={2}>
          {full ?? (seen ? "Reconnecting…" : silent ? "Nothing has arrived from the Mac in 30 seconds. Update its Lightbulb app to watch this session." : "Connecting…")}
        </Text>}
        <View style={styles.spacer} />
        {(["fit", "readable"] as const).map((option) => (
          <Pressable key={option} onPress={() => setSize(option)} hitSlop={8} accessibilityRole="radio" accessibilityState={{ selected: size === option }}
            accessibilityHint={option === "fit" ? "The whole width at once; pinch to zoom" : "Text at a readable size; scroll sideways for the rest"}>
            <Text maxFontSizeMultiplier={MAX_SCALE} style={[styles.sizeText, size === option && styles.sizeOn]}>{option === "fit" ? "Fit" : "Readable"}</Text>
          </Pressable>
        ))}
      </View>
      <WebView
        key={run}
        ref={web}
        testID="session-terminal"
        style={styles.web}
        source={{ html: TERMINAL_HTML, baseUrl: "about:blank" }}
        originWhitelist={["*"]}
        // The page is whole and loads nothing: a link in terminal output goes nowhere.
        onShouldStartLoadWithRequest={(request) => request.url === "about:blank"}
        onMessage={onMessage}
        onContentProcessDidTerminate={reloaded}
        // The key bar is this view's own; a tap in the terminal raises the keyboard.
        hideKeyboardAccessoryView
        keyboardDisplayRequiresUserAction={false}
        bounces={false}
        overScrollMode="never"
        automaticallyAdjustContentInsets={false}
        contentInsetAdjustmentBehavior="never"
        dataDetectorTypes="none"
        allowsLinkPreview={false}
      />
      {canType && (
        // One row, every key always on it: the keys share the width (9 x 36 pt on the narrowest phone), 44 pt tall.
        <View style={styles.bar} accessibilityRole="toolbar" accessibilityLabel="Terminal keys">
          {KEYS.slice(0, 2).map((k) => <BarKey key={k.key} label={k.label} name={k.name} onPress={() => tell({ t: "key", key: k.key })} />)}
          <BarKey label="ctrl" name="Control, applies to the next key" selected={ctrl} onPress={() => setCtrl(!ctrl)} />
          {KEYS.slice(2).map((k) => <BarKey key={k.key} label={k.label} name={k.name} onPress={() => tell({ t: "key", key: k.key })} />)}
          <BarKey symbol="doc.on.clipboard" name="Paste" onPress={paste} />
          <BarKey symbol="keyboard" name="Show or hide the keyboard" onPress={() => tell({ t: "keyboard" })} />
        </View>
      )}
    </View>
  );
}

function BarKey({ label, symbol, name, selected, onPress }: { label?: string; symbol?: SFSymbol; name: string; selected?: boolean; onPress: () => void }) {
  return (
    <Pressable onPress={onPress} accessibilityRole="button" accessibilityLabel={name} accessibilityState={selected === undefined ? undefined : { selected }}
      style={({ pressed }) => [styles.key, (pressed || selected) && styles.keyOn]}>
      {symbol
        ? <SymbolView accessibilityElementsHidden name={symbol} size={18} tintColor={FG} />
        : <Text maxFontSizeMultiplier={KEY_SCALE} numberOfLines={1} style={styles.keyText}>{label}</Text>}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  frame: { flex: 1, backgroundColor: BG },
  web: { flex: 1, backgroundColor: BG },
  sizes: { flexDirection: "row", alignItems: "center", gap: spacing.md, paddingHorizontal: spacing.md, paddingVertical: spacing.xs, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: EDGE },
  spacer: { flex: 1 },
  wait: { flexShrink: 1, color: FG_MUTE, fontFamily: fonts.sans, fontSize: 12, lineHeight: 16 },
  sizeText: { color: FG_MUTE, fontFamily: fonts.sansMedium, fontSize: 13, lineHeight: 18 },
  sizeOn: { color: FG },
  bar: { flexDirection: "row", gap: 4, paddingHorizontal: 4, paddingVertical: spacing.xs, borderTopWidth: StyleSheet.hairlineWidth, borderColor: EDGE },
  // 44pt tall: the tap-target floor. The width is a ninth of the row.
  key: { flex: 1, height: 44, alignItems: "center", justifyContent: "center", borderRadius: 8, backgroundColor: EDGE },
  keyOn: { backgroundColor: "#4a4a55" },
  keyText: { color: FG, fontFamily: fonts.sansMedium, fontSize: 14, lineHeight: 20 },
});
