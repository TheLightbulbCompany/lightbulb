import {
  activityActionRequest,
  newestActivityAt,
  type ActivityAction,
  type ActivityActionInput,
  type ActivityItem,
} from "@lightbulb/shared";
import { router, useIsFocused } from "expo-router";
import { memo, useCallback, useEffect, useRef, useState } from "react";
import { ActivityIndicator, Alert, Animated, FlatList, Pressable, StyleSheet, Text, View } from "react-native";

import { usePulse } from "../../../components/chat/usePulse";
import { LogoMark } from "../../../components/LogoMark";
import { Avatar } from "../../../components/room/Avatar";
import { PickerSheet, type PickerOption } from "../../../components/teams/PickerSheet";
import { ReasonSheet } from "../../../components/teams/ReasonSheet";
import { colors, fonts, radii, spacing, type } from "../../../config/theme";
import { activityHrefs, itemTarget, useMobileActivity } from "../../../lib/activity";
import { purchaseSafeMessage } from "../../../lib/purchaseCta";
import { relativeTime } from "../../../lib/relativeTime";
import { fetchAgents, fetchMembers, useTeamsConfig } from "../../../lib/teamsData";

const ACTION_FAILED = "That didn’t go through. Try again.";

/** The server's own words when it sent some; never a bare "API 500". Some
 *  activity items proxy to Paperclip (approvals), so a purchase-gate 402's
 *  code gets swapped for local neutral copy like every other Teams action. */
function failure(error: unknown): string {
  const message = purchaseSafeMessage(error, "");
  return message && !/^API \d+$/.test(message) ? message : ACTION_FAILED;
}

function go(target: NonNullable<ActivityAction["navigate"]> | null) {
  if (target) for (const href of activityHrefs(target)) router.push(href);
}

/** A resolved item's footer, generic across kinds: "Resolved by Dana · 5m". */
function resolvedLine(item: ActivityItem): string {
  const who = item.resolvedBy ? ` by ${item.resolvedBy.name}` : "";
  const when = item.resolvedAt ? ` · ${relativeTime(Date.parse(item.resolvedAt))}` : "";
  return `${item.state === "done" ? "Done" : "Resolved"}${who}${when}`;
}

type Pending = { item: ActivityItem; action: ActivityAction & { input: ActivityActionInput } };

const ActivityRow = memo(function ActivityRow({
  item,
  busy,
  error,
  onAction,
}: {
  item: ActivityItem;
  busy: boolean;
  error: string | null;
  onAction: (item: ActivityItem, action: ActivityAction) => void;
}) {
  const { summary } = item;
  const open = item.state === "open";
  const target = itemTarget(item);
  return (
    <View style={styles.row}>
      {/* The row body and its buttons are siblings, so VoiceOver reaches each
          button on its own instead of folding them into the row's label. */}
      <Pressable
        onPress={() => go(target)}
        disabled={!target}
        accessibilityRole="button"
        accessibilityLabel={summary.title}
        accessibilityHint={target ? "Opens it" : undefined}
        style={({ pressed }) => [styles.body, pressed && styles.pressed]}
      >
        <View style={styles.lead}>
          {item.read ? null : <View accessibilityLabel="Unread" style={styles.dot} />}
          <Avatar name={summary.requester.name} kind={summary.requester.kind} size={36} />
        </View>
        <View style={styles.text}>
          <Text style={[styles.title, !item.read && styles.titleUnread]}>{summary.title}</Text>
          <Text style={styles.meta}>
            {`${summary.requester.name} · ${relativeTime(Date.parse(item.createdAt))}`}
          </Text>
          {summary.reason ? <Text style={styles.reason}>{summary.reason}</Text> : null}
          {summary.preview?.text ? (
            <Text style={styles.preview} numberOfLines={4}>{summary.preview.text}</Text>
          ) : null}
          {summary.preview?.destination ? (
            <Text style={styles.destination}>{summary.preview.destination}</Text>
          ) : null}
          {summary.preview?.fields.map((field) => (
            <Text key={field.label} style={styles.field}>
              <Text style={styles.fieldLabel}>{`${field.label}: `}</Text>
              {field.value}
            </Text>
          ))}
          {open ? null : <Text style={styles.resolved}>{resolvedLine(item)}</Text>}
        </View>
      </Pressable>
      {item.actions.length > 0 ? (
        <View style={styles.actions}>
          {item.actions.map((action) => (
            <Pressable
              key={action.id}
              onPress={() => onAction(item, action)}
              disabled={busy}
              accessibilityRole="button"
              accessibilityLabel={action.label}
              accessibilityState={{ disabled: busy }}
              style={({ pressed }) => [
                styles.button,
                action.style === "primary" && styles.buttonPrimary,
                action.style === "destructive" && styles.buttonDestructive,
                (pressed || busy) && styles.buttonDim,
              ]}
            >
              <Text
                style={[
                  styles.buttonText,
                  action.style === "primary" && styles.buttonTextPrimary,
                  action.style === "destructive" && styles.buttonTextDestructive,
                ]}
              >
                {action.label}
              </Text>
            </Pressable>
          ))}
          {busy ? <ActivityIndicator color={colors.mute} /> : null}
        </View>
      ) : null}
      {error ? (
        <Text accessibilityRole="alert" style={styles.rowError}>{error}</Text>
      ) : null}
    </View>
  );
});

