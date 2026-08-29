import Feather from "@expo/vector-icons/Feather";
import { router, type Href } from "expo-router";
import { Fragment, type ComponentProps } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../../config/theme";

type FeatherName = ComponentProps<typeof Feather>["name"];

interface PanelRow {
  key: string;
  label: string;
  description: string;
  icon: FeatherName;
  /** Nested-Stack route pushed on tap. */
  route: Href;
}

// Mirrors the web ControlSidebar NAV_ITEMS (w3-control-contract §"Actual
// NAV_ITEMS") plus a Marketplace entry. All panels are implemented. Dreams +
// Skill Proposals are omitted — low-frequency, need their own memory/proposal
// data plumbing, out of scope for this build.
const PANELS: PanelRow[] = [
  {
    key: "overview",
    label: "Overview",
    description: "Health, plan, and live counts",
    icon: "activity",
    route: "/(app)/control/overview",
  },
  {
    key: "agents",
    label: "Agents",
    description: "Your roster and per-agent settings",
    icon: "users",
    route: "/(app)/control/agents",
  },
  {
    key: "skills",
    label: "Skills",
    description: "Installed and available skills",
    icon: "zap",
    route: "/(app)/control/skills",
  },
  {
    key: "sessions",
    label: "Sessions",
    description: "Recent conversations and tokens",
    icon: "layers",
    route: "/(app)/control/sessions",
  },
  {
    key: "channels",
    label: "Channels",
    description: "Telegram, Discord, and Slack status",
    icon: "radio",
    route: "/(app)/control/channels",
  },
  {
    key: "usage",
    label: "Usage",
    description: "Token spend this billing period",
    icon: "bar-chart-2",
    route: "/(app)/control/usage",
  },
  {
    key: "llm",
    label: "Model & Credits",
    description: "Provider, model, and Bedrock credits",
    icon: "cpu",
    route: "/(app)/control/llm",
  },
  {
    key: "integrations",
    label: "Integrations",
    description: "Connections, MCP servers, and plugins",
    icon: "grid",
    route: "/(app)/control/integrations",
  },
  {
    key: "marketplace",
    label: "Marketplace",
    description: "Discover and deploy free community agents",
    icon: "shopping-bag",
    route: "/(app)/control/marketplace",
  },
];

export default function ControlIndex() {
  return (
    <ScrollView style={styles.page} contentContainerStyle={styles.content}>
      <Text style={styles.overline}>PANELS</Text>
      <View style={styles.card}>
        {PANELS.map((panel, i) => (
          <Fragment key={panel.key}>
            {i > 0 && <View style={styles.divider} />}
            <Pressable
              style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
              onPress={() => router.push(panel.route)}
              accessibilityRole="button"
              accessibilityLabel={panel.label}
            >
              <View style={styles.iconTile}>
                <Feather name={panel.icon} size={16} color={colors.forest} />
              </View>
              <View style={styles.rowText}>
                <Text style={styles.rowLabel} numberOfLines={1}>
                  {panel.label}
                </Text>
                <Text style={styles.rowDescription} numberOfLines={1}>
                  {panel.description}
                </Text>
              </View>
              <Feather name="chevron-right" size={18} color={colors.muteSoft} />
            </Pressable>
          </Fragment>
        ))}
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: {
    padding: spacing.lg,
    gap: spacing.sm,
    paddingBottom: spacing.xl,
  },
  overline: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
    paddingHorizontal: spacing.xs,
  },

  // Grouped white card (settings card pattern; radius lg, hairline edge).
  card: {
    backgroundColor: colors.white,
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

  // Panel row (spec §5 row: icon + text + chevron).
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
