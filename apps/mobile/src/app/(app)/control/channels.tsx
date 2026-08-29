import { useCallback, useMemo, useState, type ReactElement } from "react";
import {
  Animated,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useGatewayRpc } from "@lightbulb/shared";

import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import { relativeTime } from "../../../lib/agentSessions";
import { useGateway } from "../../../providers/gateway";
import { LogoMark } from "../../../components/LogoMark";

// ---------------------------------------------------------------------------
// Contract shapes (w3-control-contract §5 Channels)
//
// channels.status {probe:true} forces a live connectivity probe (not the
// cached snapshot). Read-only: linking/setup lives on the web + BotSetupWizard.
//
// Refresh rule: UNDEFINED data = container unreachable — render "couldn't
// reach container", NEVER collapse into "no channels configured". An empty
// channelOrder is the genuine "no channels" state.
// ---------------------------------------------------------------------------

interface AccountSnapshot {
  accountId?: string;
  name?: string;
  configured?: boolean;
  linked?: boolean;
  running?: boolean;
  connected?: boolean;
  mode?: string;
  lastInboundAt?: number;
  lastOutboundAt?: number;
  lastError?: string;
}

interface ChannelsStatus {
  ts?: number;
  channelOrder?: string[];
  channelLabels?: Record<string, string>;
  channelAccounts?: Record<string, AccountSnapshot[]>;
}

