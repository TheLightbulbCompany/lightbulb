import { useAuth, useOrganization } from "@clerk/clerk-expo";
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
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { ApiError, createApiClient } from "@lightbulb/shared";

import { usePulse } from "../../../components/chat/usePulse";
import { WEB_ORIGIN } from "../../../config/domain";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import { makeAdapter } from "../../../lib/adapter";
import { LogoMark } from "../../../components/LogoMark";

// Adding / removing an MCP server is a HIGH-risk config mutation (arbitrary
// command + env, gateway restart). v1 is read-only; management deep-links out
// to the web control panel.
const MANAGE_INTEGRATIONS_URL = `${WEB_ORIGIN}/chat?panel=integrations`;

// ---------------------------------------------------------------------------
// Contract shapes (w3-control-contract §8 Integrations — MCP servers)
//
// GET /integrations/mcp/servers → {servers: Record<name, {command, args?, env?}>}.
// 403 for non-admin org members (entries carry env secrets only the admin set
// up) — render a restricted state, never fabricate rows.
// ---------------------------------------------------------------------------

interface McpServer {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
}

interface ServersResponse {
  servers?: Record<string, McpServer>;
}

type State =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  /** 403 — MCP config managed by the org admin. */
  | { kind: "restricted" }
  | { kind: "ready"; servers: Record<string, McpServer> };

// ---------------------------------------------------------------------------
// REST hook — MCP servers, refetched on focus. getTokenRef pins Clerk's
// churning getToken so `load` stays stable (settings.tsx lesson: a [getToken]
// dep is an infinite-refetch loop).
// ---------------------------------------------------------------------------

function useMcpServers(orgRestricted: boolean) {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );
  const orgRestrictedRef = useRef(orgRestricted);
  orgRestrictedRef.current = orgRestricted;

  const [state, setState] = useState<State>({ kind: "loading" });
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = loadSeq.current + 1;
    loadSeq.current = seq;
    // Client-side org gate: a non-admin org member only gets 403s back.
    if (orgRestrictedRef.current) {
      setState({ kind: "restricted" });
      return;
    }
    setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
    try {
      const resp = (await api.get("/integrations/mcp/servers")) as ServersResponse;
      if (loadSeq.current !== seq) return;
      setState({ kind: "ready", servers: resp.servers ?? {} });
    } catch (err) {
      if (loadSeq.current !== seq) return;
      if (err instanceof ApiError && err.status === 403) {
        setState({ kind: "restricted" });
      } else {
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : "Request failed",
        });
      }
    }
  }, [api]);

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

function ManageLink({ label }: { label: string }) {
  return (
    <Pressable
      style={({ pressed }) => [styles.linkRow, pressed && styles.linkRowPressed]}
      onPress={() => void Linking.openURL(MANAGE_INTEGRATIONS_URL)}
      accessibilityRole="link"
    >
      <Text style={styles.linkText}>{label}</Text>
    </Pressable>
  );
}

function SkeletonCards() {
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeletonGroup}>
      {[0, 1].map((i) => (
        <Animated.View key={i} style={[styles.skeletonCard, { opacity: pulse }]} />
      ))}
    </View>
  );
}

/** Compact one-line summary of the launch command + args. */
function commandSummary(server: McpServer): string {
  const cmd = server.command?.trim();
  if (!cmd) return "No command configured";
  const args = Array.isArray(server.args) ? server.args.join(" ") : "";
  return args ? `${cmd} ${args}` : cmd;
}

