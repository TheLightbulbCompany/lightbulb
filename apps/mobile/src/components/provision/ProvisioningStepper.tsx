/**
 * Pass-through host for every non-blocking container state (mobile onboarding-parity Slice 3).
 *
 *   • ready / loading / unreachable / parked → `children` and nothing else.
 *   • starting                               → a thin banner over live children. #general stays open and takes a
 *     message; Lightbulb answers it once setup finishes. Never a full-screen card.
 *
 * ONE component serves every non-blocking state on purpose. The gate hands `children` to this component
 * unconditionally, so a starting → ready flip only changes what renders AROUND `children` and React reconciles in
 * place. `children` here contains the entire live app, so a type change would cost composer drafts, loaded message
 * lists and scroll position on every cold start and every wake.
 *
 * The 3-step count is derived from `phase`/`substatus` alone (no gateway-health RPC).
 */
import { Animated, StyleSheet, Text, View } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { usePulse } from "../chat/usePulse";
import { colors, fonts, spacing } from "../../config/theme";
import type { ContainerStatus } from "../../lib/containerStatus";

/**
 * The three visible steps. Labels match web's STEPS_PAID rows for the same
 * phases, so a user who onboarded on one surface recognises the other.
 */
const STEPS = [
  { key: "container", active: "Provisioning your container…", done: "Container ready" },
  { key: "gateway", active: "Connecting to your agent's brain…", done: "Gateway connected" },
  {
    key: "agent",
    active: "Putting the finishing touches on your agent…",
    done: "Agent online",
  },
] as const;

/**
 * Which step the rail is on. Mirrors web's `derivePhase` for the cold-start
 * window, minus the gateway-health RPC (see module docstring):
 *
 *   provisioning + substatus !== "task_running" → 0 (container)
 *   provisioning + substatus === "task_running" → 1 (gateway — the ECS task is
 *                                                    RUNNING with an IP but the
 *                                                    gateway port hasn't opened)
 *   starting                                    → 2 (agent — the gateway port
 *                                                    answered; channels/sidecars
 *                                                    are loading)
 *
 * Exported for direct unit testing.
 */
export function activeStepIndex(phase?: string, substatus?: string): number {
  if (phase === "provisioning") return substatus === "task_running" ? 1 : 0;
  if (phase === "starting") return 2;
  return 0; // unknown / early — stay conservative on the first step
}

function PulsingDot() {
  const opacity = usePulse(true, 0.3);
  return <Animated.View style={[styles.bannerDot, { opacity }]} />;
}

function StartingBanner({ label }: { label: string }) {
  const insets = useSafeAreaInsets();
  return (
    <View style={[styles.banner, { paddingTop: insets.top }]} accessibilityLiveRegion="polite">
      <View style={styles.bannerRow}>
        <PulsingDot />
        <Text style={styles.bannerText} numberOfLines={2}>
          {label}
        </Text>
      </View>
    </View>
  );
}

interface Props {
  /**
   * The full polled status, not just the cold-start variants: this component
   * is the gate's single child for EVERY non-blocking state (see module
   * docstring) precisely so `children` never changes element type.
   */
  status: ContainerStatus;
  children: React.ReactNode;
}

export function ProvisioningStepper({ status, children }: Props) {
  // `children` sits at a FIXED index in a static 2-child array, so flipping starting → ready only turns the banner
  // slot into `null` — React updates in place and the tab navigator underneath never remounts. No banner for
  // `parked`: on AgentCore it is only "idle for 4 h", nothing is waking, and the next message starts the runtime itself.
  const activeStep = status.kind === "starting" ? activeStepIndex(status.phase, status.substatus) : 0;
  return (
    <View style={styles.root}>
      {status.kind === "starting" ? (
        <StartingBanner label={`Setting up, step ${activeStep + 1} of ${STEPS.length}. You can write to Lightbulb now — it answers as soon as setup finishes.`} />
      ) : null}
      {children}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },

  // Non-blocking amber bar (spec §4 disconnected banner tokens).
  banner: {
    backgroundColor: colors.bannerBg,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.bannerBorder,
  },
  bannerRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  bannerDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.connectingYellow,
  },
  bannerText: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.bannerText,
  },
});
