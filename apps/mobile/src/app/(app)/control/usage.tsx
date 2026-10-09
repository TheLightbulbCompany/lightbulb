import { useAuth, useOrganization } from "@clerk/expo";
import { useFocusEffect } from "expo-router";
import { useCallback, useMemo, useRef, useState, type ReactNode } from "react";
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

// Usage is an ADMIN-ONLY panel (contract §6): the whole surface is hidden from
// non-admin org members on the web, and the by-member table is gated again
// inline. On mobile we render a clean pointer to the web for non-admins and
// mirror the server's 403 as the same restricted state.
const USAGE_WEB_URL = `${WEB_ORIGIN}/chat?panel=usage`;

// ---------------------------------------------------------------------------
// Contract shapes (w3-control-contract §6 Usage; schemas/billing.py)
// ---------------------------------------------------------------------------

interface UsageMember {
  user_id: string;
  display_name: string | null;
  email: string | null;
  total_spend: number;
  total_input_tokens: number;
  total_output_tokens: number;
  request_count: number;
}

interface UsageSummary {
  period: string;
  total_spend: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cache_read_tokens: number;
  total_cache_write_tokens: number;
  request_count: number;
  lifetime_spend: number;
  by_member: UsageMember[];
}

type UsageState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  /** 404 — no usage rows yet (pre-activity empty state). */
  | { kind: "none" }
  /** 403 or client-side org gate — admin-only surface. */
  | { kind: "restricted" }
  | { kind: "ready"; usage: UsageSummary };

// ---------------------------------------------------------------------------
// Data hook — getTokenRef pattern (settings.tsx): pin Clerk's churning
// getToken in a ref so the api client + `load` stay referentially stable and
// useFocusEffect fires once per focus (a `[getToken]` dep loops forever).
// ---------------------------------------------------------------------------

