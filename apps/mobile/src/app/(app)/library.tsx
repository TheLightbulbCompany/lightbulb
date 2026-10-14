import * as WebBrowser from "expo-web-browser";
import { Stack, useLocalSearchParams } from "expo-router";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing } from "../../config/theme";

const first = (value: string | string[] | undefined) => Array.isArray(value) ? value[0] : value;

export default function LibraryOutput() {
  const params = useLocalSearchParams<{ title?: string; url?: string; summary?: string; type?: string }>();
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

const styles = StyleSheet.create({
  page: { padding: spacing.lg, gap: spacing.md },
  overline: { fontFamily: fonts.mono, fontSize: 12, letterSpacing: 1.2, color: "#6B7280" },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 30, lineHeight: 36, color: "#111827" },
  summary: { fontFamily: fonts.sans, fontSize: 18, lineHeight: 27, color: "#374151" },
  button: { minHeight: 52, borderRadius: radii.md, backgroundColor: "#111827", justifyContent: "center", alignItems: "center", paddingHorizontal: spacing.lg },
  buttonText: { fontFamily: fonts.sansSemiBold, fontSize: 17, color: "#FFFFFF" },
  pressed: { opacity: 0.7 },
  note: { borderRadius: radii.md, backgroundColor: "#F3F4F6", padding: spacing.md },
  noteText: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 22, color: "#4B5563" },
});