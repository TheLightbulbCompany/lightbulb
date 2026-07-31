import Feather from "@expo/vector-icons/Feather";
import { Stack, useLocalSearchParams } from "expo-router";
import {
  Animated,
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { usePulse } from "../../../../components/chat/usePulse";
import { colors, fonts, radii, spacing, type } from "../../../../config/theme";
import { timeAgo } from "../../../../lib/teamsData";
import {
  isActiveRunStatus,
  RUN_EVENTS_FETCH_LIMIT,
  useRunDetail,
  type RunDetail,
} from "../../../../lib/teamsRunDetail";
import { teamsDetailUrl } from "../../../../lib/webLinks";

// Run-detail screen (web RunDetailPanel parity): status + actions
// (Cancel / Retry / Resume), error surfaces, transcript (bounded tail of the
// run log), event timeline, and the metadata props. Polls while active.


// Bound what one RN Text renders — a 256KiB transcript in a single Text is a
// jank risk the web's scrollable <pre> doesn't have. The tail is what matters.
const TRANSCRIPT_MAX_LINES = 400;
const EVENTS_MAX_ROWS = 200;

function statusColor(status: string | null | undefined): string {
  const s = (status ?? "").toLowerCase();
  if (s === "succeeded" || s === "done" || s === "completed") return colors.forest;
  if (s === "failed" || s === "error" || s === "cancelled" || s === "timed_out")
    return colors.danger;
  if (s === "running" || s === "queued" || s === "scheduled_retry") return colors.forest;
  return colors.mute;
}

function usageNumber(
  u: Record<string, unknown> | null | undefined,
  ...keys: string[]
): number | null {
  if (!u) return null;
  for (const k of keys) {
    const v = u[k];
    if (typeof v === "number" && Number.isFinite(v)) return v;
  }
  return null;
}

function fmtWhen(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = new Date(iso);
  return Number.isNaN(t.getTime()) ? null : `${t.toLocaleString()} (${timeAgo(iso)})`;
}

function fmtDuration(start?: string | null, end?: string | null): string | null {
  if (!start || !end) return null;
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function Prop({ label, value }: { label: string; value: string | null | undefined }) {
  if (value == null || value === "") return null;
  return (
    <View style={styles.prop}>
      <Text style={styles.propLabel}>{label}</Text>
      <Text style={styles.propValue}>{value}</Text>
    </View>
  );
}

function ActionButton({
  label,
  onPress,
  disabled,
}: {
  label: string;
  onPress: () => void;
  disabled?: boolean;
}) {
  return (
    <Pressable
      style={({ pressed }) => [
        styles.actionButton,
        (pressed || disabled) && styles.actionButtonPressed,
      ]}
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={label}
    >
      <Text style={styles.actionButtonText}>{label}</Text>
    </Pressable>
  );
}

function LoadingState() {
  const opacity = usePulse(true, 0.3);
  return (
    <View style={styles.centered}>
      <Animated.View style={[styles.activeDot, { opacity }]} />
      <Text style={styles.stateTitle}>Loading run…</Text>
    </View>
  );
}

function usageLine(run: RunDetail): string | null {
  const usage = run.usageJson ?? null;
  const input = usageNumber(usage, "inputTokens", "input_tokens");
  const output = usageNumber(usage, "outputTokens", "output_tokens");
  const cached = usageNumber(
    usage,
    "cachedInputTokens",
    "cached_input_tokens",
    "cache_read_input_tokens",
  );
  if (input === null && output === null) return null;
  return `${(input ?? 0).toLocaleString()} in / ${(output ?? 0).toLocaleString()} out${
    cached ? ` (${cached.toLocaleString()} cached)` : ""
  }`;
}

export default function RunDetailScreen() {
  const { runId } = useLocalSearchParams<{ runId: string }>();
  const { state, active, acting, actionError, act, retry } = useRunDetail(runId);

  if (state.kind === "loading") {
    return (
      <View style={styles.page}>
        <LoadingState />
      </View>
    );
  }

  if (state.kind === "gate") {
    const isProvisioning = state.gate === "provisioning";
    const isSubscription = state.gate === "needs_subscription";
    return (
      <View style={[styles.page, styles.centered]}>
        <Feather
          name={isSubscription ? "lock" : "cloud-off"}
          size={28}
          color={colors.mute}
        />
        <Text style={styles.stateTitle}>
          {isProvisioning
            ? "Setting up your workspace…"
            : isSubscription
              ? "Teams requires an active subscription"
              : "Couldn’t reach your workspace"}
        </Text>
        {state.message ? <Text style={styles.stateDetail}>{state.message}</Text> : null}
        {isSubscription ? (
          <ActionButton
            label="Manage on isol8.co"
            onPress={() => void Linking.openURL(teamsDetailUrl("run", runId, "issues"))}
          />
        ) : (
          <ActionButton label="Retry" onPress={() => void retry()} />
        )}
      </View>
    );
  }

  if (state.kind === "error") {
    return (
      <View style={[styles.page, styles.centered]}>
        <Feather name="alert-circle" size={28} color={colors.danger} />
        <Text style={styles.stateTitle}>Failed to load run</Text>
        <Text style={styles.stateDetail}>{state.message}</Text>
        <ActionButton label="Retry" onPress={() => void retry()} />
      </View>
    );
  }

  const { run, transcriptLines, transcriptTruncated, events, agents } = state;
  const agentName =
    agents.find((a) => a.id === run.agentId)?.name ?? run.agentId ?? "Agent";
  const statusLabel = (run.status ?? "unknown").replace(/_/g, " ");
  const s = (run.status ?? "").toLowerCase();
  const canCancel = isActiveRunStatus(run.status);
  const canRetry = (s === "failed" || s === "timed_out") && !!run.agentId;
  const canResume = s === "failed" && run.errorCode === "process_lost" && !!run.agentId;
  const busy = acting !== null;
  const shownTranscript = transcriptLines.slice(-TRANSCRIPT_MAX_LINES);
  const shownEvents = events.slice(-EVENTS_MAX_ROWS);
  const trigger = [run.invocationSource, run.triggerDetail].filter(Boolean).join(" · ");

  return (
    <ScrollView style={styles.page} contentContainerStyle={styles.content}>
      <Stack.Screen options={{ title: `Run — ${agentName}` }} />

      <View style={styles.headerBlock}>
        <View style={styles.statusRow}>
          <Text style={[styles.status, { color: statusColor(run.status) }]}>
            {statusLabel}
          </Text>
          {run.startedAt ? (
            <Text style={styles.metaText}>started {timeAgo(run.startedAt)}</Text>
          ) : null}
          {active ? <ActivePulse /> : null}
        </View>
        {(canCancel || canRetry || canResume) && (
          <View style={styles.actionRow}>
            {canCancel && (
              <ActionButton
                label={acting === "cancel" ? "Cancelling…" : "Cancel run"}
                onPress={() => void act("cancel")}
                disabled={busy}
              />
            )}
            {canResume && (
              <ActionButton
                label={acting === "resume" ? "Resuming…" : "Resume run"}
                onPress={() => void act("resume")}
                disabled={busy}
              />
            )}
            {canRetry && (
              <ActionButton
                label={acting === "retry" ? "Retrying…" : "Retry"}
                onPress={() => void act("retry")}
                disabled={busy}
              />
            )}
          </View>
        )}
        {actionError ? <Text style={styles.actionError}>{actionError}</Text> : null}
      </View>

      {(run.error || run.retryExhaustedReason || run.stderrExcerpt) && (
        <View style={styles.errorCard}>
          {run.error ? <Text style={styles.errorText}>{run.error}</Text> : null}
          {run.retryExhaustedReason ? (
            <Text style={styles.errorText}>{run.retryExhaustedReason}</Text>
          ) : null}
          {run.stderrExcerpt ? (
            <Text style={styles.stderrText}>{run.stderrExcerpt}</Text>
          ) : null}
        </View>
      )}

      <View style={styles.section}>
        <Text style={styles.overline}>TRANSCRIPT</Text>
        {transcriptTruncated && (
          <Text style={styles.notice}>
            Log is larger than one read — showing the last 256 KiB.
          </Text>
        )}
        {transcriptLines.length > TRANSCRIPT_MAX_LINES && (
          <Text style={styles.notice}>
            Showing the last {TRANSCRIPT_MAX_LINES} lines.
          </Text>
        )}
        {shownTranscript.length === 0 ? (
          <Text style={styles.emptyText}>No transcript captured.</Text>
        ) : (
          <View style={styles.transcriptBlock}>
            <Text style={styles.transcriptText} selectable>
              {shownTranscript.join("\n")}
            </Text>
          </View>
        )}
      </View>

      <View style={styles.section}>
        <Text style={styles.overline}>EVENTS</Text>
        {/* The fetch returns the run's FIRST 1000 events (afterSeq=0, seq
            ascending) — a capped batch has no true tail, so the notices must
            not claim recency the data doesn't have. */}
        {events.length >= RUN_EVENTS_FETCH_LIMIT && (
          <Text style={styles.notice}>
            Long run — showing only the first {RUN_EVENTS_FETCH_LIMIT.toLocaleString()} events.
          </Text>
        )}
        {events.length > EVENTS_MAX_ROWS && (
          <Text style={styles.notice}>
            Displaying the last {EVENTS_MAX_ROWS} of the fetched events.
          </Text>
        )}
        {shownEvents.length === 0 ? (
          <Text style={styles.emptyText}>No events recorded.</Text>
        ) : (
          shownEvents.map((e) => (
            <View key={e.seq} style={styles.eventRow}>
              <Text
                style={[
                  styles.eventType,
                  e.level === "error"
                    ? { color: colors.danger }
                    : e.level === "warn"
                      ? { color: colors.warn }
                      : null,
                ]}
              >
                {e.eventType ?? "event"}
              </Text>
              <Text style={styles.eventMessage} numberOfLines={2}>
                {e.message ?? ""}
              </Text>
              {e.createdAt ? (
                <Text style={styles.eventWhen}>{timeAgo(e.createdAt)}</Text>
              ) : null}
            </View>
          ))
        )}
      </View>

      <View style={styles.section}>
        <Text style={styles.overline}>DETAILS</Text>
        <Prop label="Agent" value={agentName} />
        <Prop label="Trigger" value={trigger || null} />
        <Prop label="Started" value={fmtWhen(run.startedAt)} />
        <Prop label="Finished" value={fmtWhen(run.finishedAt)} />
        <Prop label="Duration" value={fmtDuration(run.startedAt, run.finishedAt)} />
        <Prop label="Usage" value={usageLine(run)} />
        <Prop
          label="Model"
          value={
            [
              typeof run.usageJson?.provider === "string" ? run.usageJson.provider : null,
              typeof run.usageJson?.model === "string" ? run.usageJson.model : null,
            ]
              .filter(Boolean)
              .join(" · ") || null
          }
        />
        <Prop label="Error code" value={run.errorCode} />
        <Prop
          label="Exit"
          value={typeof run.exitCode === "number" ? String(run.exitCode) : null}
        />
        <Prop label="Retry of" value={run.retryOfRunId} />
        <Prop
          label="Liveness"
          value={[run.livenessState, run.livenessReason].filter(Boolean).join(" — ") || null}
        />
        <Prop label="Run id" value={run.id} />
      </View>
    </ScrollView>
  );
}

function ActivePulse() {
  const opacity = usePulse(true, 0.3);
  return (
    <Animated.View
      accessibilityLabel="Run is active"
      style={[styles.activeDot, { opacity }]}
    />
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  content: {
    paddingHorizontal: spacing.md,
    paddingTop: spacing.md,
    paddingBottom: spacing.xl,
    gap: spacing.lg,
  },
  centered: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    gap: spacing.md,
    padding: spacing.xl,
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

  headerBlock: { gap: spacing.md },
  statusRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  status: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 15,
    lineHeight: 20,
    textTransform: "capitalize",
  },
  metaText: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.mute,
  },
  activeDot: {
    width: 7,
    height: 7,
    borderRadius: 4,
    backgroundColor: colors.forest,
  },
  actionRow: { flexDirection: "row", flexWrap: "wrap", gap: spacing.sm },
  actionButton: {
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.sm,
    backgroundColor: colors.white,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
  },
  actionButtonPressed: { opacity: 0.6 },
  actionButtonText: {
    fontFamily: fonts.dmSansMedium,
    fontSize: 13,
    lineHeight: 18,
    color: colors.forest,
  },
  actionError: {
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.danger,
  },

  errorCard: {
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderColor: colors.errorBorder,
    borderRadius: radii.md,
    padding: spacing.md,
    gap: spacing.xs,
  },
  errorText: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
  },
  stderrText: {
    fontFamily: fonts.mono,
    fontSize: 11,
    lineHeight: 15,
    color: colors.mute,
  },

  section: { gap: spacing.sm },
  overline: { ...type.overline },
  notice: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 15,
    fontStyle: "italic",
    color: colors.mute,
  },
  emptyText: {
    fontFamily: fonts.dmSans,
    fontSize: 13,
    lineHeight: 18,
    fontStyle: "italic",
    color: colors.mute,
  },
  transcriptBlock: {
    backgroundColor: colors.white,
    borderWidth: 1,
    borderColor: colors.edge,
    borderRadius: radii.md,
    padding: spacing.md,
  },
  transcriptText: {
    fontFamily: fonts.mono,
    fontSize: 11,
    lineHeight: 16,
    color: colors.ink,
  },

  eventRow: {
    flexDirection: "row",
    alignItems: "baseline",
    gap: spacing.sm,
    paddingVertical: spacing.xs,
    borderBottomWidth: 1,
    borderBottomColor: colors.edge,
  },
  eventType: {
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 14,
    letterSpacing: 0.5,
    textTransform: "uppercase",
    color: colors.mute,
  },
  eventMessage: {
    flex: 1,
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.ink,
  },
  eventWhen: {
    fontFamily: fonts.dmSans,
    fontSize: 11,
    lineHeight: 14,
    color: colors.mute,
  },

  prop: {
    flexDirection: "row",
    gap: spacing.md,
    paddingVertical: spacing.xs,
    borderBottomWidth: 1,
    borderBottomColor: colors.edge,
  },
  propLabel: {
    width: 88,
    fontFamily: fonts.mono,
    fontSize: 10,
    lineHeight: 16,
    letterSpacing: 1,
    textTransform: "uppercase",
    color: colors.mute,
  },
  propValue: {
    flex: 1,
    fontFamily: fonts.dmSans,
    fontSize: 12,
    lineHeight: 16,
    color: colors.ink,
  },
});
