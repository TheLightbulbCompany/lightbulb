/**
 * Phased provisioning surface for a container cold start (mobile
 * onboarding-parity Slice 3).
 *
 * Mobile previously showed ONE thin amber bar for every cold start, so a
 * 3-5 minute Fargate first-task provision looked identical to a 2-second
 * wake — no phase, no clock, no way to tell "working" from "stuck". Web has
 * a full stepper for exactly this window
 * (`apps/frontend/src/components/chat/ProvisioningStepper.tsx`). This is the
 * mobile half, time-boxed:
 *
 *   • parked (deliberately-asleep trial box)  → ALWAYS the thin banner. It is
 *     healthy and one intent away from running; the poll already carries
 *     wake=1, so it clears in seconds and must never take the screen.
 *   • starting, elapsed < PROMOTE_MS          → thin banner over live children.
 *   • starting, elapsed ≥ PROMOTE_MS          → full-screen stepper.
 *
 * The banner-first window is the deliberate divergence from web (which blocks
 * immediately): a quick start or wake must never flash a full-screen takeover,
 * but a genuinely slow one earns the phase/clock/support affordances.
 *
 * Copy + timing (ideas, rotation cadence, soft-timeout budget, mm:ss format)
 * come from `@isol8/shared` so this and the web stepper can't drift.
 *
 * The 3-step rail is derived from `phase`/`substatus` ALONE — deliberately no
 * gateway-health RPC (web splits its "starting" window further using one).
 * Mobile keeps three status-derived steps rather than adding a second network
 * dependency to a screen whose whole job is "the network isn't ready yet".
 */
