import { useAuth } from "@clerk/clerk-expo";
import { useFocusEffect } from "expo-router";
import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Animated,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  useColorScheme,
  View,
} from "react-native";
import {
  ApiError,
  createApiClient,
  fetchJsonWithStatus,
  useAgents,
  useStatusLoaded,
  useRuntimeRpc,
} from "@lightbulb/shared";

import { usePulse } from "../../../components/chat/usePulse";
import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../../config/theme";
import { makeAdapter } from "../../../lib/adapter";
import { useLiveEvents } from "../../../providers/live-events";

// ---------------------------------------------------------------------------
// Contract shapes (w3-control-contract §1 Overview)
// ---------------------------------------------------------------------------

/** `health` RPC payload — may arrive bare or wrapped in an event envelope.
 *  The Hermes bridge sends `uptimeS` and a plain agent count; an agents ARRAY
 *  is still accepted. */
interface HealthPayload {
  ok?: boolean;
  ts?: number;
  uptimeS?: number;
  agents?: unknown[] | number;
}

/** Subset of `GET /billing/account` used by the Overview tiles. */
interface BillingAccount {
  current_spend: number;
  subscription_status: string | null;
}

/** Normalized `GET /container/status` result (status-code protocol, §1). */
type ContainerState =
  | { code: "ready"; status?: string; substatus?: string }
  | { code: "team_pending" }
  | { code: "blocked"; title: string; message: string | null }
  | { code: "needs_signup" };

type RestState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ready"; container: ContainerState; billing: BillingAccount | null };

// ---------------------------------------------------------------------------
// Normalizers
// ---------------------------------------------------------------------------

/** health RPC arrives as `{type:"event", payload}` OR bare payload (§1). */
function extractHealth(raw: unknown): HealthPayload | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const obj = raw as Record<string, unknown>;
  if (obj.type === "event" && obj.payload && typeof obj.payload === "object") {
    return obj.payload as HealthPayload;
  }
  return obj as HealthPayload;
}

function formatUptime(ms: number): string {
  if (ms <= 0) return "—";
  const totalSeconds = Math.floor(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const totalMinutes = Math.floor(totalSeconds / 60);
  if (totalMinutes < 60) return `${totalMinutes}m`;
  const totalHours = Math.floor(totalMinutes / 60);
  if (totalHours < 24) return `${totalHours}h ${totalMinutes % 60}m`;
  const days = Math.floor(totalHours / 24);
  return `${days}d ${totalHours % 24}h`;
}

export function planLabel(status: string | null): string {
  if (!status) return "None";
  if (status === "trialing") return "Trial";
  if (status === "trial_local") return "Free";
  if (status === "active") return "Active";
  return status;
}

// ---------------------------------------------------------------------------
// Derived connection status (lean port of useSystemHealth priority order, §1)
// ---------------------------------------------------------------------------

type Tone = "connected" | "starting" | "down";

interface DerivedStatus {
  tone: Tone;
  label: string;
  detail: string | null;
}

function deriveStatus(
  container: ContainerState,
  health: HealthPayload | undefined,
): DerivedStatus {
  if (container.code === "needs_signup") {
    return { tone: "down", label: "Not set up", detail: "Finish setup on flickmeon.ai" };
  }
  if (container.code === "blocked") {
    return { tone: "down", label: container.title, detail: container.message };
  }
  if (container.code === "team_pending") {
    return { tone: "starting", label: "Provisioning…", detail: "Setting up your workspace" };
  }
  // code === "ready": inspect the container.status field
  const status = container.status;
  if (status === "provisioning") {
    return { tone: "starting", label: "Starting…", detail: container.substatus ?? null };
  }
  if (status === "stopped" || status === "error") {
    return { tone: "down", label: "Container down", detail: container.substatus ?? null };
  }
  // running (or unknown-but-present): the runtime health response decides liveness.
  if (!health) {
    return { tone: "starting", label: "Checking agent runtime…", detail: null };
  }
  if (health.ok === false) {
    return { tone: "down", label: "Gateway degraded", detail: "Agent runtime not ready" };
  }
  const uptimeMs =
    health?.uptimeS != null ? health.uptimeS * 1000
    : health?.ts != null ? Date.now() - health.ts
    : null;
  return {
    tone: "connected",
    label: "Healthy",
    detail: uptimeMs != null ? `Up ${formatUptime(uptimeMs)}` : null,
  };
}

function dotColor(tone: Tone, p: ChatPalette): string {
  if (tone === "connected") return p.live;
  if (tone === "starting") return p.warn;
  return p.alert;
}

// ---------------------------------------------------------------------------
// REST hook — container status + billing account, refetched on focus.
// getTokenRef pins Clerk's churning getToken so `load` stays stable and
// useFocusEffect fires once per focus (settings.tsx lesson).
// ---------------------------------------------------------------------------

function useOverviewRest() {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  // One config powers both the status-aware fetch and the billing GET.
  const config = useMemo(
    () => makeAdapter(() => getTokenRef.current()),
    [],
  );
  const api = useMemo(() => createApiClient(config), [config]);

  const [state, setState] = useState<RestState>({ kind: "loading" });
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = loadSeq.current + 1;
    loadSeq.current = seq;
    setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
    try {
      let container: ContainerState;
      try {
        const { status, body } = await fetchJsonWithStatus(
          config,
          "/container/status",
        );
        if (status === 202) {
          container = { code: "team_pending" };
        } else {
          const info = (body ?? {}) as { status?: string; substatus?: string };
          container = {
            code: "ready",
            status: info.status,
            substatus: info.substatus,
          };
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 402) {
          const blocked = (err.body as { detail?: { blocked?: unknown } } | null)
            ?.detail?.blocked as
            | { title?: string; message?: string }
            | undefined;
          container = {
            code: "blocked",
            title: blocked?.title ?? "Action needed",
            message: blocked?.message ?? null,
          };
        } else if (err instanceof ApiError && err.status === 404) {
          container = { code: "needs_signup" };
        } else {
          throw err;
        }
      }

      // Billing is best-effort: 404 = pre-subscribe, 403 = org-managed. Neither
      // fails the panel — the Plan/Spend tiles just render "—".
      const billing = (await api
        .get("/billing/account")
        .then((body) => body as BillingAccount)
        .catch(() => null)) as BillingAccount | null;

      if (loadSeq.current !== seq) return;
      setState({ kind: "ready", container, billing });
    } catch (err) {
      if (loadSeq.current !== seq) return;
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : "Request failed",
      });
    }
  }, [api, config]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  return { state, retry: load };
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

