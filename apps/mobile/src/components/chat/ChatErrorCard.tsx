import { Linking, Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";

/**
 * Chat error card (spec §4 frame), code-aware for the free-tier gate codes.
 *
 * The backend stamps `code` on error frames — Isol8-side gate codes
 * (`trial_daily_cap` / `trial_expired` from gate_chat, see
 * core/gateway/connection_pool.py) plus OpenClaw errorKinds. The two
 * free-tier codes get tailored copy + an outbound upgrade action (web
 * ChatErrorCard parity): the mobile app never reimplements billing /
 * checkout (D1), so the action deep-links to the web billing panel.
 * Any other (or no) code keeps the plain full-width error card.
 */
const UPGRADE_URL = "https://isol8.co/settings?panel=billing";

export interface ChatErrorPresentation {
  title: string;
  description: string;
  action: { label: string; url: string };
}

/**
 * Map a coded error frame to tailored copy, or null for the plain card.
 * `message` (the backend's copy) wins over the local fallback when present.
 */
export function describeChatError(
  code: string | undefined,
  message: string,
): ChatErrorPresentation | null {
  switch (code) {
    case "trial_daily_cap":
      return {
        title: "Daily free limit reached",
        description:
          message ||
          "You've used today's free allowance — resets tomorrow. Upgrade to keep going.",
        action: { label: "Upgrade on isol8.co", url: UPGRADE_URL },
      };
    case "trial_expired":
      return {
        title: "Your free trial has ended",
        description:
          message || "Your free trial has ended. Pick a plan to keep your agent.",
        action: { label: "Upgrade on isol8.co", url: UPGRADE_URL },
      };
    default:
      return null;
  }
}

export function ChatErrorCard({
  code,
  message,
}: {
  code?: string;
  message: string;
}) {
  const presentation = describeChatError(code, message);
  if (presentation == null) {
    return (
      <View style={styles.card}>
        <Text style={styles.description}>{message}</Text>
      </View>
    );
  }
  const { title, description, action } = presentation;
  return (
    <View style={styles.card}>
      <Text style={styles.title}>{title}</Text>
      <Text style={styles.description}>{description}</Text>
      <Pressable
        style={({ pressed }) => [styles.actionButton, pressed && styles.pressed]}
        onPress={() => void Linking.openURL(action.url)}
        accessibilityRole="button"
        accessibilityLabel={action.label}
      >
        <Text style={styles.actionText}>{action.label}</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  // Same §4 tinted frame the screen's plain error card used.
  card: {
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    padding: spacing.lg,
    gap: spacing.sm,
  },
  title: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
  description: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
  actionButton: {
    alignSelf: "flex-start",
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    paddingHorizontal: spacing.md,
    paddingVertical: 6,
  },
  actionText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
  pressed: { opacity: 0.6 },
});