import { Fragment, useEffect, useRef, useState } from "react";
import {
  AccessibilityInfo,
  ActivityIndicator,
  Animated,
  Linking,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import {
  PROVISION_IDEAS,
  PROVISION_IDEA_ROTATION_MS,
  PROVISION_TIMEOUT_MS,
  formatProvisionElapsed,
} from "@isol8/shared";

import { usePulse } from "../chat/usePulse";
import { colors, fonts, radii, spacing } from "../../config/theme";
import type { ContainerStatus } from "../../lib/containerStatus";
import { Wordmark } from "./Wordmark";

/**
 * How long a `starting` container may take before the thin banner is promoted
 * to the full-screen stepper. Mobile-only — web has no banner phase.
 */
const PROMOTE_MS = 10_000;

/** Crossfade half-duration for the rotating idea line (matches web's 180ms). */
const IDEA_FADE_MS = 180;

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

// ---------------------------------------------------------------------------
// Banner mode (quick starts + every parked wake)
// ---------------------------------------------------------------------------

function PulsingDot() {
  const opacity = usePulse(true, 0.3);
  return <Animated.View style={[styles.bannerDot, { opacity }]} />;
}

function StartingBanner({ label }: { label: string }) {
  const insets = useSafeAreaInsets();
  return (
    <View style={[styles.banner, { paddingTop: insets.top }]}>
      <View style={styles.bannerRow}>
        <PulsingDot />
        <Text style={styles.bannerText} numberOfLines={1}>
          {label}
        </Text>
      </View>
    </View>
  );
}

// ---------------------------------------------------------------------------
// Full-screen stepper pieces
// ---------------------------------------------------------------------------

/** Spinner inside a soft forest ring, the ring breathing via usePulse. */
function ProvisionRing() {
  const opacity = usePulse(true, 0.5);
  return (
    <View style={styles.stage}>
      <Animated.View style={[styles.ring, { opacity }]} />
      <ActivityIndicator size="large" color={colors.forest} />
    </View>
  );
}

/**
 * Rotating "while you wait" prompts. Crossfades on the shared rotation
 * cadence; under reduce-motion it still rotates but swaps instantly, same
 * bargain `usePulse` makes (keep the information, drop the animation).
 */
function RotatingIdeas() {
  const [idx, setIdx] = useState(0);
  const opacity = useRef(new Animated.Value(1)).current;
  const reduceMotionRef = useRef(false);

  useEffect(() => {
    let cancelled = false;
    AccessibilityInfo.isReduceMotionEnabled()
      .then((reduceMotion) => {
        if (!cancelled) reduceMotionRef.current = reduceMotion;
      })
      .catch(() => {
        // Best-effort: no signal means we simply keep the crossfade.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const advance = () => setIdx((i) => (i + 1) % PROVISION_IDEAS.length);
    const id = setInterval(() => {
      if (reduceMotionRef.current) {
        advance();
        return;
      }
      Animated.timing(opacity, {
        toValue: 0,
        duration: IDEA_FADE_MS,
        useNativeDriver: true,
      }).start(() => {
        advance();
        Animated.timing(opacity, {
          toValue: 1,
          duration: IDEA_FADE_MS,
          useNativeDriver: true,
        }).start();
      });
    }, PROVISION_IDEA_ROTATION_MS);
    return () => clearInterval(id);
  }, [opacity]);

  return (
    // Fixed min-height: the phrases differ in length, so without reserved
    // space a two-line idea would shove the rail down mid-rotation.
    <View style={styles.ideas}>
      <Text style={styles.ideaEyebrow}>WHILE YOU WAIT</Text>
      <Animated.Text style={[styles.idea, { opacity }]}>
        {PROVISION_IDEAS[idx]}
      </Animated.Text>
    </View>
  );
}

/** 3 dots + connectors. Dot states mirror web's `.provision-dot`. */
function DotRail({ active }: { active: number }) {
  return (
    <View
      style={styles.dotRail}
      testID="provision-dot-rail"
      // Three unlabelled dots say nothing to a screen reader — name the rail
      // with what just finished and where we are.
      accessibilityRole="progressbar"
      accessibilityLabel={
        active === 0
          ? `Step 1 of ${STEPS.length}`
          : `${STEPS[active - 1].done}. Step ${active + 1} of ${STEPS.length}`
      }
    >
      {STEPS.map((step, i) => (
        <Fragment key={step.key}>
          {i > 0 ? <View style={styles.connector} /> : null}
          <View
            style={[
              styles.dot,
              i < active && styles.dotDone,
              i === active && styles.dotActive,
            ]}
          />
        </Fragment>
      ))}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Host
// ---------------------------------------------------------------------------

interface Props {
  status:
    | Extract<ContainerStatus, { kind: "starting" }>
    | Extract<ContainerStatus, { kind: "parked" }>;
  /** The gate's refetch — re-probes /container/status. */
  onRetry: () => void;
  children: React.ReactNode;
}

export function ProvisioningStepper({ status, onRetry, children }: Props) {
  const insets = useSafeAreaInsets();

  // Mount-scoped elapsed clock. This component only exists while
  // starting/parked — reaching ready unmounts it — so there is no cross-phase
  // reset to do, unlike web's stepper (which lives across the whole flow and
  // has to reset its timer when a ready container regresses).
  const startedAtRef = useRef<number | null>(null);
  if (startedAtRef.current === null) startedAtRef.current = Date.now();
  const [elapsedMs, setElapsedMs] = useState(0);
  useEffect(() => {
    const id = setInterval(() => {
      const start = startedAtRef.current;
      if (start === null) return;
      setElapsedMs(Date.now() - start);
    }, 1000);
    return () => clearInterval(id);
  }, []);

  // `parked` never promotes: it's a healthy box waking on its own, measured in
  // seconds, and blanking readable content for it would be a regression. Only
  // a `starting` container that has genuinely dragged earns the full screen.
  if (status.kind === "parked" || elapsedMs < PROMOTE_MS) {
    return (
      <View style={styles.root}>
        <StartingBanner
          label={
            status.kind === "parked"
              ? "Waking your agents…"
              : "Your workspace is starting…"
          }
        />
        {children}
      </View>
    );
  }

  // Narrowed to the `starting` variant by the guard above.
  const active = activeStepIndex(status.phase, status.substatus);
  const timedOut = elapsedMs > PROVISION_TIMEOUT_MS;

  return (
    <View style={[styles.page, { paddingTop: insets.top + spacing.xl }]}>
      <Wordmark />

      <ProvisionRing />

      <Text
        style={styles.title}
        // Announce each phase change without stealing focus.
        accessibilityLiveRegion="polite"
        accessibilityRole="text"
        testID="provision-title"
      >
        {STEPS[active].active}
      </Text>

      <RotatingIdeas />

      <DotRail active={active} />

      <Text style={styles.elapsed} testID="provision-elapsed">
        {formatProvisionElapsed(elapsedMs)}
      </Text>

      {timedOut ? (
        <View style={styles.timeout} testID="provision-timeout">
          <Text style={styles.timeoutMsg}>This is taking longer than usual.</Text>
          <View style={styles.timeoutActions}>
            <Pressable
              style={({ pressed }) => [styles.timeoutButton, pressed && styles.pressed]}
              onPress={onRetry}
              accessibilityRole="button"
              accessibilityLabel="Retry"
            >
              <Text style={styles.timeoutButtonText}>Retry</Text>
            </Pressable>
            <Pressable
              style={({ pressed }) => [styles.timeoutButton, pressed && styles.pressed]}
              onPress={() => void Linking.openURL("mailto:support@isol8.co")}
              accessibilityRole="button"
              accessibilityLabel="Contact support"
            >
              <Text style={styles.timeoutButtonMutedText}>Contact support</Text>
            </Pressable>
          </View>
        </View>
      ) : null}
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
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.bannerText,
  },

  // Full-screen stepper — cardless on cream, mirroring web's provision-card.
  page: {
    flex: 1,
    backgroundColor: colors.cream,
    alignItems: "center",
    justifyContent: "center",
    padding: spacing.xl,
    gap: spacing.lg,
  },
  stage: {
    width: 80,
    height: 80,
    alignItems: "center",
    justifyContent: "center",
  },
  ring: {
    position: "absolute",
    width: 64,
    height: 64,
    borderRadius: 32,
    borderWidth: 2,
    borderColor: colors.edge,
    borderTopColor: colors.forest,
  },
  title: {
    fontFamily: fonts.lora,
    fontSize: 22,
    lineHeight: 28,
    color: colors.ink,
    textAlign: "center",
  },
  ideas: {
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.xs,
    minHeight: 60,
    alignSelf: "stretch",
  },
  ideaEyebrow: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 11,
    lineHeight: 15,
    letterSpacing: 0.5,
    color: colors.mute,
  },
  idea: {
    // The registered italic FACE, not `fonts.lora` + `fontStyle: "italic"`:
    // RN matches a PostScript family name exactly, so asking a
    // regular-only family to slant is a no-op on iOS (same reason
    // `interstitialGlyph` and the empty states use this token).
    fontFamily: fonts.loraItalic,
    fontSize: 15,
    lineHeight: 21,
    color: colors.inkSoft,
    textAlign: "center",
  },
  dotRail: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
  },
  dot: {
    width: 10,
    height: 10,
    borderRadius: 5,
    backgroundColor: colors.edge,
  },
  dotActive: {
    backgroundColor: colors.forest,
    transform: [{ scale: 1.2 }],
  },
  dotDone: {
    backgroundColor: colors.runningGreen,
  },
  connector: {
    width: 32,
    height: 2,
    backgroundColor: colors.edge,
  },
  elapsed: {
    fontFamily: fonts.mono,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  // Soft-timeout block — calm, not alarming; the provision is still running.
  timeout: {
    alignSelf: "stretch",
    alignItems: "center",
    gap: spacing.md,
    paddingTop: spacing.lg,
    borderTopWidth: 1,
    borderTopColor: colors.edge,
  },
  timeoutMsg: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 19,
    color: colors.inkSoft,
    textAlign: "center",
  },
  timeoutActions: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
  },
  timeoutButton: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
    borderRadius: radii.md,
  },
  timeoutButtonText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.forest,
  },
  timeoutButtonMutedText: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 18,
    color: colors.mute,
  },
  pressed: { opacity: 0.6 },
});