/** Placeholder rows shaped like the real ones, so the first paint does not jump. */
function Skeleton() {
  const pulse = usePulse(true, 0.55);
  return (
    <View testID="activity-skeleton">
      {[0, 1, 2, 3, 4].map((i) => (
        <Animated.View key={i} style={[styles.row, styles.body, { opacity: pulse }]}>
          <View style={styles.skeletonAvatar} />
          <View style={styles.text}>
            <View style={[styles.skeletonLine, { width: "80%" }]} />
            <View style={[styles.skeletonLine, { width: "45%" }]} />
          </View>
        </Animated.View>
      ))}
    </View>
  );
}

/** The workspace's agents and people, for a `person` input: loaded when one opens. */
function useRoster(open: boolean): PickerOption[] {
  const config = useTeamsConfig();
  const [roster, setRoster] = useState<PickerOption[]>([]);
  useEffect(() => {
    if (!open) return;
    let alive = true;
    void Promise.all([fetchAgents(config), fetchMembers(config)]).then(([agents, members]) => {
      if (!alive) return;
      setRoster([
        ...agents.map((a) => ({ key: a.id, label: a.name || "Agent", section: "Agents" })),
        ...members.map((m) => ({ key: m.userId, label: m.name || m.email || "Workspace member", section: "People" })),
      ]);
    });
    return () => {
      alive = false;
    };
  }, [open, config]);
  return roster;
}

/**
 * Activity: what needs you — mentions, DMs, thread replies, approvals and
 * issues assigned to you, newest first. Every word and button is the
 * server's (`summary`, `actions`); this screen renders them generically and
 * never composes text per kind.
 */
