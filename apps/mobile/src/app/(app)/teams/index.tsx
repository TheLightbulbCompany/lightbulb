import Feather from "@expo/vector-icons/Feather";
import { router } from "expo-router";
import { type ReactElement } from "react";
import {
  Animated,
  FlatList,
  Linking,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { usePulse } from "../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import {
  humanizeStatus,
  timeAgo,
  useTeamsInbox,
  type InboxItem,
} from "../../../lib/teamsData";

// Teams is a subscription-gated, web-managed surface: when the workspace needs
// a subscription the mobile app deep-links out rather than running checkout.
const MANAGE_URL = "https://isol8.co/teams";

// ---------------------------------------------------------------------------
// Readiness affordance (spec §7) — pulsing dot for the provisioning state.
// ---------------------------------------------------------------------------

function PulsingDot() {
  const opacity = usePulse(true, 0.3);
  return <Animated.View style={[styles.pulseDot, { opacity }]} />;
}

function SkeletonRows() {
  const pulse = usePulse(true, 0.55);
  return (
    <View style={styles.skeletonGroup}>
      {[0, 1, 2, 3].map((i) => (
        <Animated.View key={i} style={[styles.skeletonRow, { opacity: pulse }]} />
      ))}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Inbox row (spec §5) — title / author+time / status preview + unread dot.
// ---------------------------------------------------------------------------

function InboxRow({ item }: { item: InboxItem }) {
  const identifier = item.identifier ?? item.id.slice(0, 8);
  const when = timeAgo(item.updatedAt ?? item.lastActivityAt ?? item.createdAt);
  const meta = [identifier, when].filter(Boolean).join(" · ");
  const preview = item.status ? humanizeStatus(item.status) : null;

  return (
    <Pressable
      style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
      onPress={() =>
        router.push({
          pathname: "/(app)/teams/issue/[id]",
          params: { id: item.id, title: item.title },
        })
      }
      accessibilityRole="button"
      accessibilityLabel={`${item.title}. ${meta}`}
    >
      <View style={styles.rowText}>
        <View style={styles.titleRow}>
          <Text style={styles.title} numberOfLines={1}>
            {item.title}
          </Text>
          {item.unread ? <View style={styles.unreadDot} /> : null}
        </View>
        {meta !== "" && (
          <Text style={styles.metaLine} numberOfLines={1}>
            {meta}
          </Text>
        )}
        {preview != null && (
          <Text style={styles.previewLine} numberOfLines={1}>
            {preview}
          </Text>
        )}
      </View>
      <Feather name="chevron-right" size={18} color={colors.muteSoft} />
    </Pressable>
  );
}

// ---------------------------------------------------------------------------
// Readiness / empty / error states (contract §1 — 202/402/503 are NOT errors).
// ---------------------------------------------------------------------------

function CenteredState({
  glyph,
  title,
  detail,
  action,
  pulsing,
}: {
  glyph?: ReactElement;
  title: string;
  detail?: string | null;
  action?: ReactElement;
  pulsing?: boolean;
}) {
  return (
    <View style={styles.centered}>
      {pulsing ? <PulsingDot /> : glyph}
      <Text style={styles.stateTitle}>{title}</Text>
      {detail != null && detail !== "" && (
        <Text style={styles.stateDetail}>{detail}</Text>
      )}
      {action}
    </View>
  );
}

function OutlineButton({
  label,
  onPress,
  danger,
}: {
  label: string;
  onPress: () => void;
  danger?: boolean;
}) {
  return (
    <Pressable
      style={({ pressed }) => [
        styles.button,
        danger && styles.buttonDanger,
        pressed && styles.buttonPressed,
      ]}
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
    >
      <Text style={[styles.buttonText, danger && styles.buttonTextDanger]}>
        {label}
      </Text>
    </Pressable>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function TeamsInbox() {
  const { state, refreshing, retry, refresh } = useTeamsInbox();

  if (state.kind === "loading") {
    return (
      <View style={styles.page}>
        <SkeletonRows />
      </View>
    );
  }

  if (state.kind === "gate") {
    if (state.gate === "provisioning") {
      return (
        <View style={styles.page}>
          <CenteredState
            pulsing
            title="Setting up your workspace…"
            detail="Your team’s agents are being provisioned. This usually takes under a minute."
            action={<OutlineButton label="Refresh" onPress={retry} />}
          />
        </View>
      );
    }
    if (state.gate === "needs_subscription") {
      return (
        <View style={styles.page}>
          <CenteredState
            glyph={<Feather name="lock" size={28} color={colors.mute} />}
            title="Teams requires an active subscription"
            detail="Manage your subscription on isol8.co to unlock your team workspace."
            action={
              <OutlineButton
                label="Manage on isol8.co"
                onPress={() => void Linking.openURL(MANAGE_URL)}
              />
            }
          />
        </View>
      );
    }
    // unavailable
    return (
      <View style={styles.page}>
        <CenteredState
          glyph={<Feather name="cloud-off" size={28} color={colors.mute} />}
          title="Couldn’t reach your workspace"
          detail={state.message}
          action={<OutlineButton label="Retry" onPress={retry} />}
        />
      </View>
    );
  }

  // ready
  const empty = state.items.length === 0;
  return (
    <FlatList
      style={styles.page}
      data={state.items}
      keyExtractor={(item) => item.id}
      contentContainerStyle={[styles.listContent, empty && styles.grow]}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          onRefresh={refresh}
          tintColor={colors.mute}
        />
      }
      ListHeaderComponent={
        !empty ? (
          <Text style={styles.sectionLabel}>
            INBOX
            <Text style={styles.sectionCount}> · {state.items.length}</Text>
          </Text>
        ) : null
      }
      ListEmptyComponent={
        <CenteredState
          glyph={<Text style={styles.emptyGlyph}>8</Text>}
          title="No items"
          detail="Issues assigned to you will show up here."
        />
      }
      renderItem={({ item }) => <InboxRow item={item} />}
    />
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  listContent: {
    paddingHorizontal: spacing.md,
    paddingTop: spacing.md,
    paddingBottom: spacing.xl,
  },
  grow: { flexGrow: 1, justifyContent: "center" },

  // Section label (spec §5).
  sectionLabel: {
    ...type.overline,
    paddingHorizontal: spacing.sm,
    marginBottom: spacing.sm,
  },
  sectionCount: { color: colors.sectionCount },

  // Inbox rows (spec §5).
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingVertical: spacing.md,
    paddingHorizontal: spacing.sm,
    borderRadius: radii.md,
  },
  rowPressed: { backgroundColor: colors.rowPressed },
  rowText: { flex: 1, gap: 3 },
  titleRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  title: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.ink,
    flexShrink: 1,
  },
  unreadDot: {
    width: 7,
    height: 7,
    borderRadius: 4,
    backgroundColor: colors.forest,
  },
  metaLine: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },
  previewLine: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },

  // Readiness / empty / error states.
  centered: {
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    padding: spacing.xl,
  },
  pulseDot: {
    width: 12,
    height: 12,
    borderRadius: 6,
    backgroundColor: colors.forestSoft,
  },
  stateTitle: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 20,
    color: colors.ink,
    textAlign: "center",
  },
  stateDetail: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 19,
    color: colors.mute,
    textAlign: "center",
  },
  emptyGlyph: {
    fontFamily: fonts.loraItalic,
    fontSize: 48,
    lineHeight: 56,
    color: colors.ink,
  },

  // Buttons.
  button: {
    marginTop: spacing.xs,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  buttonDanger: { borderColor: colors.errorBorder },
  buttonPressed: { opacity: 0.6 },
  buttonText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },
  buttonTextDanger: { color: colors.errorText },

  // Loading skeletons (spec §7).
  skeletonGroup: {
    gap: spacing.sm,
    paddingHorizontal: spacing.md,
    paddingTop: spacing.md,
  },
  skeletonRow: {
    height: 56,
    borderRadius: radii.md,
    backgroundColor: colors.skeleton,
  },
});
