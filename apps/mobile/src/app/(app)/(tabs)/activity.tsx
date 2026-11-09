import {
  ACTIVITY_EMPTY_COPY,
  ACTIVITY_FILTERS,
  activityActionRequest,
  activityRowParts,
  agentRingState,
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
import { CredentialSheet, type CredentialField } from "../../../components/ui/CredentialSheet";
import { InsetSectionHeader, insetRowStyle } from "../../../components/ui/InsetGroup";
import { ListRow } from "../../../components/ui/ListRow";
import { fonts, spacing, useChatPalette, type ChatPalette } from "../../../config/theme";
import { activityHrefs, itemTarget, useMobileActivity } from "../../../lib/activity";
import { purchaseSafeMessage } from "../../../lib/purchaseCta";
import { relativeTime } from "../../../lib/relativeTime";
import { useWorkspaceRoster } from "../../../lib/workspaceRoster";

const LOGIN_FIELDS: readonly CredentialField[] = ["username", "password"];

const ACTION_FAILED = "That didn’t go through. Try again.";

const MOBILE_FILTER_IDS: readonly ActivityFilter[] = ["all", "needs-you", "mentions"];

/** Mobile shows three of the shared filters. */
export const MOBILE_ACTIVITY_FILTERS: readonly { id: ActivityFilter; label: string }[] =
  ACTIVITY_FILTERS.filter((filter) => MOBILE_FILTER_IDS.includes(filter.id));

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

/** What a row can do besides going somewhere: approve, reply, retry. */
const rowActions = (item: ActivityItem): ActivityAction[] =>
  item.state === "open" ? item.actions.filter((action) => !action.navigate) : [];

/**
 * The one button a row carries: Review (something to decide), Open (somewhere
 * to go), or none.
 *
 * Review is for what waits on the reader -- an approval, an agent's question, a
 * failed run. A message has a Reply action too, and counting that made every
 * direct message ask for a review; it gets Open, and its Reply is one long
 * press away. Only an item with nowhere to go falls back to Review.
 */
export function rowButton(
  item: ActivityItem,
):
  | { kind: "review"; actions: ActivityAction[] }
  | { kind: "open"; target: NonNullable<ActivityAction["navigate"]> }
  | null {
  if (item.state !== "open") return null;
  const actions = rowActions(item);
  if (isNeedsYou(item) && actions.length > 0) return { kind: "review", actions };
  const target = itemTarget(item);
  if (target) return { kind: "open", target };
  return actions.length > 0 ? { kind: "review", actions } : null;
}

/** The Review sheet's message: the item's why and payload, verbatim, one per line. */
export function reviewMessage(item: ActivityItem): string | undefined {
  const { summary } = item;
  return (
    [
      summary.reason,
      summary.preview?.text,
      summary.preview?.destination ? `To ${summary.preview.destination}` : null,
      ...(summary.preview?.fields ?? []).map((field) => `${field.label}: ${field.value}`),
    ]
      .filter(Boolean)
      .join("\n") || undefined
  );
}

/** The row's decision surface: its why/payload verbatim, then one button per action, then Open/Cancel. */
function openReviewSheet(
  item: ActivityItem,
  actions: ActivityAction[],
  target: NonNullable<ActivityAction["navigate"]> | null,
  onAction: (item: ActivityItem, action: ActivityAction) => void,
) {
  Alert.alert(item.summary.title, reviewMessage(item), [
    ...actions.map((action) => ({
      text: action.label,
      style: action.style === "destructive" ? ("destructive" as const) : ("default" as const),
      onPress: () => onAction(item, action),
    })),
    ...(target ? [{ text: "Open", onPress: () => go(target) }] : []),
    { text: "Cancel", style: "cancel" as const },
  ]);
}

type Pending = { item: ActivityItem; action: ActivityAction & { input: ActivityActionInput } };

const ActivityRow = memo(function ActivityRow({
  item,
  index,
  count,
  busy,
  error,
  highlighted,
  palette,
  agentStatus,
  onAction,
}: {
  item: ActivityItem;
  index: number;
  count: number;
  busy: boolean;
  error: string | null;
  highlighted: boolean;
  palette: ChatPalette;
  agentStatus: string | null | undefined;
  onAction: (item: ActivityItem, action: ActivityAction) => void;
}) {
  const parts = activityRowParts(item);
  const target = itemTarget(item);
  const button = rowButton(item);
  // A title of the shape "Name: subject" collapses to a bare ":" rest for a
  // non-approval subtype (activityRowParts) -- the subject moved into the
  // snippet, so that is what the row shows instead of a stray colon.
  const colon = parts.rest === ":";
  const location = parts.location ? ` · ${parts.location}` : "";
  const preview = colon
    ? (parts.snippet ?? undefined)
    : parts.lead
      ? `${parts.rest.trimStart()}${location}`
      : `${item.summary.title}${location}`;
  const detail = colon ? null : item.state === "open" ? parts.snippet : resolvedLine(item);
  // Busy: the action is already in flight (a new idempotency key per call
  // means a second submit is a duplicate, not a retry), so long-press, its
  // accessibility action and the press fallback below must not reopen it —
  // the trailing button already swaps to a spinner.
  const actions = rowActions(item);
  const openReview = !busy && actions.length > 0 ? () => openReviewSheet(item, actions, target, onAction) : undefined;
  // A row with review actions but no navigation target (e.g. a website-login
  // request with no room/thread) falls back to the same action as its own
  // trailing button: open the Review sheet. With neither, the row isn't pressable.
  const onRowPress = target != null ? () => go(target) : openReview;
  const ring = item.actor.kind === "agent" ? agentRingState(agentStatus) : null;
  // A coding session's actor is its tool ("Claude"/"Codex"): show the tool's mark,
  // and title the row by the session ("Claude · Fix X" → "Fix X").
  const session = item.kind === "session";
  const vendor = session ? item.actor.name.toLowerCase() : undefined;
  const sessionTitle = session && parts.lead ? parts.rest.replace(/^·\s*/, "").trim() : null;

  return (
    <View style={insetRowStyle(palette, index, count)}>
      <ListRow
        palette={palette}
        leading={<Avatar name={item.actor.name} kind={item.actor.kind} size={36} palette={palette} ring={ring} vendor={vendor} />}
        title={sessionTitle || (parts.lead ?? item.actor.name)}
        preview={sessionTitle ? (parts.snippet ?? undefined) : preview}
        previewLines={2}
        detail={sessionTitle ? null : detail}
        time={relativeTime(Date.parse(item.createdAt))}
        unread={!item.read}
        highlighted={highlighted}
        accessibilityLabel={item.summary.title}
        onPress={onRowPress}
        onLongPress={openReview}
        accessibilityActions={openReview ? [{ name: "longpress", label: "More actions" }] : undefined}
        onAccessibilityAction={openReview ? () => openReview() : undefined}
        trailing={
          button ? (
            busy ? (
              <ActivityIndicator color={palette.fgMute} />
            ) : button.kind === "review" ? (
              <Pressable
                onPress={() => openReviewSheet(item, button.actions, target, onAction)}
                hitSlop={6}
                accessibilityRole="button"
                accessibilityLabel={`Review: ${item.summary.title}`}
                style={[styles.pill, { backgroundColor: palette.fg }]}
              >
                <Text style={[styles.pillText, { color: palette.onFg }]}>Review</Text>
              </Pressable>
            ) : (
              <Pressable
                onPress={() => go(button.target)}
                hitSlop={6}
                accessibilityRole="button"
                accessibilityLabel={`Open: ${item.summary.title}`}
                style={[styles.pill, { backgroundColor: palette.chip }]}
              >
                <Text style={[styles.pillText, { color: palette.fg }]}>Open</Text>
              </Pressable>
            )
          ) : null
        }
      />
      {error ? (
        <Text accessibilityRole="alert" style={[styles.rowError, { color: palette.alert }]}>
          {error}
        </Text>
      ) : null}
    </View>
  );
});

function Chips({
  value,
  needsYou,
  palette,
  onChange,
}: {
  value: ActivityFilter;
  needsYou: number;
  palette: ChatPalette;
  onChange: (f: ActivityFilter) => void;
}) {
  return (
    <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.chips}>
      {MOBILE_ACTIVITY_FILTERS.map((chip) => {
        const on = chip.id === value;
        const count = chip.id === "needs-you" && needsYou > 0 ? needsYou : 0;
        return (
          <Pressable
            key={chip.id}
            onPress={() => onChange(chip.id)}
            accessibilityRole="button"
            accessibilityLabel={count ? `${chip.label}, ${count}` : chip.label}
            accessibilityState={{ selected: on }}
            style={[styles.chip, { backgroundColor: on ? palette.fg : palette.chip }]}
          >
            <Text style={[styles.chipText, { color: on ? palette.onFg : palette.fg }]}>{chip.label}</Text>
            {count ? (
              <Text style={[styles.chipCount, { backgroundColor: palette.alert, color: palette.onFg }]}>{count}</Text>
            ) : null}
          </Pressable>
        );
      })}
    </ScrollView>
  );
}