export default function Activity() {
  const focused = useIsFocused();
  const activity = useMobileActivity(focused);
  const { items, markSeen, runAction } = activity;
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [errors, setErrors] = useState<ReadonlyMap<string, string>>(new Map());
  const [pending, setPending] = useState<Pending | null>(null);
  const roster = useRoster(pending?.action.input.kind === "person");

  // Looking at the list is seeing it: the badge clears up to the newest item.
  const newest = newestActivityAt(items);
  useEffect(() => {
    if (focused && newest) void markSeen(newest).catch(() => {});
  }, [focused, newest, markSeen]);

  const run = useCallback((item: ActivityItem, action: ActivityAction, answer?: string) => {
    setBusy(item.id);
    setErrors((current) => {
      const next = new Map(current);
      next.delete(item.id);
      return next;
    });
    runAction(item.id, action.id, activityActionRequest(action.input, answer))
      .catch((error: unknown) => setErrors((current) => new Map(current).set(item.id, failure(error))))
      .finally(() => setBusy(null));
  }, [runAction]);

  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const onAction = useCallback((item: ActivityItem, action: ActivityAction) => {
    if (action.navigate) return go(action.navigate);
    const proceed = () => {
      const { input } = action;
      if (input) setPending({ item, action: { ...action, input } });
      else run(item, action);
    };
    if (!action.confirm) return proceed();
    Alert.alert(action.confirm, undefined, [
      { text: "Cancel", style: "cancel" },
      { text: action.label, style: action.style === "destructive" ? "destructive" : "default", onPress: proceed },
    ]);
  }, [run]);

  const answer = (value: string) => {
    const current = pendingRef.current;
    if (current) run(current.item, current.action, value);
  };

  const refresh = () => {
    setRefreshing(true);
    void activity.refresh().catch(() => {}).finally(() => setRefreshing(false));
  };

  const input = pending?.action.input;
  return (
    <View style={styles.page}>
      <FlatList
        testID="activity-list"
        contentInsetAdjustmentBehavior="automatic"
        data={items}
        keyExtractor={(item) => item.id}
        contentContainerStyle={[styles.list, items.length === 0 && styles.listGrow]}
        refreshing={refreshing}
        onRefresh={refresh}
        onEndReached={activity.loadMore}
        onEndReachedThreshold={0.5}
        renderItem={({ item }) => (
          <ActivityRow item={item} busy={busy === item.id} error={errors.get(item.id) ?? null} onAction={onAction} />
        )}
        ItemSeparatorComponent={() => <View style={styles.separator} />}
        ListHeaderComponent={
          activity.error && items.length > 0 ? (
            <Text accessibilityRole="alert" style={styles.headerError}>
              Couldn’t refresh activity: {activity.error.message}
            </Text>
          ) : null
        }
        ListEmptyComponent={
          activity.loading ? (
            <Skeleton />
          ) : activity.error ? (
            <View style={styles.errorCard}>
              <Text selectable style={styles.errorText}>
                Couldn’t load activity: {activity.error.message}
              </Text>
              <Pressable onPress={() => void activity.refresh().catch(() => {})} accessibilityRole="button" accessibilityLabel="Retry">
                <Text style={styles.retryText}>Retry</Text>
              </Pressable>
            </View>
          ) : (
            <View style={styles.empty}>
              <LogoMark size={48} />
              <Text style={styles.dim}>Nothing needs you right now.</Text>
            </View>
          )
        }
        ListFooterComponent={
          <>
            {activity.loadingMore ? <ActivityIndicator style={styles.more} color={colors.mute} /> : null}
            {activity.paperclipAvailable ? null : (
              <Text style={styles.footnote}>
                Paperclip is unavailable, so approvals and assigned issues may be missing.
              </Text>
            )}
          </>
        }
      />
      <ReasonSheet
        visible={input?.kind === "text"}
        title={pending?.action.confirm ?? input?.label ?? ""}
        placeholder={input?.label}
        submitLabel={pending?.action.label}
        required={input?.required ?? false}
        tone={pending?.action.style === "destructive" ? "danger" : "primary"}
        onSubmit={answer}
        onClose={() => setPending(null)}
      />
      <PickerSheet
        visible={input?.kind === "select" || input?.kind === "person"}
        heading={input?.label ?? ""}
        subheading={pending?.item.summary.title ?? null}
        options={input?.kind === "select" ? (input.options ?? []).map((o) => ({ key: o.value, label: o.label })) : roster}
        onSelect={answer}
        onClose={() => setPending(null)}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  list: { paddingBottom: spacing.xl },
  listGrow: { flexGrow: 1 },
  row: { paddingHorizontal: spacing.lg, paddingVertical: spacing.md, gap: spacing.sm },
  body: { flexDirection: "row", gap: spacing.md, borderRadius: radii.md },
  pressed: { backgroundColor: colors.rowPressed },
  // The dot sits in the gutter left of the avatar, where Slack keeps it.
  lead: { flexDirection: "row", alignItems: "flex-start" },
  dot: {
    position: "absolute",
    left: -spacing.md,
    top: 14,
    width: 8,
    height: 8,
    borderRadius: 4,
    backgroundColor: colors.info,
  },
  text: { flex: 1, gap: 2 },
  title: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.ink },
  titleUnread: { fontFamily: fonts.sansSemiBold },
  meta: { ...type.caption },
  reason: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 19, color: colors.inkSoft },
  preview: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.ink,
    marginTop: spacing.xs,
    paddingLeft: spacing.sm,
    borderLeftWidth: 2,
    borderLeftColor: colors.edge,
  },
  destination: { ...type.caption, marginTop: 2 },
  field: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.inkSoft },
  fieldLabel: { fontFamily: fonts.sansMedium, color: colors.mute },
  resolved: { ...type.caption, marginTop: spacing.xs },
  // Wraps under large text instead of squeezing the labels.
  actions: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: spacing.sm, paddingLeft: 36 + spacing.md },
  button: {
    minHeight: 32,
    justifyContent: "center",
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.xs,
    borderRadius: radii.sm,
    borderCurve: "continuous",
    borderWidth: 1,
    borderColor: colors.edge,
  },
  buttonPrimary: { backgroundColor: colors.forest, borderColor: colors.forest },
  buttonDestructive: { borderColor: colors.danger },
  buttonDim: { opacity: 0.6 },
  buttonText: { fontFamily: fonts.sansMedium, fontSize: 14, lineHeight: 19, color: colors.ink },
  buttonTextPrimary: { color: colors.white },
  buttonTextDestructive: { color: colors.danger },
  rowError: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText, paddingLeft: 36 + spacing.md },
  separator: { height: StyleSheet.hairlineWidth, backgroundColor: colors.edge, marginLeft: spacing.lg + 36 + spacing.md },
  skeletonAvatar: { width: 36, height: 36, borderRadius: radii.md, backgroundColor: colors.skeleton },
  skeletonLine: { height: 12, borderRadius: radii.xs, backgroundColor: colors.skeleton, marginVertical: 3 },
  empty: { flex: 1, alignItems: "center", justifyContent: "center", gap: spacing.sm, padding: spacing.xl },
  dim: { ...type.body, color: colors.mute, textAlign: "center" },
  headerError: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText, paddingHorizontal: spacing.lg, paddingTop: spacing.sm },
  errorCard: {
    margin: spacing.lg,
    padding: spacing.lg,
    gap: spacing.sm,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
  },
  errorText: { ...type.body, color: colors.errorText },
  retryText: { fontFamily: fonts.sansSemiBold, fontSize: 14, color: colors.errorText },
  more: { paddingVertical: spacing.md },
  footnote: { ...type.caption, textAlign: "center", paddingHorizontal: spacing.xl, paddingTop: spacing.md },
});