function useUsageData(orgRestricted: boolean) {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );
  const orgRestrictedRef = useRef(orgRestricted);
  orgRestrictedRef.current = orgRestricted;
  const [state, setState] = useState<UsageState>({ kind: "loading" });
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = loadSeq.current + 1;
    loadSeq.current = seq;
    // Non-admin org members only get a 403 back — skip the round-trip.
    if (orgRestrictedRef.current) {
      setState({ kind: "restricted" });
      return;
    }
    setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
    try {
      const usage = (await api.get("/billing/usage")) as UsageSummary;
      if (loadSeq.current !== seq) return;
      setState({ kind: "ready", usage });
    } catch (err) {
      if (loadSeq.current !== seq) return;
      if (err instanceof ApiError && err.status === 404) {
        setState({ kind: "none" });
      } else if (err instanceof ApiError && err.status === 403) {
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
// Formatting (contract §6 display fields)
// ---------------------------------------------------------------------------

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}K`;
  return String(n);
}

function memberName(m: UsageMember): string {
  return m.display_name || m.email || m.user_id;
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

function Card({ children }: { children: ReactNode }) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return <View style={styles.card}>{children}</View>;
}

function Tile({ label, value }: { label: string; value: string }) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return (
    <View style={styles.tile}>
      <Text style={styles.tileLabel}>{label}</Text>
      <Text style={styles.tileValue} numberOfLines={1}>
        {value}
      </Text>
    </View>
  );
}

function SkeletonRow({ delayMs, width }: { delayMs: number; width: `${number}%` }) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const pulse = usePulse(true, 0.55, 700, delayMs);
  return <Animated.View style={[styles.skeletonLine, { width, opacity: pulse }]} />;
}

function SkeletonCards() {
  return (
    <>
      <Card>
        <SkeletonRow delayMs={150} width="30%" />
        <SkeletonRow delayMs={300} width="70%" />
      </Card>
      <Card>
        <SkeletonRow delayMs={450} width="40%" />
        <SkeletonRow delayMs={600} width="60%" />
      </Card>
    </>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function UsagePanel() {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const { organization, membership } = useOrganization();
  const orgRestricted =
    organization != null && membership != null && membership.role !== "org:admin";
  const { state, retry } = useUsageData(orgRestricted);

  const restricted = orgRestricted || state.kind === "restricted";

  let body: ReactNode;
  if (restricted) {
    body = (
      <Card>
        <Text style={styles.overline}>USAGE</Text>
        <Text style={styles.bodyText}>
          Usage is available to workspace admins on thelightbulb.company
        </Text>
        <Pressable
          style={({ pressed }) => [styles.linkRow, pressed && styles.pressed]}
          onPress={() => void Linking.openURL(USAGE_WEB_URL)}
          accessibilityRole="button"
          accessibilityLabel="Open usage on thelightbulb.company"
        >
          <Text style={styles.linkText}>Open usage on thelightbulb.company</Text>
        </Pressable>
      </Card>
    );
  } else if (state.kind === "loading") {
    body = <SkeletonCards />;
  } else if (state.kind === "error") {
    body = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load usage: {state.message}
        </Text>
        <Pressable
          style={({ pressed }) => [styles.retryButton, pressed && styles.pressed]}
          onPress={() => void retry()}
          accessibilityRole="button"
          accessibilityLabel="Retry"
        >
          <Text style={styles.retryText}>Retry</Text>
        </Pressable>
      </View>
    );
  } else if (state.kind === "none") {
    body = (
      <Card>
        <Text style={styles.overline}>USAGE</Text>
        <Text style={styles.bodyText}>No usage recorded yet.</Text>
        <Text style={styles.captionText}>
          Token spend appears here once your agents start running.
        </Text>
      </Card>
    );
  } else {
    const u = state.usage;
    body = (
      <>
        {u.period !== "" && (
          <Text style={styles.periodText}>{u.period}</Text>
        )}

        <View style={styles.grid}>
          <Tile label="INPUT" value={formatTokens(u.total_input_tokens)} />
          <Tile label="OUTPUT" value={formatTokens(u.total_output_tokens)} />
          <Tile label="CACHE READ" value={formatTokens(u.total_cache_read_tokens)} />
          <Tile label="CACHE WRITE" value={formatTokens(u.total_cache_write_tokens)} />
          <Tile label="REQUESTS" value={String(u.request_count)} />
        </View>

        {u.by_member.length > 0 && (
          <Card>
            <Text style={styles.overline}>BY MEMBER</Text>
            {u.by_member.map((m, i) => (
              <View
                key={m.user_id}
                style={[styles.memberRow, i > 0 && styles.memberRowBorder]}
              >
                <Text style={styles.memberName} numberOfLines={1}>
                  {memberName(m)}
                </Text>
                <View style={styles.memberMeta}>
                  <Text style={styles.memberTokens}>{m.request_count} req</Text>
                </View>
              </View>
            ))}
          </Card>
        )}
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
    content: {
      padding: spacing.lg,
      gap: spacing.md,
      paddingBottom: spacing.xl,
    },

    card: {
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.lg,
      gap: spacing.sm,
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
    captionText: {
      fontFamily: fonts.sans,
      fontSize: 12,
      lineHeight: 16,
      color: p.fgMute,
    },

    linkRow: { marginTop: spacing.xs, alignSelf: "flex-start", borderRadius: radii.xs },
    linkText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.link,
    },

    periodText: {
      fontFamily: fonts.mono,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 0.6,
      color: p.fgMute,
    },
    // Token tile grid.
    grid: { flexDirection: "row", flexWrap: "wrap", gap: spacing.md },
    tile: {
      flexGrow: 1,
      flexBasis: "30%",
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.md,
      gap: spacing.xs,
    },
    tileLabel: {
      fontFamily: fonts.mono,
      fontSize: 9,
      lineHeight: 13,
      letterSpacing: 1.2,
      color: p.fgMute,
    },
    tileValue: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 18,
      lineHeight: 24,
      color: p.fg,
    },

    // By-member rows.
    memberRow: {
      flexDirection: "row",
      alignItems: "center",
      justifyContent: "space-between",
      gap: spacing.sm,
      paddingVertical: spacing.sm,
    },
    memberRowBorder: {
      borderTopWidth: StyleSheet.hairlineWidth,
      borderTopColor: p.edge,
    },
    memberName: {
      flex: 1,
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    memberMeta: { alignItems: "flex-end", gap: 1 },
    memberTokens: {
      fontFamily: fonts.mono,
      fontSize: 10,
      lineHeight: 14,
      color: p.fgMute,
    },
    // Loading skeleton (§7).
    skeletonLine: {
      height: 14,
      borderRadius: radii.xs,
      backgroundColor: p.raised,
    },

    // Error card (§4).
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
    pressed: { opacity: 0.6 },
    retryButton: {
      alignSelf: "flex-start",
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.sm,
      backgroundColor: p.surface,
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.xs,
    },
    retryText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.alert,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