/** Placeholder rows shaped like the real ones, so the first paint does not jump. */
function Skeleton({ palette }: { palette: ChatPalette }) {
  const pulse = usePulse(true, 0.55);
  return (
    <View testID="activity-skeleton">
      {[0, 1, 2, 3, 4].map((i) => (
        <Animated.View key={i} style={[styles.skeletonRow, { opacity: pulse }]}>
          <View style={[styles.skeletonAvatar, { backgroundColor: palette.raised }]} />
          <View style={styles.skeletonText}>
            <View style={[styles.skeletonLine, { width: "80%", backgroundColor: palette.raised }]} />
            <View style={[styles.skeletonLine, { width: "45%", backgroundColor: palette.raised }]} />
          </View>
        </Animated.View>
      ))}
    </View>
  );
}

/**
 * Activity: what needs you — mentions, DMs, thread replies, approvals and
 * issues assigned to you, newest first. Every word and button is the
 * server's (`summary`, `actions`); this screen renders them generically and
 * never composes text per kind.
 */
/** Every finished turn of a coding session is its own feed item (the backend
 * sends them `open`), so show only the newest one per session; a session
 * "waiting" on the owner always stays, and so does a teammate's ask to join,
 * which would otherwise hide behind the session finishing. Assumes the feed
 * is newest-first, as useActivity returns it. */
