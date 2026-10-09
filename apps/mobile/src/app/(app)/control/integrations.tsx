import { useAuth, useOrganization } from "@clerk/expo";
import { router, useFocusEffect } from "expo-router";
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
  useColorScheme,
  View,
} from "react-native";
import { ApiError, createApiClient } from "@lightbulb/shared";

import { usePulse } from "../../../components/chat/usePulse";
import { WEB_ORIGIN } from "../../../config/domain";
import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../../config/theme";
import { makeAdapter } from "../../../lib/adapter";
import { LogoMark } from "../../../components/LogoMark";

// Adding / removing an MCP server is a HIGH-risk config mutation (arbitrary
// command + env, gateway restart). v1 is read-only; management deep-links out
// to the web control panel.
// from=app: the connect finishes in Safari, so the web asks OAuth to hand the user back to the app.
const MANAGE_INTEGRATIONS_URL = `${WEB_ORIGIN}/chat?panel=integrations&from=app`;

// ---------------------------------------------------------------------------
// Contract shapes (w3-control-contract §8 Integrations — MCP servers)
//
// GET /integrations/mcp/servers → stdio command or remote URL per server.
// 403 for non-admin org members (entries carry env secrets only the admin set
// up) — render a restricted state, never fabricate rows.
// ---------------------------------------------------------------------------

interface McpServer {
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  transport?: string;
  url?: string;
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
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
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
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeletonGroup}>
      {[0, 1].map((i) => (
        <Animated.View key={i} style={[styles.skeletonCard, { opacity: pulse }]} />
      ))}
    </View>
  );
}

/** Show only the remote host: URL paths and query strings can carry credentials. */
function serverSummary(server: McpServer): string {
  const cmd = server.command?.trim();
  if (cmd) {
    const args = Array.isArray(server.args) ? server.args.join(" ") : "";
    return args ? `${cmd} ${args}` : cmd;
  }
  if (!server.url) return "No endpoint configured";
  try {
    const host = new URL(server.url).host;
    return host ? `${server.transport ?? "Remote MCP"} · ${host}` : "Remote MCP server";
  } catch {
    return "Remote MCP server";
  }
}

function ServerCard({ name, server }: { name: string; server: McpServer }) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const envKeys = server.env ? Object.keys(server.env) : [];
  const configured = !!server.command?.trim() || !!server.url?.trim();
  return (
    <View style={styles.card}>
      <View style={styles.cardHead}>
        <View
          style={[
            styles.statusDot,
            { backgroundColor: configured ? palette.live : palette.fgMute },
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
        {serverSummary(server)}
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
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
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
          Add Model Context Protocol servers on thelightbulb.company.
        </Text>
        <ManageLink label="Manage on thelightbulb.company" />
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
            thelightbulb.company.
          </Text>
          <ManageLink label="Manage on thelightbulb.company" />
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
      {/* Org admins only: every website sign-in route 403s for a member. */}
      {restricted ? null : (
        <Pressable
          style={({ pressed }) => [styles.card, pressed && styles.linkRowPressed]}
          onPress={() => router.push("/(app)/control/website-signins")}
          accessibilityRole="button"
          accessibilityLabel="Website sign-ins"
        >
          <Text style={[styles.cardTitle, styles.cardTitleStacked]}>Website sign-ins</Text>
          <Text style={styles.captionText}>Usernames and passwords agents use in the workspace browser.</Text>
        </Pressable>
      )}
      {inner}
    </ScrollView>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },
    content: {
      padding: spacing.lg,
      gap: spacing.md,
      paddingBottom: spacing.xl,
      flexGrow: 1,
    },

    overline: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
    },
    bodyText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    captionText: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: p.fgMute },

    section: { gap: spacing.sm },
    sectionLabel: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
      paddingHorizontal: spacing.xs,
    },
    sectionCount: { color: p.fgMute },

    // Server card.
    card: {
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
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
      color: p.fg,
    },
    // In a column card (no row to fill), so the title keeps its own height.
    cardTitleStacked: { flex: 0 },
    statusLabel: {
      fontFamily: fonts.mono,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 0.6,
      color: p.fgMute,
    },
    commandLine: {
      fontFamily: fonts.mono,
      fontSize: 11,
      lineHeight: 16,
      color: p.fgSoft,
    },
    metaLine: {
      fontFamily: fonts.sans,
      fontSize: 11,
      lineHeight: 15,
      color: p.fgMute,
    },

    // Read-only note card.
    noteCard: {
      backgroundColor: p.raised,
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
      color: p.link,
    },

    // Empty state (spec §7).
    emptyWrap: {
      flexGrow: 1,
      alignItems: "center",
      justifyContent: "center",
      gap: spacing.sm,
      padding: spacing.xl,
    },
    dim: {
      fontFamily: fonts.sans,
      fontSize: 15,
      lineHeight: 22,
      color: p.fgMute,
      textAlign: "center",
    },
    dimSmall: {
      fontFamily: fonts.sans,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
      textAlign: "center",
    },

    // Skeletons (spec §7).
    skeletonGroup: { gap: spacing.md },
    skeletonCard: {
      height: 96,
      borderRadius: radii.lg,
      backgroundColor: p.raised,
    },

    // Error card (spec §4).
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
