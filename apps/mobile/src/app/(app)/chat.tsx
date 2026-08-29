import { useUser } from "@clerk/clerk-expo";
import Feather from "@expo/vector-icons/Feather";
import { router } from "expo-router";
import { type ReactElement, useEffect, useRef, useState } from "react";
import {
  Animated,
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { agentDisplayName, useAgents } from "@lightbulb/shared";

import { colors, fonts, radii, spacing, type } from "../../config/theme";
import { AgentPickerSheet } from "../../components/chat/AgentPickerSheet";
import { SessionsSheet } from "../../components/chat/SessionsSheet";
import { usePulse } from "../../components/chat/usePulse";
import { readFirstRun } from "../../lib/firstRun";
import { useContainerStatusContext } from "../../lib/containerStatusContext";
import { isContainerComingUp } from "../../lib/rosterPresentation";
import { useGateway } from "../../providers/gateway";
import { LogoMark } from "../../components/LogoMark";

// Slim connection-state row above the roster (web HealthIndicator, spec §5).
function HealthRow({
  isConnected,
  errorMessage,
}: {
  isConnected: boolean;
  errorMessage: string | null;
}) {
  const state = isConnected ? "connected" : errorMessage ? "down" : "connecting";
  const pulse = usePulse(state === "connecting", 0.35);
  const dotColor =
    state === "connected"
      ? colors.runningGreen
      : state === "down"
        ? colors.danger
        : colors.connectingYellow;
  const label =
    state === "connected"
      ? "Connected"
      : state === "down"
        ? (errorMessage ?? "Disconnected")
        : "Connecting…";
  return (
    <View style={styles.healthRow}>
      <Animated.View
        style={[styles.healthDot, { backgroundColor: dotColor, opacity: pulse }]}
      />
      <Text
        style={[styles.healthLabel, state === "down" && styles.healthLabelDown]}
        numberOfLines={1}
      >
        {label}
      </Text>
    </View>
  );
}

// Tinted avatar tile — Lora italic-bold initial, or the agent's emoji inside
// the same tile when one is set (spec §5 Monogram). `big` = 44px grid variant.
function Monogram({
  name,
  emoji,
  big,
}: {
  name: string;
  emoji?: string;
  big?: boolean;
}) {
  return (
    <View style={[styles.monogram, big && styles.monogramBig]}>
      {emoji ? (
        <Text style={big ? styles.monogramEmojiBig : styles.monogramEmoji}>
          {emoji}
        </Text>
      ) : (
        <Text style={big ? styles.monogramLetterBig : styles.monogramLetter}>
          {(name.trim()[0] ?? "L").toUpperCase()}
        </Text>
      )}
    </View>
  );
}

// Four pulsing placeholder tiles (2×2) while agents.list is in flight (§7).
// `warming` adds a line naming what we're waiting on, for the one case where
// the wait is long enough to look like a hang: a container waking from zero.
function SkeletonGrid({ warming }: { warming?: boolean }) {
  const pulse = usePulse(true, 0.55);
  return (
    <View>
      {warming ? (
        <Text style={styles.warmingLabel}>Waking your agents…</Text>
      ) : null}
      <View style={styles.skeletonGrid}>
        {[0, 1, 2, 3].map((i) => (
          <Animated.View
            key={i}
            style={[styles.skeletonTile, { opacity: pulse }]}
          />
        ))}
      </View>
    </View>
  );
}

// Always carries a Retry: a card with no way out is only marginally better
// than the spinner it replaced, and this is the surface a first-time user
// lands on.
function ErrorCard({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <View style={styles.errorCard}>
      <Text style={styles.errorText}>Couldn’t load agents: {message}</Text>
      <Pressable
        onPress={onRetry}
        accessibilityRole="button"
        accessibilityLabel="Retry loading agents"
      >
        <Text style={styles.retryText}>Retry</Text>
      </Pressable>
    </View>
  );
}

// Agent roster — a 2-column grid of agent tiles, plus a "New task" entry that
// starts a fresh session with a picked agent. Reads the roster through the
// SHARED useAgents hook (the same one the web sidebar uses), which carries the
// onboarding empty-roster latch fix and revalidation on container-reachable —
// neither of which the raw agents.list call this replaced had.
export default function Chat() {
  const gateway = useGateway();
  const { agents, defaultId, error, isLoading, isSettled, refresh } =
    useAgents(gateway);
  const containerStatus = useContainerStatusContext();
  const connectionDown = !gateway.isConnected && gateway.error != null;
  // Is a missing/failed roster a wake or a failure? Ask the server, don't
  // infer it from the RPC error (see isContainerComingUp). `warming` also
  // covers the NO-error case: a first-run owner's agents.list resolves to
  // undefined-as-success, so there is nothing to classify and the screen
  // would otherwise skeleton silently forever.
  const warming = isContainerComingUp(containerStatus);
  const hardError = error && !warming ? error : null;

  // `hardError` suppresses it: the error card is the answer, and rendering
  // "No agents yet." beside it tells the user two contradictory things.
  const showEmpty = agents.length === 0 && !isLoading && isSettled && !hardError;
  const [pickerOpen, setPickerOpen] = useState(false);
  // Long-pressing a tile opens that agent's session switcher right from the
  // roster. `sessionsAgent` stays set after close so the sheet can animate out
  // (unmounting it mid-close would cut the exit animation); `sessionsOpen`
  // drives visibility.
  const [sessionsAgent, setSessionsAgent] = useState<{
    id: string;
    name: string;
  } | null>(null);
  const [sessionsOpen, setSessionsOpen] = useState(false);

  // First-run aha: right after the funnel brief is submitted, walk the user
  // straight into the default agent's (Friday's) chat so they land on the
  // live kickoff the backend server-dispatches (spec PR-6, BOOTSTRAP) rather
  // than the roster. Web needs no redirect (its main surface IS the
  // default-agent chat); the roster is the mobile equivalent's front door.
  //
  // Gated on the DURABLE server-owned `onboarded` fact (funnel_state ==
  // "complete", surfaced on every ready status body), NOT on `briefSubmitted`
  // alone. `briefSubmitted` is a permanently-sticky Clerk flag that is never
  // reset, and `redirectedRef` only survives a single mount -- so gating on
  // those two would re-yank a user who onboarded weeks ago into Friday on
  // every roster mount (cold start, relaunch, any remount). Once the backend
  // reports `onboarded`, the first-run window is over and we stand down.
  const { user } = useUser();
  const onboarded =
    containerStatus?.kind === "ready" && containerStatus.onboarded === true;
  const redirectedRef = useRef(false);
  useEffect(() => {
    if (redirectedRef.current || !user || !defaultId) return;
    const fr = readFirstRun(user);
    if (!fr.briefSubmitted || onboarded) return;
    const agent = agents.find((a) => a.id === defaultId);
    if (!agent) return;
    redirectedRef.current = true;
    router.push({
      pathname: "/(app)/agent/[agentId]",
      params: { agentId: agent.id, name: agentDisplayName(agent) },
    });
  }, [user, agents, defaultId, onboarded]);

  let body: ReactElement;
  if (hardError && !isSettled) {
    body = <ErrorCard message={hardError.message} onRetry={() => void refresh()} />;
  } else if (!isSettled && connectionDown) {
    // No cached roster and the socket is down — the health row carries the
    // detail; keep the body quiet rather than pulsing skeletons forever.
    body = (
      <View style={styles.centerGrow}>
        <Text style={styles.dim}>{gateway.error}</Text>
      </View>
    );
  } else {
    body = (
      <FlatList
        data={agents}
        keyExtractor={(a) => a.id}
        numColumns={2}
        columnWrapperStyle={styles.column}
        style={styles.list}
        contentContainerStyle={[
          styles.listContent,
          showEmpty && styles.centerGrow,
        ]}
        ListHeaderComponent={
          <>
            {/* A hard failure over a cached roster reports itself above the
                tiles rather than replacing them — the agents we already
                fetched are still the truest thing we can show. */}
            {hardError ? (
              <ErrorCard
                message={hardError.message}
                onRetry={() => void refresh()}
              />
            ) : null}
            {agents.length > 0 ? (
              <Text style={styles.sectionLabel}>
                AGENTS
                <Text style={styles.sectionCount}> · {agents.length}</Text>
              </Text>
            ) : null}
          </>
        }
        ListEmptyComponent={
          hardError ? null : isLoading || !isSettled ? (
            <SkeletonGrid warming={warming} />
          ) : (
            <View style={styles.emptyWrap}>
              <LogoMark size={48} />
              <Text style={styles.dim}>No agents yet.</Text>
            </View>
          )
        }
        renderItem={({ item }) => (
          <Pressable
            style={({ pressed }) => [styles.tile, pressed && styles.tilePressed]}
            onPress={() =>
              router.push({
                pathname: "/(app)/agent/[agentId]",
                params: { agentId: item.id, name: agentDisplayName(item) },
              })
            }
            onLongPress={() => {
              setSessionsAgent({ id: item.id, name: agentDisplayName(item) });
              setSessionsOpen(true);
            }}
            accessibilityRole="button"
            accessibilityLabel={agentDisplayName(item)}
          >
            <Monogram
              name={agentDisplayName(item)}
              emoji={item.identity?.emoji ?? undefined}
              big
            />
            <Text style={styles.tileName} numberOfLines={2}>
              {agentDisplayName(item)}
            </Text>
          </Pressable>
        )}
      />
    );
  }

  return (
    <View style={styles.page}>
      <HealthRow isConnected={gateway.isConnected} errorMessage={gateway.error} />
      {agents.length > 0 ? (
        <Pressable
          style={({ pressed }) => [styles.newTask, pressed && styles.newTaskPressed]}
          onPress={() => setPickerOpen(true)}
          accessibilityRole="button"
          accessibilityLabel="New task"
        >
          <Feather name="plus" size={16} color={colors.cream} />
          <Text style={styles.newTaskLabel}>New task</Text>
        </Pressable>
      ) : null}
      {body}
      <AgentPickerSheet
        visible={pickerOpen}
        agents={agents}
        onClose={() => setPickerOpen(false)}
        onPick={(agent) => {
          setPickerOpen(false);
          router.push({
            pathname: "/(app)/agent/[agentId]",
            params: {
              agentId: agent.id,
              name: agentDisplayName(agent),
              newSession: "1",
            },
          });
        }}
      />
      {sessionsAgent ? (
        <SessionsSheet
          visible={sessionsOpen}
          onClose={() => setSessionsOpen(false)}
          agentId={sessionsAgent.id}
          userId={user?.id ?? null}
          // The roster has no active session — General reads as the default.
          activeSessionId={null}
          onSelect={(sessionId, label) => {
            setSessionsOpen(false);
            router.push({
              pathname: "/(app)/agent/[agentId]",
              params: {
                agentId: sessionsAgent.id,
                name: sessionsAgent.name,
                // A named session carries its id + label into the chat screen;
                // General (null) navigates with neither so the screen defaults.
                ...(sessionId ? { sessionId, sessionLabel: label ?? "" } : {}),
              },
            });
          }}
          onNewSession={() => {
            setSessionsOpen(false);
            router.push({
              pathname: "/(app)/agent/[agentId]",
              params: {
                agentId: sessionsAgent.id,
                name: sessionsAgent.name,
                newSession: "1",
              },
            });
          }}
        />
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  list: { flex: 1 },
  listContent: {
    paddingHorizontal: spacing.md,
    paddingBottom: spacing.xl,
  },
  column: { gap: spacing.sm },
  centerGrow: {
    flexGrow: 1,
    alignItems: "center",
    justifyContent: "center",
    padding: spacing.xl,
  },
  dim: { ...type.body, color: colors.mute, textAlign: "center" },

  // Connection state (spec §5 HealthIndicator).
  healthRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
  },
  healthDot: { width: 8, height: 8, borderRadius: 4 },
  healthLabel: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
    flexShrink: 1,
  },
  healthLabelDown: { color: colors.danger },

  // "New task" primary action (start a fresh session with a picked agent).
  newTask: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.xs,
    marginHorizontal: spacing.md,
    marginBottom: spacing.sm,
    paddingVertical: spacing.sm,
    borderRadius: radii.md,
    backgroundColor: colors.forest,
  },
  newTaskPressed: { opacity: 0.85 },
  newTaskLabel: {
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.cream,
  },

  // Section label (spec §5).
  sectionLabel: {
    ...type.overline,
    paddingHorizontal: spacing.sm,
    marginBottom: spacing.sm,
  },
  sectionCount: { color: colors.sectionCount },

  // Agent tiles (2-column grid).
  tile: {
    flex: 1,
    minHeight: 120,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.sm,
    paddingVertical: spacing.lg,
    paddingHorizontal: spacing.sm,
    backgroundColor: colors.cream,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.lg,
    marginBottom: spacing.sm,
  },
  tilePressed: { backgroundColor: colors.creamPressed },
  monogram: {
    width: 28,
    height: 28,
    borderRadius: radii.md,
    backgroundColor: colors.forestTint,
    alignItems: "center",
    justifyContent: "center",
  },
  monogramBig: { width: 44, height: 44, borderRadius: radii.lg },
  monogramLetter: {
    fontFamily: fonts.loraBoldItalic,
    fontSize: 14,
    color: colors.forest,
  },
  monogramLetterBig: {
    fontFamily: fonts.loraBoldItalic,
    fontSize: 20,
    color: colors.forest,
  },
  monogramEmoji: { fontSize: 14 },
  monogramEmojiBig: { fontSize: 22 },
  tileName: {
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.ink,
    textAlign: "center",
  },
  // Empty state (spec §7).
  emptyWrap: { alignItems: "center", gap: spacing.sm },

  // Loading skeletons — 2×2 tile grid (spec §7).
  warmingLabel: {
    ...type.body,
    color: colors.mute,
    textAlign: "center",
    marginBottom: spacing.md,
  },
  skeletonGrid: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: spacing.sm,
  },
  skeletonTile: {
    flexGrow: 1,
    flexBasis: "45%",
    minHeight: 120,
    borderRadius: radii.lg,
    backgroundColor: colors.skeleton,
  },

  // Error card (spec §4).
  errorCard: {
    margin: spacing.md,
    padding: spacing.lg,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
  },
  retryText: {
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 20,
    color: colors.forest,
    marginTop: spacing.sm,
  },
  errorText: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
});
