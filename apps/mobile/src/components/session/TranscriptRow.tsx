import {
  PREVIEW_LINES, buildRows, groupRows, lifecycleLabel, subagentLabel, subagentStatus, subagentTags, thinkingLabel, toolHeader, toolRowView,
  type LocalSessionEvent, type LocalSessionProvider, type SessionSubagent, type SessionReceiptChip, type ThinkingTranscriptRow, type ToolTranscriptRow, type TranscriptItem,
} from "@lightbulb/shared";
import { memo, useEffect, useState } from "react";
import { StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing, useChatPalette, type ChatPalette } from "../../config/theme";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { Bubble, BubbleMeta, Chip, MetaText, WorkingBubble } from "../conversation/bubbles";

/** HH:MM in the reader's locale; null when the event carries no usable timestamp. */
function time(ts?: string): string | null {
  const d = ts ? new Date(ts) : null;
  return d && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null;
}

/**
 * One item of a shared session, in the bubble/chip language the redesigned
 * room and thread screens use — the phone twin of the web NativeSessionTranscript.
 * `name` resolves a prompt's author: null is the session owner, otherwise a room
 * member's id. `isMine` says whether a `prompt` row was written by the person
 * looking at this screen (default: the owner viewing their own session — a
 * teammate viewing passes `(who) => who === viewerId`). `live` marks the last
 * row of a still-running turn (a live thinking block reads "Thinking…").
 */
export const TranscriptRowView = memo(function TranscriptRowView({ provider, row, name, live = false, events = [], status, isMine = (who) => who === null }: {
  provider: LocalSessionProvider;
  row: TranscriptItem;
  name: (who: string | null) => string;
  live?: boolean;
  /** Every loaded event: a subagent's row opens onto its own rows from them. */
  events?: readonly LocalSessionEvent[];
  /** The session's status: a subagent with no end reads "Ended" once its session is not working. */
  status?: string;
  /** True when this prompt was written by the viewer (owner viewing: who === null; teammate: who === viewerId). */
  isMine?: (who: string | null) => boolean;
}) {
  const palette = useChatPalette();
  switch (row.kind) {
    case "tools":
      return <ToolGroup provider={provider} rows={row.rows} summary={row.summary} lines={row.lines} />;
    case "prompt": {
      const mine = isMine(row.who);
      const authorName = mine ? "You" : name(row.who);
      const at = time(row.ts);
      return (
        <View style={styles.row}>
          {!mine && (
            <Text style={[styles.authorName, { color: palette.fgMute }]} numberOfLines={1}>
              {authorName}
            </Text>
          )}
          <Bubble mine={mine} palette={palette} accessibilityLabel={`${authorName}: ${row.text}`}>
            <MessageMarkdown text={row.text} palette={palette} inverse={mine} />
          </Bubble>
          {at ? (
            <BubbleMeta align={mine ? "end" : "start"} palette={palette}>
              <MetaText palette={palette}>{at}</MetaText>
            </BubbleMeta>
          ) : null}
        </View>
      );
    }
    case "prose": {
      const final = provider === "codex" && row.final;
      const author = provider === "codex" ? "Codex" : "Claude";
      return (
        <View style={styles.row}>
          <Bubble mine={false} palette={palette} accessibilityLabel={`${author}: ${row.text}`}>
            <MessageMarkdown text={row.text} palette={palette} />
          </Bubble>
          {final ? (
            <BubbleMeta align="start" palette={palette}>
              <MetaText palette={palette}>final answer</MetaText>
            </BubbleMeta>
          ) : null}
        </View>
      );
    }
    case "thinking":
      return <ThinkingRow provider={provider} row={row} live={live} />;
    case "tool":
      return <ToolRow provider={provider} row={row} />;
    case "notice":
      return <Text style={[styles.centerMeta, { color: palette.fgMute }]}>{row.text}</Text>;
    case "agent":
      return <AgentRow provider={provider} subagent={row.subagent} events={events} status={status} />;
    case "subagent":
      return <Text style={[styles.centerMeta, { color: palette.fgMute }]}>{subagentLabel(row)}</Text>;
    case "lifecycle": {
      const label = lifecycleLabel(row);
      return label ? <Text style={[styles.centerMeta, { color: palette.fgMute }]}>{label}</Text> : null;
    }
  }
});

/**
 * A subagent inline where it was launched: a "person.2" chip naming it and its
 * live status ("Agent · Fix login — Running · 2m 14s"); tapping opens its own
 * rows nested under it. A subagent with no id (older history, never mirrored)
 * draws the same chip with nothing to expand.
 */