function StatusDot({ tone, pulse }: { tone: Tone; pulse: boolean }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const opacity = usePulse(pulse, 0.35);
  return (
    <Animated.View
      style={[
        styles.statusDot,
        { backgroundColor: dotColor(tone, palette), opacity },
      ]}
    />
  );
}

/** 2-col tile: overline label + big value (or "—" placeholder). */
function Tile({
  label,
  value,
  loading,
}: {
  label: string;
  value: string;
  loading?: boolean;
}) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return (
    <View style={styles.tile}>
      <Text style={styles.tileLabel}>{label}</Text>
      {loading ? (
        <TilePulse />
      ) : (
        <Text style={styles.tileValue} numberOfLines={1}>
          {value}
        </Text>
      )}
    </View>
  );
}

function TilePulse() {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const opacity = usePulse(true, 0.55);
  return <Animated.View style={[styles.tileSkeleton, { opacity }]} />;
}

function StatusCardSkeleton() {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const opacity = usePulse(true, 0.55);
  return (
    <View style={styles.statusCard}>
      <Animated.View style={[styles.statusSkeletonDot, { opacity }]} />
      <View style={styles.statusText}>
        <Animated.View style={[styles.statusSkeletonLine, { opacity, width: "45%" }]} />
        <Animated.View style={[styles.statusSkeletonLine, { opacity, width: "70%" }]} />
      </View>
    </View>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function Overview() {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const live = useLiveEvents();
  const { state, retry } = useOverviewRest();

  // Runtime counts use canonical HTTP RPC and AppSync invalidations.
  const { agents, isSettled: agentsSettled } = useAgents(live);
  // Held until the runtime is DEFINITE — `undefined` is "not known for this
  // workspace", and guessing costs a microVM start on AgentCore.
  const statusLoaded = useStatusLoaded();
  const { data: healthRaw } = useRuntimeRpc<unknown>(
    live,
    statusLoaded ? "health" : null,
    undefined,
    { refreshInterval: 10_000 },
  );
  const health = extractHealth(healthRaw);

  const agentCount = agentsSettled ? agents.length : undefined;

  let body: ReactNode;
  if (state.kind === "loading") {
    body = (
      <>
        <StatusCardSkeleton />
        <View style={styles.grid}>
          <Tile label="AGENTS" value="" loading />
          <Tile label="SESSIONS" value="" loading />
          <Tile label="PLAN" value="" loading />
          <Tile label="SPEND" value="" loading />
        </View>
      </>
    );
  } else if (state.kind === "error") {
    body = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load overview: {state.message}
        </Text>
        <Pressable
          style={({ pressed }) => [
            styles.retryButton,
            pressed && styles.retryButtonPressed,
          ]}
          onPress={() => void retry()}
          accessibilityRole="button"
          accessibilityLabel="Retry"
        >
          <Text style={styles.retryText}>Retry</Text>
        </Pressable>
      </View>
    );
  } else {
    const status = deriveStatus(state.container, health);
    const billing = state.billing;
    body = (
      <>
        <View style={styles.statusCard}>
          <StatusDot tone={status.tone} pulse={status.tone === "starting"} />
          <View style={styles.statusText}>
            <Text
              style={[
                styles.statusLabel,
                status.tone === "down" && styles.statusLabelDown,
              ]}
            >
              {status.label}
            </Text>
            {/* Never clipped: when the workspace is blocked this card is the
                only place that says why, so it has to survive a large text
                size. The panel scrolls, so a taller card costs nothing. */}
            {status.detail != null && <Text style={styles.statusDetail}>{status.detail}</Text>}
          </View>
        </View>

        <View style={styles.grid}>
          <Tile
            label="AGENTS"
            value={agentCount != null ? String(agentCount) : "—"}
            loading={agentCount == null && !agentsSettled}
          />
          <Tile
            label="PLAN"
            value={planLabel(billing?.subscription_status ?? null)}
          />
          <Tile
            label="SPEND"
            value={
              billing != null ? `$${(billing.current_spend ?? 0).toFixed(2)}` : "—"
            }
          />
        </View>
      </>
    );
  }

  return (
    <ScrollView
      contentInsetAdjustmentBehavior="automatic"
      style={styles.page}
      contentContainerStyle={styles.content}
    >
      {body}
    </ScrollView>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },
    content: { padding: spacing.lg, gap: spacing.md, paddingBottom: spacing.xl },

    // Status card (full-width; spec §5 HealthIndicator adapted to a card).
    statusCard: {
      flexDirection: "row",
      // The dot stays beside the label's first line once the text wraps.
      alignItems: "flex-start",
      gap: spacing.md,
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.lg,
    },
    // `marginTop` centres it on the label's first line (20pt leading, 10pt dot).
    statusDot: { width: 10, height: 10, borderRadius: 5, marginTop: 5 },
    statusText: { flex: 1, gap: 2 },
    statusLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 15,
      lineHeight: 20,
      color: p.fg,
    },
    statusLabelDown: { color: p.alert },
    statusDetail: {
      fontFamily: fonts.sans,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
    },
    statusSkeletonDot: {
      width: 10,
      height: 10,
      borderRadius: 5,
      backgroundColor: p.raised,
    },
    statusSkeletonLine: {
      height: 12,
      borderRadius: radii.xs,
      backgroundColor: p.raised,
    },

    // 2-col tile grid.
    grid: {
      flexDirection: "row",
      flexWrap: "wrap",
      gap: spacing.md,
    },
    tile: {
      flexGrow: 1,
      flexBasis: "47%",
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.lg,
      gap: spacing.sm,
    },
    tileLabel: {
      fontFamily: fonts.mono,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.4,
      color: p.fgMute,
    },
    tileValue: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 20,
      lineHeight: 26,
      color: p.fg,
    },
    tileSkeleton: {
      height: 22,
      width: "55%",
      borderRadius: radii.xs,
      backgroundColor: p.raised,
    },

    // Error card (spec §4 trio).
    errorCard: {
      padding: spacing.lg,
      backgroundColor: p.raised,
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.md,
      gap: spacing.md,
    },
    errorText: {
      fontFamily: fonts.sans,
      fontSize: 14,
      lineHeight: 20,
      color: p.alert,
    },
    retryButton: {
      alignSelf: "flex-start",
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.sm,
      backgroundColor: p.surface,
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.xs,
    },
    retryButtonPressed: { opacity: 0.6 },
    retryText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.alert,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
