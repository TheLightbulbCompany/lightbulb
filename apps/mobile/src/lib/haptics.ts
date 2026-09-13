import * as Haptics from "expo-haptics";
import { Platform } from "react-native";

/**
 * The whole haptics surface for the app — two calls, matching Apple's own
 * rule (and Slack's use of it): a haptic is utility, not decoration, so it
 * fires only where a discrete state actually changed (a reaction toggled, a
 * sheet about to present), never on a plain tap or a send. Both no-op on web
 * (no Taptic engine) and swallow a native failure — a simulator with no
 * haptics support, or any other rejection — so a haptic call can never break
 * the interaction it's decorating.
 */
export function hapticImpact(
  style: Haptics.ImpactFeedbackStyle = Haptics.ImpactFeedbackStyle.Medium,
): void {
  if (Platform.OS === "web") return;
  Haptics.impactAsync(style).catch(() => {});
}

export function hapticSelection(): void {
  if (Platform.OS === "web") return;
  Haptics.selectionAsync().catch(() => {});
}