function AgentRow({ provider, subagent: s, events, status }: {
  provider: LocalSessionProvider; subagent: SessionSubagent; events: readonly LocalSessionEvent[]; status?: string;
}) {
  const palette = useChatPalette();
  const [open, setOpen] = useState(false);
  const running = s.state === "running";
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running]);
  const description = s.description || "subagent";
  const label = [provider === "codex" ? `Spawned ${description}` : `Agent · ${description}`, ...subagentTags(s)].join(" · ");
  const rows = open ? groupRows(buildRows(events, s.id), false, provider) : [];
  return (
    <View style={styles.row}>
      <Chip
        icon="person.2"
        label={`${label} — ${subagentStatus(provider, s, now, status)}`}
        palette={palette}
        onPress={s.id ? () => setOpen((o) => !o) : undefined}
        expanded={s.id ? open : undefined}
      />
      {open && (
        <View style={[styles.nestedGroup, { borderLeftColor: palette.edge }]}>
          {rows.map((r) => (
            // A subagent's own `who === null` prompt is the PARENT AGENT, never the viewer —
            // override the parent's isMine default (which treats null as "the session owner").
            <TranscriptRowView
              key={r.seq}
              provider={provider}
              row={r}
              name={(who) => (who ? "Teammate" : "Parent agent")}
              isMine={() => false}
              events={events}
              status={status}
            />
          ))}
        </View>
      )}
    </View>
  );
}

/** "Thought" / "Thought for 5s", expanding onto the thinking text when there is any. */
function ThinkingRow({ provider, row, live }: { provider: LocalSessionProvider; row: ThinkingTranscriptRow; live: boolean }) {
  const palette = useChatPalette();
  const [open, setOpen] = useState(false);
  const label = thinkingLabel(row, live);
  return (
    <View style={styles.row}>
      <Chip icon="sparkles" label={label} palette={palette} onPress={() => setOpen((o) => !o)} expanded={open} />
      {open && row.text ? (
        // ponytail: MessageMarkdown has no "soft" tone, so a lower opacity stands
        // in for fgSoft text here rather than threading a new prop into it.
        <View style={[styles.well, { backgroundColor: palette.raised }, styles.softContent]}>
          <MessageMarkdown text={row.text} palette={palette} />
        </View>
      ) : null}
    </View>
  );
}

/** A folded run of tool calls: one summary chip that opens onto the individual rows. */
/** `lines`: what Codex prints under "Explored", one per command, visible while the chip is closed. */
function ToolGroup({ provider, rows, summary, lines }: { provider: LocalSessionProvider; rows: (ToolTranscriptRow | ThinkingTranscriptRow)[]; summary: string; lines?: string[] }) {
  const palette = useChatPalette();
  const [open, setOpen] = useState(false);
  // A thought on its own has no calls behind it to open: one muted line, not a tool chip.
  if (!rows.some((r) => r.kind === "tool")) return <Text style={[styles.centerMeta, { color: palette.fgMute }]}>{summary}</Text>;
  return (
    <View style={styles.row}>
      <Chip icon="wrench.and.screwdriver" label={summary} palette={palette} onPress={() => setOpen((o) => !o)} expanded={open} />
      {!open && lines?.map((line, i) => (
        <Text key={i} style={[styles.explored, { color: palette.fgMute }]}>{line}</Text>
      ))}
      {open && (
        <View style={[styles.nestedGroup, { borderLeftColor: palette.edge }]}>
          {rows.map((r) => (r.kind === "tool" ? <ToolRow key={r.seq} provider={provider} row={r} /> : <ThinkingRow key={r.seq} provider={provider} row={r} live={false} />))}
        </View>
      )}
    </View>
  );
}

