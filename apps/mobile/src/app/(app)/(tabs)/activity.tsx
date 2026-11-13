import { useFocusEffect } from "expo-router";
import { useCallback, useState } from "react";
import { RefreshControl, ScrollView, StyleSheet, Text } from "react-native";

import { NeedsYouCard, useNeedsYou } from "../../../components/tasks/NeedsYouCard";
import { FocusedStatusBar } from "../../../components/ui/FocusedStatusBar";
import { fonts, spacing, useChatPalette } from "../../../config/theme";

/** Needs you: only what a person must decide, each card with its own action (web /voice/needs-you). */
export default function NeedsYouScreen() {
  const palette = useChatPalette();
  const { items, failed, reload } = useNeedsYou();
  useFocusEffect(useCallback(() => { reload(); }, [reload]));
  const [refreshing, setRefreshing] = useState(false);
  const onRefresh = () => { setRefreshing(true); reload(); setRefreshing(false); };
  const text = [styles.text, { color: palette.fgSoft }];
  return (
    <ScrollView testID="needs-you" contentInsetAdjustmentBehavior="automatic"
      style={{ backgroundColor: palette.surface }} contentContainerStyle={styles.list}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} />}>
      <FocusedStatusBar />
      <Text style={text}>Only what a person must decide lands here. Answers go into Knowledge, so the same question doesn’t come back.</Text>
      {failed ? <Text accessibilityRole="alert" style={[styles.text, { color: palette.alert }]}>Couldn’t load everything. Pull to retry.</Text> : null}
      {items === null && !failed ? <Text style={text}>Loading…</Text> : null}
      {items?.length === 0 ? <Text style={text}>Nothing needs you.</Text> : null}
      {items?.map((q) => <NeedsYouCard key={q.id} question={q} palette={palette} onDone={reload} />)}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  list: { padding: spacing.lg, gap: spacing.md },
  text: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 22 },
});
