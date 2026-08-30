import { StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { purchaseGateCopy } from "../../lib/purchaseCta";

/**
 * Chat error card (spec §4 frame), code-aware for the free-tier gate codes.
 *
 * The backend stamps `code` on error frames — Lightbulb-side gate codes
 * (`trial_daily_cap` / `trial_expired` from gate_chat, see
 * core/gateway/connection_pool.py) plus OpenClaw errorKinds. The two
 * free-tier codes get tailored copy; any other (or no) code keeps the plain
 * full-width error card.
 *
 * DIVERGES FROM WEB, deliberately (App Store 3.1.3(f) — see lib/purchaseCta):
 * web renders an "Upgrade on flickmeon.ai" button here and prefers the server's
 * message. iOS renders neither. The server copy for both of these codes ends
 * in a call to action ("Upgrade to keep going" / "Pick a plan to keep your
 * agent"), so for a purchase gate the LOCAL text wins and no action is shown —
 * the inverse of the `message || fallback` precedence used everywhere else.
 */
export interface ChatErrorPresentation {
  title: string;
  description: string;
}

/**
 * Map a coded error frame to tailored copy, or null for the plain card.
 *
 * Takes no `message`: for the codes this handles, the server's copy is exactly
 * what must not be shown. Uncoded frames still render the raw message — via
 * the plain card below, not through here.
 */
export function describeChatError(
  code: string | undefined,
): ChatErrorPresentation | null {
  const local = purchaseGateCopy(code);
  if (local == null) return null;
  return { title: local.title, description: local.body };
}

export function ChatErrorCard({
  code,
  message,
}: {
  code?: string;
  message: string;
}) {
  const presentation = describeChatError(code);
  if (presentation == null) {
    return (
      <View style={styles.card}>
        <Text style={styles.description}>{message}</Text>
      </View>
    );
  }
  const { title, description } = presentation;
  return (
    <View style={styles.card}>
      <Text style={styles.title}>{title}</Text>
      <Text style={styles.description}>{description}</Text>
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
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
  description: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
});
