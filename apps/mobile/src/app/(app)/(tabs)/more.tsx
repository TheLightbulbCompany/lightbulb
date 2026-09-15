import { SymbolView, type SFSymbol } from "expo-symbols";
import { router, type Href } from "expo-router";
import { Fragment } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../../config/theme";

interface MoreRow {
  label: string;
  description: string;
  icon: SFSymbol;
  route: Href;
}

// An iOS tab bar fits four tabs, and Slack's four are Home · DMs · Activity ·
// More. These route trees are PUSHED screens on the (app) stack — not
// hidden tabs — so each one arrives with a real native back button, and this
// screen is the only way a user reaches them.
const ROWS: MoreRow[] = [
  {
    label: "Teams",
    description: "Inbox, issues, and routines",
    icon: "tray",
    route: "/(app)/teams",
  },
  {
    label: "Shared sessions",
    description: "Live Claude and Codex terminals in this workspace",
    icon: "terminal",
    route: "/(app)/sessions",
  },
  {
    label: "Control",
    description: "Agents, skills, usage, and billing",
    icon: "slider.horizontal.3",
    route: "/(app)/control",
  },
  {
    label: "Settings",
    description: "Account, notifications, and sign out",
    icon: "gearshape",
    route: "/(app)/settings",
  },
];

export default function More() {
  return (
    <ScrollView
      contentInsetAdjustmentBehavior="automatic"
      style={styles.page}
      contentContainerStyle={styles.content}
    >
      <View style={styles.card}>
        {ROWS.map((row, i) => (
          <Fragment key={row.label}>
            {i > 0 && <View style={styles.divider} />}
            <Pressable
              style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
              onPress={() => router.push(row.route)}
              accessibilityRole="button"
              accessibilityLabel={row.label}
            >
              <View style={styles.iconTile}>
                <SymbolView accessibilityElementsHidden name={row.icon} size={16} tintColor={colors.forest} />
              </View>
              <View style={styles.rowText}>
                <Text style={styles.rowLabel} numberOfLines={1}>
                  {row.label}
                </Text>
                <Text style={styles.rowDescription} numberOfLines={1}>
                  {row.description}
                </Text>
              </View>
              <SymbolView accessibilityElementsHidden name="chevron.right" size={18} tintColor={colors.muteSoft} />
            </Pressable>
          </Fragment>
        ))}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: { padding: spacing.lg, paddingBottom: spacing.xl },
  card: {
    backgroundColor: colors.cream,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    overflow: "hidden",
  },
  divider: {
    height: StyleSheet.hairlineWidth,
    backgroundColor: colors.edge,
    marginLeft: spacing.lg,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
  },
  rowPressed: { backgroundColor: colors.creamPressed },
  iconTile: {
    width: 32,
    height: 32,
    borderRadius: radii.md,
    backgroundColor: colors.forestTint,
    alignItems: "center",
    justifyContent: "center",
  },
  rowText: { flex: 1, gap: 2 },
  rowLabel: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  rowDescription: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
});