function statusTone(a: AccountSnapshot): {
  color: string;
  label: string;
} {
  if (a.lastError) return { color: colors.danger, label: "Error" };
  if (a.connected) return { color: colors.runningGreen, label: "Connected" };
  if (a.running) return { color: colors.connectingYellow, label: "Running" };
  if (a.linked) return { color: colors.connectingYellow, label: "Linked" };
  if (a.configured) return { color: colors.muteSoft, label: "Configured" };
  return { color: colors.muteSoft, label: "Idle" };
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

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

function BoolFlag({ label, on }: { label: string; on: boolean | undefined }) {
  return (
    <View style={styles.flag}>
      <View
        style={[
          styles.flagDot,
          { backgroundColor: on ? colors.runningGreen : colors.muteSoft },
        ]}
      />
      <Text style={styles.flagText}>{label}</Text>
    </View>
  );
}

function AccountCard({ account }: { account: AccountSnapshot }) {
  const tone = statusTone(account);
  const title = account.name || account.accountId || "Account";
  return (
    <View style={styles.card}>
      <View style={styles.cardHead}>
        <View style={[styles.statusDot, { backgroundColor: tone.color }]} />
        <Text style={styles.cardTitle} numberOfLines={1}>
          {title}
        </Text>
        <Text style={styles.statusLabel}>{tone.label}</Text>
      </View>

      <View style={styles.flagRow}>
        <BoolFlag label="Configured" on={account.configured} />
        <BoolFlag label="Linked" on={account.linked} />
        <BoolFlag label="Running" on={account.running} />
        <BoolFlag label="Connected" on={account.connected} />
      </View>

      {account.mode != null && (
        <Text style={styles.metaLine}>Mode · {account.mode}</Text>
      )}
      {account.lastInboundAt != null && (
        <Text style={styles.metaLine}>
          Last inbound · {relativeTime(account.lastInboundAt)}
        </Text>
      )}
      {account.lastOutboundAt != null && (
        <Text style={styles.metaLine}>
          Last outbound · {relativeTime(account.lastOutboundAt)}
        </Text>
      )}
      {account.lastError ? (
        <Text style={styles.errorLine} numberOfLines={3}>
          {account.lastError}
        </Text>
      ) : null}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function ChannelsPanel() {
  const gateway = useGateway();
  const { data, error, isLoading, mutate } = useGatewayRpc<ChannelsStatus>(
    gateway,
    "channels.status",
    { probe: true },
  );

  const [refreshing, setRefreshing] = useState(false);
  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await mutate();
    } finally {
      setRefreshing(false);
    }
  }, [mutate]);

  const order = useMemo(() => data?.channelOrder ?? [], [data]);

  const connectionDown = !gateway.isConnected && gateway.error != null;

  let inner: ReactElement;
  if (error) {
    inner = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load channels: {error.message}
        </Text>
        <Pressable
          style={({ pressed }) => [styles.retryButton, pressed && styles.pressed]}
          onPress={() => void mutate()}
          accessibilityRole="button"
          accessibilityLabel="Retry"
        >
          <Text style={styles.errorRetryText}>Retry</Text>
        </Pressable>
      </View>
    );
  } else if (!data && connectionDown) {
    inner = (
      <View style={styles.centerGrow}>
        <Text style={styles.dim}>{gateway.error}</Text>
      </View>
    );
  } else if (isLoading && !data) {
    inner = <SkeletonCards />;
  } else if (!data) {
    // UNDEFINED data (not an error) = container unreachable. Contract §5:
    // must NOT collapse into "no channels configured".
    inner = (
      <View style={styles.centerGrow}>
        <Text style={styles.dim}>Couldn’t reach your container.</Text>
        <Pressable
          style={({ pressed }) => [styles.textRetry, pressed && styles.pressed]}
          onPress={() => void mutate()}
          accessibilityRole="button"
          accessibilityLabel="Retry"
        >
          <Text style={styles.retryText}>Retry</Text>
        </Pressable>
      </View>
    );
  } else if (order.length === 0) {
    inner = (
      <View style={styles.emptyWrap}>
        <LogoMark size={64} />
        <Text style={styles.dim}>No channels configured.</Text>
        <Text style={styles.dimSmall}>
          Set up Telegram, Discord, or Slack on flickmeon.ai.
        </Text>
      </View>
    );
  } else {
    inner = (
      <>
        {order.map((channelId) => {
          const label = data.channelLabels?.[channelId] ?? channelId;
          const accounts = data.channelAccounts?.[channelId] ?? [];
          return (
            <View key={channelId} style={styles.section}>
              <Text style={styles.sectionLabel}>
                {label.toUpperCase()}
                <Text style={styles.sectionCount}> · {accounts.length}</Text>
              </Text>
              {accounts.length === 0 ? (
                <Text style={styles.dimSmall}>No accounts linked.</Text>
              ) : (
                accounts.map((account, i) => (
                  <AccountCard
                    key={account.accountId ?? `${channelId}-${i}`}
                    account={account}
                  />
                ))
              )}
            </View>
          );
        })}
      </>
    );
  }

  return (
    <ScrollView
      style={styles.page}
      contentContainerStyle={styles.content}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={() => void onRefresh()}
          tintColor={colors.forest}
        />
      }
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
  centerGrow: {
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

  section: { gap: spacing.sm },
  sectionLabel: { ...type.overline, paddingHorizontal: spacing.xs },
  sectionCount: { color: colors.sectionCount },

  // Account card.
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
  flagRow: {
    flexDirection: "row",
    flexWrap: "wrap",
    gap: spacing.md,
  },
  flag: { flexDirection: "row", alignItems: "center", gap: spacing.xs },
  flagDot: { width: 6, height: 6, borderRadius: 3 },
  flagText: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 14,
    color: colors.inkSoft,
  },
  metaLine: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  errorLine: {
    fontFamily: fonts.sans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.danger,
  },

  // Empty state (spec §7).
  emptyWrap: {
    flexGrow: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.sm,
    padding: spacing.xl,
  },

  // Skeletons (spec §7).
  skeletonGroup: { gap: spacing.md },
  skeletonCard: {
    height: 120,
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
  pressed: { opacity: 0.6 },
  textRetry: { paddingVertical: spacing.xs },
  retryButton: {
    alignSelf: "flex-start",
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
  },
  retryText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },
  errorRetryText: {
    fontFamily: fonts.sansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
});