function latestPerSession(items: readonly ActivityItem[]): ActivityItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (item.kind !== "session" || item.subtype === "waiting" || item.subtype === "join_request") return true;
    const id = item.summary.links.find((link) => link.kind === "session")?.id ?? item.id;
    if (seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

export default function Activity() {
  const palette = useChatPalette();
  const focused = useIsFocused();
  const activity = useMobileActivity(focused);
  const { items, markSeen, runAction } = activity;
  // A push tap lands here with the approval to show (`focus`); it shows under any chip.
  const { focus } = useLocalSearchParams<{ focus?: string }>();
  const [filter, setFilter] = useState<ActivityFilter>("needs-you");
  const [focusedFor, setFocusedFor] = useState<string | null>(null);
  if (focus && focus !== focusedFor && items.some((entry) => entry.id === focus)) {
    setFocusedFor(focus);
    if (!filterActivity(items, filter).some((entry) => entry.id === focus)) setFilter("all");
  }
  const sections = useMemo(
    () => groupActivity(filterActivity(latestPerSession(items), filter)).map((section) => ({ ...section, data: section.items })),
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
  // Feeds both the person picker and the row rings, so the roster polls
  // whenever the tab is on screen, not only while a picker is open.
  // ponytail: roster poll feeds Activity rings; drop focus-gating if the BFF load matters.
  const { agents, members } = useWorkspaceRoster(pending?.action.input.kind === "person" || focused);
  const agentsById = useMemo(() => new Map(agents.map((agent) => [agent.id, agent])), [agents]);
  const rosterOptions: PickerOption[] = useMemo(
    () => [
      ...agents.map((agent) => ({ key: agent.id, label: agent.name || "Agent", section: "Agents" })),
      ...members.map((member) => ({
        key: member.userId,
        label: member.name || member.email || "Workspace member",
        section: "People",
      })),
    ],
    [agents, members],
  );

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

  // An agent's "add the sign-in here" link lands with `focus=login:<id>`: open its form once.
  const openedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!focusedFor || openedFor.current === focusedFor) return;
    openedFor.current = focusedFor;
    const item = items.find((entry) => entry.id === focusedFor);
    const action = item?.state === "open" ? item.actions.find((entry) => entry.input?.kind === "login") : undefined;
    if (item && action) onAction(item, action);
  }, [focusedFor, items, onAction]);

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
    <View style={[styles.page, { backgroundColor: palette.ground }]}>
      <SectionList
        ref={listRef}
        testID="activity-list"
        contentInsetAdjustmentBehavior="automatic"
        sections={sections}
        stickySectionHeadersEnabled={false}
        keyExtractor={(item) => item.id}
        onScrollToIndexFailed={() => {}}
        renderSectionHeader={({ section }) => (
          <InsetSectionHeader
            title={section.title}
            count={section.key === "needs-you" ? section.data.length : undefined}
            palette={palette}
          />
        )}
        contentContainerStyle={[styles.list, sections.length === 0 && styles.listGrow]}
        refreshing={refreshing}
        onRefresh={refresh}
        onEndReached={activity.loadMore}
        onEndReachedThreshold={0.5}
        renderItem={({ item, index, section }) => (
          <ActivityRow
            item={item}
            index={index}
            count={section.data.length}
            busy={busy === item.id}
            error={errors.get(item.id) ?? null}
            highlighted={item.id === focusedFor}
            palette={palette}
            agentStatus={item.actor.kind === "agent" ? agentsById.get(item.actor.id)?.status : undefined}
            onAction={onAction}
          />
        )}
        ListHeaderComponent={
          <>
            <Chips value={filter} needsYou={needsYou} palette={palette} onChange={setFilter} />
            {activity.error && items.length > 0 ? (
              <Text accessibilityRole="alert" style={[styles.headerError, { color: palette.alert }]}>
                Couldn’t refresh activity: {activity.error.message}
              </Text>
            ) : null}
          </>
        }
        ListEmptyComponent={
          activity.loading && items.length === 0 ? (
            <Skeleton palette={palette} />
          ) : activity.error && items.length === 0 ? (
            <View style={[styles.errorCard, { backgroundColor: palette.raised, borderColor: palette.edge }]}>
              <Text selectable style={[styles.errorText, { color: palette.alert }]}>
                Couldn’t load activity: {activity.error.message}
              </Text>
              <Pressable onPress={() => void activity.refresh().catch(() => {})} accessibilityRole="button" accessibilityLabel="Retry">
                <Text style={[styles.retryText, { color: palette.alert }]}>Retry</Text>
              </Pressable>
            </View>
          ) : (
            <View style={styles.empty}>
              <LogoMark size={48} />
              <Text style={[styles.dim, { color: palette.fgMute }]}>{ACTIVITY_EMPTY_COPY[filter]}</Text>
            </View>
          )
        }
        ListFooterComponent={
          <>
            {activity.loadingMore ? <ActivityIndicator style={styles.more} color={palette.fgMute} /> : null}
            {activity.paperclipAvailable ? null : (
              <Text style={[styles.footnote, { color: palette.fgMute }]}>
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
      {/* The same answer the web's SecretForm posts: `value` only, straight to the workspace's website sign-ins. */}
      <CredentialSheet
        visible={input?.kind === "login"}
        title={pending?.item.summary.title ?? input?.label ?? ""}
        hint={input?.hint}
        fields={LOGIN_FIELDS}
        submitLabel={pending?.action.label ?? "Add"}
        onSubmit={({ username, password }) => answer(JSON.stringify({ username, password }))}
        onClose={() => setPending(null)}
      />
      <PickerSheet
        visible={input?.kind === "select" || input?.kind === "person"}
        heading={input?.label ?? ""}
        subheading={pending?.item.summary.title ?? null}
        options={input?.kind === "select" ? (input.options ?? []).map((o) => ({ key: o.value, label: o.label })) : rosterOptions}
        onSelect={answer}
        onClose={() => setPending(null)}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1 },
  list: { paddingBottom: spacing.xl },
  listGrow: { flexGrow: 1 },
  chips: { gap: spacing.xs + 2, paddingHorizontal: spacing.lg, paddingVertical: spacing.sm },
  chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    minHeight: 32,
    paddingHorizontal: spacing.md,
    borderRadius: 999,
  },
  chipText: { fontFamily: fonts.sansMedium, fontSize: 14, lineHeight: 19 },
  chipCount: {
    minWidth: 18,
    paddingHorizontal: 5,
    borderRadius: 9,
    overflow: "hidden",
    fontFamily: fonts.sansSemiBold,
    fontSize: 11,
    lineHeight: 18,
    textAlign: "center",
  },
  pill: {
    minHeight: 32,
    justifyContent: "center",
    alignItems: "center",
    paddingHorizontal: spacing.md,
    borderRadius: 999,
  },
  pillText: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18 },
  rowError: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, paddingHorizontal: 20, paddingBottom: spacing.sm },
  headerError: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    paddingHorizontal: spacing.lg,
    paddingTop: spacing.sm,
  },
  errorCard: { margin: spacing.lg, padding: spacing.lg, gap: spacing.sm, borderWidth: 1, borderRadius: 12 },
  errorText: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 22 },
  retryText: { fontFamily: fonts.sansSemiBold, fontSize: 14, marginTop: spacing.sm },
  empty: { flex: 1, alignItems: "center", justifyContent: "center", gap: spacing.sm, padding: spacing.xl },
  dim: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 22, textAlign: "center" },
  more: { paddingVertical: spacing.md },
  footnote: {
    fontFamily: fonts.sans,
    fontSize: 12,
    lineHeight: 16,
    textAlign: "center",
    paddingHorizontal: spacing.xl,
    paddingTop: spacing.md,
  },
  skeletonRow: { flexDirection: "row", gap: spacing.md, paddingHorizontal: 20, paddingVertical: 10 },
  skeletonAvatar: { width: 36, height: 36, borderRadius: 12 },
  skeletonText: { flex: 1, gap: 2, justifyContent: "center" },
  skeletonLine: { height: 12, borderRadius: 4, marginVertical: 3 },
});
