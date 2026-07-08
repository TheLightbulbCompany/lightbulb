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
  View,
} from "react-native";
import {
  ApiError,
  createApiClient,
  fetchJsonWithStatus,
  useGatewayRpc,
  type AgentsListResponse,
} from "@isol8/shared";

import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import { makeAdapter } from "../../../lib/adapter";
import { useGateway } from "../../../providers/gateway";

// ---------------------------------------------------------------------------
// Contract shapes (w3-control-contract §1 Overview)
// ---------------------------------------------------------------------------

/** `health` RPC payload — may arrive bare or wrapped in an event envelope. */
interface HealthPayload {
  ok?: boolean;
  ts?: number;
  sessions?: { count?: number };
  agents?: unknown[];
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

function planLabel(status: string | null): string {
  if (!status) return "None";
  if (status === "trialing") return "Trial";
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
  gatewayConnected: boolean,
  gatewayError: string | null,
  health: HealthPayload | undefined,
): DerivedStatus {
  if (container.code === "needs_signup") {
    return { tone: "down", label: "Not set up", detail: "Finish setup on isol8.co" };
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
  // running (or unknown-but-present): the socket + health decide liveness
  if (!gatewayConnected) {
    return gatewayError
      ? { tone: "down", label: "Gateway down", detail: gatewayError }
      : { tone: "starting", label: "Connecting…", detail: null };
  }
  if (health && health.ok === false) {
    return { tone: "down", label: "Gateway degraded", detail: "Agent runtime not ready" };
  }
  const uptime =
    health?.ts != null ? `Up ${formatUptime(Date.now() - health.ts)}` : null;
  return { tone: "connected", label: "Healthy", detail: uptime };
}

function dotColor(tone: Tone): string {
  if (tone === "connected") return colors.runningGreen;
  if (tone === "starting") return colors.connectingYellow;
  return colors.danger;
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
  const opacity = usePulse(pulse, 0.35);
  return (
    <Animated.View
      style={[
        styles.statusDot,
        { backgroundColor: dotColor(tone), opacity },
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
  const opacity = usePulse(true, 0.55);
  return <Animated.View style={[styles.tileSkeleton, { opacity }]} />;
}

function StatusCardSkeleton() {
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
  const gateway = useGateway();
  const { state, retry } = useOverviewRest();

  // Live counts over the same RPCs the web Overview polls.
  const { data: agentsData } = useGatewayRpc<AgentsListResponse>(
    gateway,
    "agents.list",
  );
  const { data: healthRaw } = useGatewayRpc<unknown>(
    gateway,
    "health",
    undefined,
    { refreshInterval: 10_000 },
  );
  const health = extractHealth(healthRaw);

  const agentCount = agentsData?.agents?.length;
  const sessionCount = health?.sessions?.count;

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
    const status = deriveStatus(
      state.container,
      gateway.isConnected,
      gateway.error,
      health,
    );
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
              numberOfLines={1}
            >
              {status.label}
            </Text>
            {status.detail != null && (
              <Text style={styles.statusDetail} numberOfLines={2}>
                {status.detail}
              </Text>
            )}
          </View>
        </View>

        <View style={styles.grid}>
          <Tile
            label="AGENTS"
            value={agentCount != null ? String(agentCount) : "—"}
            loading={
              agentCount == null && agentsData == null && gateway.isConnected
            }
          />
          <Tile
            label="SESSIONS"
            value={sessionCount != null ? String(sessionCount) : "—"}
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
    <ScrollView style={styles.page} contentContainerStyle={styles.content}>
      {body}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: { padding: spacing.lg, gap: spacing.md, paddingBottom: spacing.xl },

  // Status card (full-width; spec §5 HealthIndicator adapted to a card).
  statusCard: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
  },
  statusDot: { width: 10, height: 10, borderRadius: 5 },
  statusText: { flex: 1, gap: 2 },
  statusLabel: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
  },
  statusLabelDown: { color: colors.danger },
  statusDetail: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  statusSkeletonDot: {
    width: 10,
    height: 10,
    borderRadius: 5,
    backgroundColor: colors.skeleton,
  },
  statusSkeletonLine: {
    height: 12,
    borderRadius: radii.xs,
    backgroundColor: colors.skeleton,
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
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
    gap: spacing.sm,
  },
  tileLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 1.4,
    color: colors.mute,
  },
  tileValue: { ...type.title },
  tileSkeleton: {
    height: 22,
    width: "55%",
    borderRadius: radii.xs,
    backgroundColor: colors.skeleton,
  },

  // Error card (spec §4 trio).
  errorCard: {
    padding: spacing.lg,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    gap: spacing.md,
  },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.errorText,
  },
  retryButton: {
    alignSelf: "flex-start",
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
  },
  retryButtonPressed: { opacity: 0.6 },
  retryText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
});
