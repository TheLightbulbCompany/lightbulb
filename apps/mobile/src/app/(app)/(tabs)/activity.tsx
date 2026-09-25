import {
  ACTIVITY_EMPTY_COPY,
  ACTIVITY_FILTERS,
  activityActionRequest,
  activityRowParts,
  filterActivity,
  groupActivity,
  isNeedsYou,
  newestActivityAt,
  type ActivityAction,
  type ActivityActionInput,
  type ActivityFilter,
  type ActivityItem,
} from "@lightbulb/shared";
import { router, useIsFocused, useLocalSearchParams } from "expo-router";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Animated,
  Pressable,
  ScrollView,
  SectionList,
  StyleSheet,
  Text,
  View,
} from "react-native";

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
  highlighted,
  onAction,
}: {
  item: ActivityItem;
  busy: boolean;
  error: string | null;
  highlighted: boolean;
  onAction: (item: ActivityItem, action: ActivityAction) => void;
}) {
  const { summary } = item;
  const open = item.state === "open";
  const pinned = isNeedsYou(item);
  const parts = activityRowParts(item);
  const target = itemTarget(item);
  // Slack's shape: needs-you rows carry their decision inline; every other row is
  // one tap to open, and a long press offers its actions.
  const inline = pinned && item.actions.length > 0;
  const more = !pinned && open && item.actions.length > 0
    ? () => Alert.alert(summary.title, undefined, [
      ...item.actions.map((action) => ({ text: action.label, onPress: () => onAction(item, action) })),
      { text: "Cancel", style: "cancel" as const },
    ])
    : undefined;
  return (
    <View style={[styles.row, highlighted && styles.rowHighlighted]}>
      {/* The row body and its buttons are siblings, so VoiceOver reaches each
          button on its own instead of folding them into the row's label. */}
      <Pressable
        onPress={() => go(target)}
        onLongPress={more}
        disabled={!target && !more}
        accessibilityRole="button"
        accessibilityLabel={summary.title}
        accessibilityHint={target ? "Opens it" : undefined}
        accessibilityActions={more ? [{ name: "longpress", label: "More actions" }] : undefined}
        onAccessibilityAction={more ? () => more() : undefined}
        style={({ pressed }) => [styles.body, pressed && styles.pressed]}
      >
        <View style={styles.lead}>
          {item.read ? null : <View accessibilityLabel="Unread" style={[styles.dot, pinned && styles.dotPinned]} />}
          <Avatar name={item.actor.name} kind={item.actor.kind} size={32} />
        </View>
        <View style={styles.text}>
          <View style={styles.lineRow}>
            <Text style={[styles.title, !item.read && styles.titleUnread]} numberOfLines={2}>
              {parts.lead ? <Text style={styles.actor}>{parts.lead}</Text> : null}
              {parts.lead && !/^['’]/.test(parts.rest) ? " " : ""}
              {parts.rest}
              {parts.location ? <Text style={styles.where}>{` · ${parts.location}`}</Text> : null}
            </Text>
            <Text style={styles.time}>{relativeTime(Date.parse(item.createdAt))}</Text>
          </View>
          {parts.snippet ? (
            <Text style={[styles.snippet, pinned && styles.snippetPinned]} numberOfLines={pinned ? 2 : 1}>
              {parts.snippet}
            </Text>
          ) : null}
          {pinned && summary.preview?.destination ? (
            <Text style={styles.destination}>{`To ${summary.preview.destination}`}</Text>
          ) : null}
          {pinned && summary.preview?.text ? (
            <Text style={styles.preview} numberOfLines={4}>{summary.preview.text}</Text>
          ) : null}
          {pinned ? summary.preview?.fields.map((field) => (
            <Text key={field.label} style={styles.field}>
              <Text style={[styles.fieldLabel, field.label === "Recommended action" && styles.fieldWarn]}>{`${field.label}: `}</Text>
              {field.value}
            </Text>
          )) : null}
          {open ? null : <Text style={styles.resolved}>{resolvedLine(item)}</Text>}
        </View>
      </Pressable>
      {inline ? (
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

function Chips({ value, needsYou, onChange }: { value: ActivityFilter; needsYou: number; onChange: (f: ActivityFilter) => void }) {
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chips}>
      {ACTIVITY_FILTERS.map((chip) => {
        const on = chip.id === value;
        const count = chip.id === "needs-you" && needsYou > 0 ? needsYou : 0;
        return (
          <Pressable
            key={chip.id}
            onPress={() => onChange(chip.id)}
            accessibilityRole="button"
            accessibilityLabel={count ? `${chip.label}, ${count}` : chip.label}
            accessibilityState={{ selected: on }}
            style={[styles.chip, on && styles.chipOn]}
          >
            <Text style={[styles.chipText, on && styles.chipTextOn]}>{chip.label}</Text>
            {count ? <Text style={styles.chipCount}>{count}</Text> : null}
          </Pressable>
        );
      })}
    </ScrollView>
  );
}

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
  // A push tap lands here with the approval to show (`focus`); it shows under any chip.
  const { focus } = useLocalSearchParams<{ focus?: string }>();
  const [filter, setFilter] = useState<ActivityFilter>("all");
  const [focusedFor, setFocusedFor] = useState<string | null>(null);
  if (focus && focus !== focusedFor && items.some((entry) => entry.id === focus)) {
    setFocusedFor(focus);
    if (!filterActivity(items, filter).some((entry) => entry.id === focus)) setFilter("all");
  }
  const sections = useMemo(
    () => groupActivity(filterActivity(items, filter)).map((section) => ({ ...section, data: section.items })),
    [items, filter],
  );
  const needsYou = useMemo(() => items.filter(isNeedsYou).length, [items]);
  const listRef = useRef<SectionList<ActivityItem>>(null);
  useEffect(() => {
    if (!focusedFor) return;
    const sectionIndex = sections.findIndex((section) => section.data.some((entry) => entry.id === focusedFor));
    if (sectionIndex < 0) return;
    const itemIndex = sections[sectionIndex].data.findIndex((entry) => entry.id === focusedFor);
    try {
      listRef.current?.scrollToLocation({ sectionIndex, itemIndex, viewPosition: 0.3 });
    } catch {
      // Not laid out yet: the pinned section is at the top anyway.
    }
  }, [focusedFor, sections]);
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
      <SectionList
        ref={listRef}
        testID="activity-list"
        contentInsetAdjustmentBehavior="automatic"
        sections={sections}
        stickySectionHeadersEnabled
        keyExtractor={(item) => item.id}
        onScrollToIndexFailed={() => {}}
        renderSectionHeader={({ section }) => (
          <Text accessibilityRole="header" style={styles.sectionTitle}>
            {section.title}
            {section.key === "needs-you" ? <Text style={styles.sectionCount}>{`  ${section.data.length}`}</Text> : null}
          </Text>
        )}
        contentContainerStyle={[styles.list, sections.length === 0 && styles.listGrow]}
        refreshing={refreshing}
        onRefresh={refresh}
        onEndReached={activity.loadMore}
        onEndReachedThreshold={0.5}
        renderItem={({ item }) => (
          <ActivityRow item={item} busy={busy === item.id} error={errors.get(item.id) ?? null}
            highlighted={item.id === focusedFor} onAction={onAction} />
        )}
        ItemSeparatorComponent={() => <View style={styles.separator} />}
        ListHeaderComponent={
          <>
            <Chips value={filter} needsYou={needsYou} onChange={setFilter} />
            {activity.error && items.length > 0 ? (
              <Text accessibilityRole="alert" style={styles.headerError}>
                Couldn’t refresh activity: {activity.error.message}
              </Text>
            ) : null}
          </>
        }
        ListEmptyComponent={
          activity.loading && items.length === 0 ? (
            <Skeleton />
          ) : activity.error && items.length === 0 ? (
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
              <Text style={styles.dim}>{ACTIVITY_EMPTY_COPY[filter]}</Text>
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
  row: { paddingHorizontal: spacing.lg, paddingVertical: spacing.sm + 2, gap: spacing.sm },
  rowHighlighted: { backgroundColor: colors.rowActive },
  chips: { gap: spacing.xs + 2, paddingHorizontal: spacing.lg, paddingVertical: spacing.sm },
  chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    minHeight: 32,
    paddingHorizontal: spacing.md,
    borderRadius: 999,
    borderWidth: 1,
    borderColor: colors.edge,
  },
  chipOn: { backgroundColor: colors.ink, borderColor: colors.ink },
  chipText: { fontFamily: fonts.sansMedium, fontSize: 14, lineHeight: 19, color: colors.inkSoft },
  chipTextOn: { color: colors.cream },
  chipCount: {
    minWidth: 18,
    paddingHorizontal: 5,
    borderRadius: 9,
    overflow: "hidden",
    backgroundColor: colors.gold,
    color: colors.ink,
    fontFamily: fonts.sansSemiBold,
    fontSize: 11,
    lineHeight: 18,
    textAlign: "center",
  },
  sectionTitle: {
    fontFamily: fonts.sansSemiBold,
    fontSize: 13,
    lineHeight: 18,
    color: colors.inkSoft,
    backgroundColor: colors.cream,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.md,
    paddingBottom: spacing.xs,
  },
  sectionCount: { fontFamily: fonts.sans, color: colors.mute },
  lineRow: { flexDirection: "row", alignItems: "flex-start", gap: spacing.sm },
  actor: { fontFamily: fonts.sansSemiBold, color: colors.ink },
  where: { color: colors.mute },
  time: { ...type.caption, paddingTop: 1 },
  snippet: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 19, color: colors.mute },
  snippetPinned: { color: colors.inkSoft },
  dotPinned: { backgroundColor: colors.gold },
  fieldWarn: { color: colors.warn },
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
  title: { flex: 1, fontFamily: fonts.sans, fontSize: 15, lineHeight: 20, color: colors.inkSoft },
  titleUnread: { color: colors.ink },
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
  actions: { flexDirection: "row", flexWrap: "wrap", alignItems: "center", gap: spacing.sm, paddingLeft: 32 + spacing.md },
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
  rowError: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText, paddingLeft: 32 + spacing.md },
  separator: { height: StyleSheet.hairlineWidth, backgroundColor: colors.edge, marginLeft: spacing.lg + 32 + spacing.md },
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