function ToolRow({ provider, row }: { provider: LocalSessionProvider; row: ToolTranscriptRow }) {
  const palette = useChatPalette();
  // `expanded` (the chip) gates whether the well shows at all; `detailOpen` is
  // the existing preview-vs-full toggle nested inside it once it does. A diff
  // is always shown regardless of `expanded` — there is no "collapsed" diff.
  const [expanded, setExpanded] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const { status, preview, full, diff, more, showOutput, truncated } = toolRowView(row);
  const isDiff = diff.length > 0;
  const showWell = isDiff || expanded;
  const shown = detailOpen ? full || preview : preview;
  const body = truncated && shown ? `${shown}\n(truncated)` : shown;
  const hidden = isDiff ? Math.max(0, diff.length - PREVIEW_LINES) : more;
  return (
    <View style={styles.row}>
      <View style={styles.toolHead}>
        <Chip label={toolHeader(provider, row.call)} palette={palette} onPress={() => setExpanded((o) => !o)} expanded={expanded} />
        {status === "running" && (
          <Text style={[styles.runningSuffix, { color: palette.fgMute }]}>
            {` · ${provider === "claude" ? "running…" : "…"}`}
          </Text>
        )}
      </View>
      {status === "error" && <MetaText palette={palette} tone="alert">Error</MetaText>}
      {showWell && isDiff && (
        <View style={[styles.well, { backgroundColor: palette.raised }]}>
          {(detailOpen ? diff : diff.slice(0, PREVIEW_LINES)).map((line, i) => (
            <Text
              key={i}
              style={[styles.output, { color: line.startsWith("+") ? palette.live : line.startsWith("-") ? palette.alert : palette.fgSoft }]}
            >
              {line}
            </Text>
          ))}
          {truncated && <Text style={[styles.output, { color: palette.fgSoft }]}>(truncated)</Text>}
        </View>
      )}
      {showWell && !isDiff && body ? (
        <View style={[styles.well, { backgroundColor: palette.raised }]}>
          <Text style={[styles.output, { color: palette.fgSoft }]}>{body}</Text>
        </View>
      ) : null}
      {showWell && (hidden > 0 || (!isDiff && showOutput)) && (
        <MetaText
          palette={palette}
          tone="link"
          onPress={() => setDetailOpen((o) => !o)}
          expanded={detailOpen}
          accessibilityLabel={detailOpen ? "collapse" : hidden > 0 ? `… +${hidden} lines` : "show output"}
        >
          {detailOpen ? "collapse" : hidden > 0 ? `… +${hidden} lines` : "show output"}
        </MetaText>
      )}
    </View>
  );
}

const TONE_COLOR = (p: ChatPalette): Record<SessionReceiptChip["tone"], string> => ({
  pending: p.fgMute,
  ok: p.live,
  warn: p.warn,
  error: p.alert,
});

/** The one status chip a follow-up carries, MetaText-styled; the detail rides along for VoiceOver. */
export function ReceiptChipText({ chip }: { chip: SessionReceiptChip }) {
  const palette = useChatPalette();
  return (
    <Text
      accessibilityLiveRegion="polite"
      accessibilityLabel={chip.detail ? `${chip.label}. ${chip.detail}` : chip.label}
      style={[styles.metaText, { color: TONE_COLOR(palette)[chip.tone] }]}
    >
      {chip.label}
    </Text>
  );
}

/** A follow-up sent from here that the harness has not echoed yet: a dimmed mine bubble, like Claude Code's queued prompt. */
export function PendingPromptRow({ provider: _provider, text, ownerName, chip, label = "Pending follow-up" }: {
  provider: LocalSessionProvider; text: string; ownerName: string; chip: SessionReceiptChip | null; label?: string;
}) {
  const palette = useChatPalette();
  return (
    <View style={styles.row} accessibilityLabel={label}>
      <Bubble mine palette={palette} dimmed accessibilityLabel={`You: ${text}`}>
        <MessageMarkdown text={text} palette={palette} inverse />
      </Bubble>
      <BubbleMeta align="end" palette={palette}>
        <MetaText palette={palette}>{ownerName}</MetaText>
        {chip && <ReceiptChipText chip={chip} />}
      </BubbleMeta>
    </View>
  );
}

export function WorkingRow({ provider: _provider, since }: { provider: LocalSessionProvider; since?: string }) {
  const palette = useChatPalette();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const started = since ? Date.parse(since) : NaN;
  const seconds = Number.isFinite(started) ? Math.max(0, Math.round((now - started) / 1000)) : null;
  const caption = seconds !== null ? `Working · ${seconds}s` : "Working";
  return <WorkingBubble palette={palette} caption={caption} accessibilityLabel="Working" />;
}

const styles = StyleSheet.create({
  row: { marginBottom: spacing.md },
  authorName: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, marginBottom: 2 },
  centerMeta: { textAlign: "center", fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, marginVertical: spacing.xs },
  toolHead: { flexDirection: "row", alignItems: "center", flexWrap: "wrap" },
  runningSuffix: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16 },
  well: { borderRadius: radii.lg, padding: 10, marginTop: 4 },
  softContent: { opacity: 0.75 },
  output: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 17 },
  explored: { marginLeft: spacing.lg, fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  nestedGroup: { marginLeft: spacing.lg, marginTop: spacing.xs, paddingLeft: spacing.sm, borderLeftWidth: StyleSheet.hairlineWidth },
  metaText: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16 },
});
