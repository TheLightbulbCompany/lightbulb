import { PHONE_VOICES, phoneVoice, voiceSamplePath, type SampleLanguage } from "@lightbulb/shared";
import { useCallback, useEffect, useState } from "react";
import { useFocusEffect } from "expo-router";
import { ActivityIndicator, AppState, Pressable, Switch, Text, View } from "react-native";
import { WebView } from "react-native-webview";

import { API_URL } from "../../config/env";
import { fonts, useChatPalette } from "../../config/theme";
import { useLive } from "../../providers/live";

interface Preferences { voice?: string; spanish?: boolean }

// Two known frontend origins only; neither config text nor a server document can supply HTML or an asset URL.
const SAMPLE_ORIGIN = API_URL === "https://api-dev.thelightbulb.company/api/v1"
  ? "https://dev.thelightbulb.company" : "https://thelightbulb.company";

/** Existing WebView's native media controls; no new audio SDK or runtime provider request. */
export function PhoneVoiceSettings({ config, patch, disabled }: {
  config: Preferences; patch: (next: Preferences) => void; disabled: boolean;
}) {
  const palette = useChatPalette();
  const live = useLive();
  const blocked = !!(live.roomId || live.joining);
  const [language, setLanguage] = useState<SampleLanguage>("en");
  const [preview, setPreview] = useState(false);
  const sampleLanguage = config.spanish ? language : "en";
  const path = voiceSamplePath(config.voice, sampleLanguage);
  useFocusEffect(useCallback(() => () => setPreview(false), []));
  useEffect(() => {
    const listener = AppState.addEventListener("change", (state) => { if (state !== "active") setPreview(false); });
    return () => listener.remove();
  }, []);
  const text = { color: palette.fg, fontFamily: fonts.sans, fontSize: 16 };
  const choose = (next: Preferences) => { setPreview(false); patch(next); };
  return <View style={{ gap: 12 }}>
    <Text accessibilityRole="header" style={text}>Phone voice</Text>
    <View accessibilityRole="radiogroup" style={{ flexDirection: "row", flexWrap: "wrap", gap: 10 }}>
      {PHONE_VOICES.map((voice) => <Pressable key={voice.id} accessibilityRole="radio" accessibilityLabel={voice.label}
        accessibilityState={{ selected: phoneVoice(config.voice) === voice.id, disabled }} disabled={disabled}
        onPress={() => choose({ voice: voice.id })}
        style={{ padding: 12, borderRadius: 12, borderWidth: 1, borderColor: phoneVoice(config.voice) === voice.id ? palette.fg : palette.edge }}>
        <Text style={text}>{voice.label}</Text>
      </Pressable>)}
    </View>
    <View style={{ flexDirection: "row", alignItems: "center", gap: 16 }}>
      <Text style={[text, { flex: 1 }]}>Also speaks Spanish</Text>
      <Switch accessibilityLabel="Also speaks Spanish" disabled={disabled} value={config.spanish === true} onValueChange={(spanish) => choose({ spanish })} />
    </View>
    <Text style={[text, { color: palette.fgSoft, fontSize: 14 }]}>Opens in English. When Spanish is on, follows callers in English or Spanish. Changes apply to the next call.</Text>
    <Text style={text}>Voice sample</Text>
    <Text style={[text, { color: palette.fgSoft, fontSize: 14 }]}>A stock AI voice sample, not your customized greeting.</Text>
    {config.spanish && <View accessibilityRole="radiogroup" style={{ flexDirection: "row", gap: 12 }}>
      {([['en', 'English sample'], ['es', 'Spanish sample']] as const).map(([value, label]) => <Pressable key={value}
        accessibilityRole="radio" accessibilityLabel={label} accessibilityState={{ selected: sampleLanguage === value }}
        onPress={() => { setPreview(false); setLanguage(value); }} style={{ padding: 12 }}><Text style={text}>{label}</Text></Pressable>)}
    </View>}
    {blocked ? <Text accessibilityLiveRegion="polite" style={text}>End your Live conversation to play a voice sample.</Text>
      : preview ? <><StockPlayer key={path} url={`${SAMPLE_ORIGIN}${path}`} />
          <Pressable accessibilityRole="button" accessibilityLabel="Close voice sample player" onPress={() => setPreview(false)} style={{ padding: 12 }}>
            <Text style={[text, { color: palette.link }]}>Close voice sample player</Text></Pressable></>
        : <Pressable accessibilityRole="button" accessibilityLabel="Open voice sample player" onPress={() => setPreview(true)} style={{ padding: 12 }}>
          <Text style={[text, { color: palette.link }]}>Open voice sample player</Text></Pressable>}
  </View>;
}

function StockPlayer({ url }: { url: string }) {
  const palette = useChatPalette();
  const [status, setStatus] = useState("loading");
  useEffect(() => {
    const timer = setTimeout(() => setStatus((current) => current === "loading" ? "error" : current), 15_000);
    return () => clearTimeout(timer);
  }, []);
  // Fixed HTML + event bridge only. Native audio controls require a user gesture; no autoplay or injected scripts.
  const html = `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; media-src ${url}; script-src 'unsafe-inline'; style-src 'unsafe-inline'"><style>body{margin:0;padding:8px}audio{width:100%}</style></head><body><audio aria-label="Voice sample player" controls preload="metadata" src="${url}"></audio><script>const a=document.querySelector('audio');const report=(status)=>window.ReactNativeWebView.postMessage(status);a.addEventListener('loadedmetadata',()=>report('ready'));a.addEventListener('error',()=>report('error'));a.addEventListener('playing',()=>report('playing'));</script></body></html>`;
  return <View style={{ gap: 8 }}>
    <WebView source={{ html }} style={{ height: 76, backgroundColor: "transparent" }} scrollEnabled={false}
      accessibilityLabel="Voice sample player" originWhitelist={['about:blank']} mediaPlaybackRequiresUserAction allowsInlineMediaPlayback
      allowsFullscreenVideo={false} setSupportMultipleWindows={false}
      onShouldStartLoadWithRequest={(request) => request.url === "about:blank"}
      onMessage={(event) => { if (["ready", "error", "playing"].includes(event.nativeEvent.data)) setStatus(event.nativeEvent.data); }}
      onError={() => setStatus("error")} onHttpError={() => setStatus("error")} />
    {status === "loading" && <ActivityIndicator accessibilityLabel="Loading voice sample" color={palette.fg} />}
    {status === "error" && <Text accessibilityRole="alert" style={{ color: palette.alert }}>Couldn’t load this voice sample. Open the player again to retry.</Text>}
  </View>;
}
