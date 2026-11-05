/**
 * Provisioning surface AND pass-through host for every non-blocking container
 * state (mobile onboarding-parity Slice 3).
 *
 * Mobile previously showed ONE thin amber bar for every cold start, so a
 * 3-5 minute Fargate first-task provision looked identical to a 2-second
 * wake — no phase, no clock, no way to tell "working" from "stuck". Web has
 * a full stepper for exactly this window
 * (`apps/frontend/src/components/chat/ProvisioningStepper.tsx`). This is the
 * mobile half, time-boxed:
 *
 *   • ready / loading / unreachable            → `children` and nothing else.
 *   • parked (deliberately-asleep trial box)   → thin banner over live
 *     children, ALWAYS. It is healthy and one intent away from running (the
 *     next invoke wakes it), so it never takes the screen.
 *   • starting on a FIRST cold start, elapsed < PROMOTE_MS
 *                                              → thin banner over live children.
 *   • starting on a FIRST cold start, elapsed ≥ PROMOTE_MS
 *                                              → full-screen stepper (blocking).
 *   • starting AFTER this mount has once seen ready or parked
 *                                              → thin banner over live
 *     children, for as long as it runs. See `settledOnceRef` below.
 *
 * The banner-first window is the deliberate divergence from web (which blocks
 * immediately): a quick start or wake never flashes a full-screen takeover,
 * but a genuinely slow FIRST one earns the phase/clock/support affordances.
 *
 * ONE component serves all five non-blocking states on purpose. The gate hands
 * `children` to this component unconditionally, so a starting → ready flip only
 * changes what renders AROUND `children` and React reconciles in place.
 * Returning a bare <View> for `ready` and this component for `starting` would
 * change the child's element TYPE and tear down the whole subtree on every
 * transition — `children` here contains the entire live app, so that costs
 * composer drafts, loaded message lists and scroll position on every cold
 * start and every wake.
 *
 * The elapsed clock measures ONLY the current contiguous run of one status
 * kind, so the promotion budget (and the elapsed readout / soft-timeout it
 * feeds) is spent on the current `starting` run alone — never on the
 * `loading` probe that precedes a first cold start.
 *
 * Copy + timing (ideas, rotation cadence, soft-timeout budget, mm:ss format)
 * come from `@lightbulb/shared` so this and the web stepper can't drift.
 *
 * The 3-step rail is derived from `phase`/`substatus` ALONE — deliberately no
 * gateway-health RPC (web splits its "starting" window further using one).
 * Mobile keeps three status-derived steps rather than adding a second network
 * dependency to a screen whose whole job is "the network isn't ready yet".
 */
import { useAuth } from "@clerk/expo";
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import {
  AccessibilityInfo,
  ActivityIndicator,
  Alert,
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
  PROVISION_STUCK_MS,
  PROVISION_TIMEOUT_MS,
  formatProvisionElapsed,
} from "@lightbulb/shared";

import { usePulse } from "../chat/usePulse";
import { colors, fonts, radii, spacing } from "../../config/theme";
import type { ContainerStatus } from "../../lib/containerStatus";
import { Wordmark } from "./Wordmark";
import { useSuperAgent } from "../../providers/live-events";

/**
 * How long a FIRST-cold-start `starting` container may take before the thin
 * banner is promoted to the full-screen stepper. Mobile-only — web has no
 * banner phase. Not a budget for wakes/restarts: those never promote at all
 * (see `settledOnceRef`).
 */
const PROMOTE_MS = 10_000;

/** Crossfade half-duration for the rotating idea line (matches web's 180ms). */
const IDEA_FADE_MS = 180;

/** The one escape hatch this screen has. Also used by the failure notice. */
const SUPPORT_MAILTO = "mailto:support@thelightbulb.company";

/** Replaces the step label once the kickoff is known to have failed. The step
 *  labels are all present-progressive ("Putting the finishing touches on your
 *  agent…"), i.e. claims that work is under way. */

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

// ---------------------------------------------------------------------------
// Full-screen stepper pieces
// ---------------------------------------------------------------------------

/**
 * Spinner inside a soft forest ring, the ring breathing via usePulse.
 *
 * `animating={false}` stops both the spinner and the breath when callers need
 * a static status mark.
 */
function ProvisionRing({ animating = true }: { animating?: boolean }) {
  const opacity = usePulse(animating, 0.5);
  return (
    <View style={styles.stage}>
      <Animated.View style={[styles.ring, { opacity }]} />
      {animating ? <ActivityIndicator size="large" color={colors.forest} /> : null}
    </View>
  );
}

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
      // with what just finished and where we are. `accessible` is what makes
      // the rail ONE element with that label: without it, iOS keeps walking
      // into the (unlabelled, decorative) child dots and the label below is
      // never spoken.
      accessible
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
  /**
   * The full polled status, not just the cold-start variants: this component
   * is the gate's single child for EVERY non-blocking state (see module
   * docstring) precisely so `children` never changes element type.
   */
  status: ContainerStatus;
  /** The gate's refetch — re-probes /container/status. */
  onRetry: () => void;
  children: React.ReactNode;
}