function ServerCard({ name, server }: { name: string; server: McpServer }) {
  const envKeys = server.env ? Object.keys(server.env) : [];
  const configured = !!server.command?.trim();
  return (
    <View style={styles.card}>
      <View style={styles.cardHead}>
        <View
          style={[
            styles.statusDot,
            { backgroundColor: configured ? colors.runningGreen : colors.muteSoft },
          ]}
        />
        <Text style={styles.cardTitle} numberOfLines={1}>
          {name}
        </Text>
        <Text style={styles.statusLabel}>
          {configured ? "Configured" : "Incomplete"}
        </Text>
      </View>
      <Text style={styles.commandLine} numberOfLines={2}>
        {commandSummary(server)}
      </Text>
      {envKeys.length > 0 && (
        <Text style={styles.metaLine}>
          {envKeys.length} env {envKeys.length === 1 ? "variable" : "variables"}
        </Text>
      )}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function IntegrationsPanel() {
  const { organization, membership } = useOrganization();
  const orgRestricted =
    organization != null && membership != null && membership.role !== "org:admin";
  const { state, retry } = useMcpServers(orgRestricted);

  const restricted = orgRestricted || state.kind === "restricted";

  const names = useMemo(
    () => (state.kind === "ready" ? Object.keys(state.servers).sort() : []),
    [state],
  );

  let inner: ReactNode;
  if (restricted) {
    inner = (
      <View style={styles.card}>
        <Text style={styles.overline}>MCP SERVERS</Text>
        <Text style={styles.bodyText}>
          MCP servers are managed by your organization admin
        </Text>
      </View>
    );
  } else if (state.kind === "loading") {
    inner = <SkeletonCards />;
  } else if (state.kind === "error") {
    inner = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load integrations: {state.message}
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
  } else if (names.length === 0) {
    inner = (
      <View style={styles.emptyWrap}>
        <LogoMark size={48} />
        <Text style={styles.dim}>No MCP servers connected.</Text>
        <Text style={styles.dimSmall}>
          Add Model Context Protocol servers on flickmeon.ai.
        </Text>
        <ManageLink label="Manage on flickmeon.ai" />
      </View>
    );
  } else {
    const servers = (state as { servers: Record<string, McpServer> }).servers;
    inner = (
      <>
        <View style={styles.section}>
          <Text style={styles.sectionLabel}>
            MCP SERVERS
            <Text style={styles.sectionCount}> · {names.length}</Text>
          </Text>
          {names.map((name) => (
            <ServerCard key={name} name={name} server={servers[name]} />
          ))}
        </View>
        <View style={styles.noteCard}>
          <Text style={styles.captionText}>
            Adding or removing servers is read-only here. Manage integrations on
            flickmeon.ai.
          </Text>
          <ManageLink label="Manage on flickmeon.ai" />
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
      {inner}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: {
    padding: spacing.lg,
    gap: spacing.md,
    paddingBottom: spacing.xl,
    flexGrow: 1,
  },

  overline: { ...type.overline },
  bodyText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
  },
  captionText: { ...type.caption },

  section: { gap: spacing.sm },
  sectionLabel: { ...type.overline, paddingHorizontal: spacing.xs },
  sectionCount: { color: colors.sectionCount },

  // Server card.
  card: {
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    padding: spacing.lg,
    gap: spacing.sm,
  },
  cardHead: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
  },
  statusDot: { width: 8, height: 8, borderRadius: 4 },
  cardTitle: {
    flex: 1,
    fontFamily: fonts.sansMedium,
    fontSize: 14,
    lineHeight: 18,
    color: colors.ink,
  },
  statusLabel: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 0.6,
    color: colors.mute,
  },
  commandLine: {
    fontFamily: fonts.mono,
    fontSize: 11,
    lineHeight: 16,
    color: colors.inkSoft,
  },
  metaLine: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },

  // Read-only note card.
  noteCard: {
    backgroundColor: colors.creamDeeper,
    borderRadius: radii.md,
    padding: spacing.lg,
    gap: spacing.xs,
  },

  // Deep-link-out row.
  linkRow: {
    marginTop: spacing.xs,
    alignSelf: "flex-start",
    borderRadius: radii.xs,
  },
  linkRowPressed: { opacity: 0.6 },
  linkText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },

  // Empty state (spec §7).
  emptyWrap: {
    flexGrow: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.sm,
    padding: spacing.xl,
  },
  dim: { ...type.body, color: colors.mute, textAlign: "center" },
  dimSmall: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
    textAlign: "center",
  },

  // Skeletons (spec §7).
  skeletonGroup: { gap: spacing.md },
  skeletonCard: {
    height: 96,
    borderRadius: radii.lg,
    backgroundColor: colors.skeleton,
  },

  // Error card (spec §4).
  errorCard: {
    padding: spacing.lg,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    gap: spacing.md,
  },
  errorText: {
    fontFamily: fonts.sans,
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
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
});
