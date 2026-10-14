import * as WebBrowser from "expo-web-browser";
import { Stack, useLocalSearchParams } from "expo-router";
import { Pressable, ScrollView, StyleSheet, Text, useColorScheme, View } from "react-native";

import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../config/theme";

const first = (value: string | string[] | undefined) => Array.isArray(value) ? value[0] : value;

export default function LibraryOutput() {
  const params = useLocalSearchParams<{ title?: string; url?: string; summary?: string; type?: string }>();
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const title = first(params.title) || "Output";
  const url = first(params.url);
  const summary = first(params.summary);
  const type = first(params.type);

  return (
    <>
      <Stack.Screen options={{ title: "Library", headerLargeTitle: true }} />
      <ScrollView contentInsetAdjustmentBehavior="automatic" contentContainerStyle={styles.page}>
        <Text style={styles.overline}>{type?.replaceAll("_", " ").toUpperCase() || "OUTPUT"}</Text>
        <Text selectable style={styles.title}>{title}</Text>
        {summary ? <Text selectable style={styles.summary}>{summary}</Text> : null}
        {url ? (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`View ${title}`}
            style={({ pressed }) => [styles.button, pressed && styles.pressed]}
            onPress={() => void WebBrowser.openBrowserAsync(url)}
          >
            <Text style={styles.buttonText}>View output</Text>
          </Pressable>
        ) : null}
        <View style={styles.note}><Text style={styles.noteText}>Saved in Library for a focused review.</Text></View>
      </ScrollView>
    </>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { padding: spacing.lg, gap: spacing.md },
    overline: { fontFamily: fonts.mono, fontSize: 12, letterSpacing: 1.2, color: p.fgMute },
    title: { fontFamily: fonts.sansSemiBold, fontSize: 30, lineHeight: 36, color: p.fg },
    summary: { fontFamily: fonts.sans, fontSize: 18, lineHeight: 27, color: p.fgSoft },
    button: { minHeight: 52, borderRadius: radii.md, backgroundColor: p.fg, justifyContent: "center", alignItems: "center", paddingHorizontal: spacing.lg },
    buttonText: { fontFamily: fonts.sansSemiBold, fontSize: 17, color: p.onFg },
    pressed: { opacity: 0.7 },
    note: { borderRadius: radii.md, backgroundColor: p.raised, padding: spacing.md },
    noteText: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 22, color: p.fgSoft },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