export function ProvisioningStepper({ status, onRetry, children }: Props) {
  const insets = useSafeAreaInsets();
  const { signOut } = useAuth();

  // Has this mount ever seen a settled container? `ready` obviously, and
  // `parked` too — a parked box is ready-equivalent (healthy, asleep on
  // purpose, one intent away from running), which is exactly how web's
  // `reachedReady` latch treats it.
  //
  // This is what makes "a wake must never take the screen" TRUE rather than
  // merely time-boxed. A free-tier wake reads `parked` for ONE poll and then
  // spends the next 30-90s in `starting`: the wake dispatch writes
  // status="provisioning" immediately, and the backend maps every non-running
  // status to phase="provisioning" (see core/services/readiness_state.py's
  // resolve_runtime_phase), so ECS placement, ENI attach and the container's
  // `health` probe (over container_rpc, per request -- no persistent
  // connection to hold) all read as `starting`. Behind that sits the live tab
  // navigator with a composer draft, a loaded message list, a scroll position
  // and a navigation stack. Same story for an idle scale-to-zero restart, and
  // for the documented case where /container/status reports `starting` over a
  // perfectly working container because that per-request health probe hasn't
  // landed yet. On a genuine FIRST cold start none of that exists yet —
  // nothing to lose, and the stepper is the only thing to look at — so the
  // promotion still happens there and only there.
  //
  // Derived from the current prop and written during render (same pattern as
  // the clock below): it is therefore already true on the very render that
  // first observes ready/parked, with no question about whether an effect
  // from the previous commit has run yet.
  const settledOnceRef = useRef(false);
  if (status.kind === "ready" || status.kind === "parked") {
    settledOnceRef.current = true;
  }

  // Elapsed clock, scoped to the current contiguous run of ONE status kind, so
  // the promotion budget below — and the elapsed readout and soft timeout the
  // promoted stepper shows — measure the current `starting` run and not the
  // `loading` probe that precedes a first cold start. Restamped DURING render
  // (React's "adjust state when a prop changes" pattern) rather than from an
  // effect: an effect lands after the frame is already on screen, so the first
  // `starting` frame would render against the previous run's stamp — a
  // full-screen takeover for one frame when the budget was already spent.
  const [clock, setClock] = useState(() => ({
    kind: status.kind,
    startedAt: Date.now(),
  }));
  if (clock.kind !== status.kind) {
    setClock({ kind: status.kind, startedAt: Date.now() });
  }

  // Tick only while something is reading the clock. This component is mounted
  // for the WHOLE app session (it hosts every non-blocking state), so an
  // ungated 1s interval would run forever behind a healthy `ready` container.
  const ticking = status.kind === "starting" || status.kind === "parked";
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!ticking) return;
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [ticking]);

  // Clamped: on the render right after a restamp, `nowMs` still holds the
  // PREVIOUS tick and so predates the new stamp — which reads as 0 elapsed,
  // exactly what a freshly-entered run should report. The next tick (≤1s away,
  // and the interval is already running for any kind that reads this) corrects
  // it, so the clamp is only ever load-bearing for that first frame.
  const elapsedMs = Math.max(0, nowMs - clock.startedAt);

  // Promote on a FIRST cold start only: `starting`, nothing settled on this
  // mount yet (see `settledOnceRef`), and past the banner budget. `parked`
  // never promotes at all — it's a healthy box waking on its own, measured in
  // seconds, and blanking readable content for it would be a regression.
  // Super-agent: never promoted. #general stays open and takes a message; Lightbulb answers it once setup finishes.
  const superAgent = useSuperAgent();
  const promoted =
    !superAgent &&
    status.kind === "starting" &&
    !settledOnceRef.current &&
    elapsedMs >= PROMOTE_MS;
  const activeStep =
    status.kind === "starting"
      ? activeStepIndex(status.phase, status.substatus)
      : 0;

  // iOS has no live regions (`accessibilityLiveRegion` is documented
  // "@platform android" in ViewAccessibility.d.ts) and iOS is the only
  // platform this app ships on, so announce phase changes explicitly. Scoped
  // to the promoted stepper — the thin banner is a line a VoiceOver user can
  // swipe to, not something worth interrupting them for.
  useEffect(() => {
    if (!promoted) return;
    AccessibilityInfo.announceForAccessibility(
      STEPS[activeStep].active,
    );
  }, [promoted, activeStep]);

  // The promoted stepper replaces the whole tab shell, so the gate's rule for
  // the Interstitial applies verbatim here: it is the only exit a wrong-account
  // user has, and Retry + a mailto is not an exit.
  const onSignOut = useCallback(() => {
    Alert.alert("Sign out", "Sign out of this account?", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive", onPress: () => void signOut() },
    ]);
  }, [signOut]);

  if (!promoted) {
    // `children` sits at a FIXED index in a static 2-child array, so flipping
    // starting → ready only turns the banner slot into `null` — React updates
    // in place and the tab navigator underneath never remounts.
    return (
      <View style={styles.root}>
        {/* No banner for `parked`: on AgentCore it is only "idle for 4 h",
            nothing is waking, and the next message starts the runtime itself
            (its run shows as working). A banner there never cleared. */}
        {status.kind === "starting" ? (
          <StartingBanner label={superAgent
            ? `Setting up, step ${activeStep + 1} of ${STEPS.length}. You can write to Lightbulb now — it answers as soon as setup finishes.`
            : "Your workspace is starting…"} />
        ) : null}
        {children}
      </View>
    );
  }

  const timedOut = elapsedMs > PROVISION_TIMEOUT_MS;
  // Second, harder tier. Derived from the clock rather than held as state, so
  // nothing can erase it: web's equivalent button used to clear its own
  // escalation flag AND zero the clock, which is how 21 hours kept reading as
  // five minutes. Mobile's Retry only refetches — a poll that comes back
  // `starting` again doesn't change `status.kind`, so it never restamps the
  // clock either. Both halves of that rule now hold here by construction.
  const stuck = elapsedMs > PROVISION_STUCK_MS;

  return (
    <View style={[styles.page, { paddingTop: insets.top + spacing.xl }]}>
      <Wordmark />

      <ProvisionRing />

      <Text style={styles.title} accessibilityRole="text" testID="provision-title">
        {STEPS[activeStep].active}
      </Text>

      {/* Keep chat-first prompts visible while the workspace starts. */}
      <RotatingIdeas />

      {/* The rail reports the current provisioning phase. */}
      <DotRail active={activeStep} />

      <Text style={styles.elapsed} testID="provision-elapsed">
        {formatProvisionElapsed(elapsedMs)}
      </Text>

      {/* A slow start exposes a status refresh and support path. */}
      {timedOut ? (
        <View style={styles.timeout} testID="provision-timeout">
          <Text style={styles.timeoutMsg}>
            {stuck
              ? "This is taking longer than it should — something is stuck on our side. Setup normally finishes in a few minutes. Your progress is saved; contact us and we’ll get it moving."
              : "This is taking longer than usual."}
          </Text>
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
              onPress={() => void Linking.openURL(SUPPORT_MAILTO)}
              accessibilityRole="button"
              accessibilityLabel="Contact support"
            >
              <Text style={styles.timeoutButtonMutedText}>Contact support</Text>
            </Pressable>
          </View>
        </View>
      ) : null}

      <Pressable
        style={({ pressed }) => [styles.signOutButton, pressed && styles.pressed]}
        onPress={onSignOut}
        accessibilityRole="button"
        accessibilityLabel="Sign out"
      >
        <Text style={styles.signOutText}>Sign out</Text>
      </Pressable>
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

  // First-run failure — a real failure, not a "needs attention" nudge, so it
  // uses the error tokens rather than the amber ones. Two chromes, one fact:
  // `firstRunBanner` is the thin bar over live children (the gate's `ready`
  // rendering); `firstRunInline` is the bordered block inside the promoted
  // stepper's card, where there is nothing behind it to bar.
  firstRunBanner: {
    backgroundColor: colors.errorBg,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.errorBorder,
  },
  firstRunInline: {
    alignSelf: "stretch",
    maxWidth: 400,
    backgroundColor: colors.errorBg,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
  },
  firstRunRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  firstRunCopy: { flex: 1, gap: spacing.xs },
  firstRunText: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
  firstRunAction: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    color: colors.errorText,
  },
  firstRunDetail: {
    marginTop: spacing.xs,
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.errorText,
    opacity: 0.8,
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
    fontFamily: fonts.sansMedium,
    fontSize: 11,
    lineHeight: 15,
    letterSpacing: 0.5,
    color: colors.mute,
  },
  idea: {
    // The registered italic FACE, not `fonts.lora` + `fontStyle: "italic"`:
    // RN matches a PostScript family name exactly, so asking a
    // regular-only family to slant is a no-op on iOS (same reason the
    // marketplace listing's `vibe` tagline uses this token).
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
    fontFamily: fonts.sans,
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
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.forest,
  },
  timeoutButtonMutedText: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 18,
    color: colors.mute,
  },
  // Same muted treatment as the gate's interstitial sign-out — a deliberate
  // last resort, never competing with Retry.
  signOutButton: {
    alignSelf: "center",
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.xs,
  },
  signOutText: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 17,
    color: colors.mute,
  },
  pressed: { opacity: 0.6 },
});
