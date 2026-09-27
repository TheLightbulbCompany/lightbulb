import {
  GLYPH, buildRows, groupRows, lifecycleLabel, subagentLabel, subagentStatus, subagentTags, thinkingLabel, toolHeader, toolRowView,
  type LocalSessionEvent, type LocalSessionProvider, type SessionSubagent, type SessionReceiptChip, type DiffLine, type ThinkingTranscriptRow, type ToolGroupItem, type ToolTranscriptRow, type TranscriptItem,
} from "@lightbulb/shared";
import { memo, useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { MessageMarkdown } from "../chat/MessageMarkdown";

/** An edit's diff opens this far; output previews open PREVIEW_LINES (the shared model's own clamp). */
const DIFF_LINES = 12;

/** HH:MM in the reader's locale; null when the event carries no usable timestamp. */
function time(ts?: string): string | null {
  const d = ts ? new Date(ts) : null;
  return d && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null;
}

/**
 * One item of a shared session in its TUI's grammar — the phone twin of the
 * web NativeSessionTranscript. `name` resolves a prompt's author: null is the
 * session owner, otherwise a room member's id. `live` marks the last row of a
 * still-running turn (a live thinking block reads "Thinking…").
 */
export const TranscriptRowView = memo(function TranscriptRowView({ provider, row, name, live = false, events = [], status }: {
  provider: LocalSessionProvider;
  row: TranscriptItem;
  name: (who: string | null) => string;
  live?: boolean;
  /** Every loaded event: a subagent's row opens onto its own rows from them. */
  events?: readonly LocalSessionEvent[];
  /** The session's status: a subagent with no end reads "Ended" once its session is not working. */
  status?: string;
}) {
  const g = GLYPH[provider];
  switch (row.kind) {
    case "tools":
      return <ToolGroup provider={provider} group={row} />;
    case "prompt": {
      const at = time(row.ts);
      return (
        <View style={[styles.row, styles.promptRow]} accessibilityLabel={`Native event ${row.seq}`}>
          <View style={styles.promptBand}>
            <Text style={styles.glyph}>{g.prompt}</Text>
            <Text style={styles.promptText}>{row.text}</Text>
          </View>
          <Text style={styles.meta}>{`${name(row.who)}${at ? ` · ${at}` : ""}`}</Text>
        </View>
      );
    }
    case "prose":
      // Codex's final answer is bulleted like every other message, with no label.
      return (
        <View style={styles.row} accessibilityLabel={`Native event ${row.seq}`}>
          <View style={styles.line}>
            <Text style={styles.glyph}>{g.bullet}</Text>
            <View style={styles.prose}><MessageMarkdown text={row.text} /></View>
          </View>
        </View>
      );
    case "thinking":
      return <ThinkingRow provider={provider} row={row} live={live} />;
    case "tool":
      return <ToolRow provider={provider} row={row} />;
    case "notice":
      return <Text style={styles.meta} accessibilityLabel={`Native event ${row.seq}`}>{row.text}</Text>;
    case "agent":
      return <AgentRow provider={provider} seq={row.seq} subagent={row.subagent} events={events} status={status} />;
    case "subagent":
      return <Text style={styles.meta} accessibilityLabel={`Native event ${row.seq}`}>{`${g.output} ${subagentLabel(row)}`}</Text>;
    case "lifecycle": {
      const label = lifecycleLabel(row);
      // Claude Code closes a turn with `✻ Worked for 25s`; Codex prints no such line of its own.
      if (label && row.lifecycle === "turn_end" && provider === "claude") {
        return (
          <View style={[styles.line, styles.turnEnd]} accessibilityLabel={`Native event ${row.seq}`}>
            <Text style={[styles.glyph, styles.thinkingGlyph]} accessibilityElementsHidden importantForAccessibility="no">{g.working}</Text>
            <Text style={styles.metaInline}>{label}</Text>
          </View>
        );
      }
      return label ? <Text style={styles.meta} accessibilityLabel={`Native event ${row.seq}`}>{label}</Text> : null;
    }
  }
});

/**
 * A subagent inline where it was launched: `⏺ Agent · <description>` and a `⎿`
 * status line (live while it runs); tapping opens its own rows nested under it.
 */
function AgentRow({ provider, seq, subagent: s, events, status }: { provider: LocalSessionProvider; seq: number; subagent: SessionSubagent; events: readonly LocalSessionEvent[]; status?: string }) {
  const [open, setOpen] = useState(false);
  const running = s.state === "running";
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [running]);
  const g = GLYPH[provider];
  const name = s.description || "subagent";
  const label = [provider === "codex" ? `Spawned ${name}` : `Agent · ${name}`, ...subagentTags(s)].join(" · ");
  const rows = open ? groupRows(buildRows(events, s.id), false, provider) : [];
  return (
    <View style={styles.row} accessibilityLabel={`Native event ${seq}`}>
      {s.id ? <Toggle glyph={g.bullet} label={label} open={open} onPress={() => setOpen((o) => !o)} /> : (
        <View style={styles.line}><Text style={styles.glyph}>{g.bullet}</Text><Text style={styles.toolHeader}>{label}</Text></View>
      )}
      <Text style={[styles.output, s.state === "failed" && styles.errorText]}>{`${g.output} ${subagentStatus(provider, s, now, status)}`}</Text>
      {open && (
        <View style={[styles.nested, styles.groupRows]}>
          {rows.map((r) => <TranscriptRowView key={r.seq} provider={provider} row={r} name={(who) => (who ? "Teammate" : "Parent agent")} events={events} status={status} />)}
        </View>
      )}
    </View>
  );
}

/** One line to tap to open or close — the shared shape of a folded group and collapsed thinking. */
function Toggle({ glyph, label, open, onPress }: { glyph: string; label: string; open: boolean; onPress: () => void }) {
  return (
    <Pressable style={styles.line} onPress={onPress} accessibilityRole="button" accessibilityLabel={label} accessibilityState={{ expanded: open }} hitSlop={8}>
      <Text style={[styles.glyph, styles.thinkingGlyph]}>{glyph}</Text>
      <Text style={styles.toggle}>{`${label} ${open ? "⌄" : "›"}`}</Text>
    </Pressable>
  );
}

function ThinkingRow({ provider, row, live }: { provider: LocalSessionProvider; row: ThinkingTranscriptRow; live: boolean }) {
  const [open, setOpen] = useState(false);
  const g = GLYPH[provider];
  const label = thinkingLabel(row, live);
  return (
    <View style={styles.row} accessibilityLabel={`Native event ${row.seq}`}>
      {row.text ? (
        <Toggle glyph={g.thinking} label={label} open={open} onPress={() => setOpen((o) => !o)} />
      ) : (
        <View style={styles.line}>
          <Text style={[styles.glyph, styles.thinkingGlyph]}>{g.thinking}</Text>
          <Text style={styles.metaInline}>{label}</Text>
        </View>
      )}
      {open && row.text ? <View style={[styles.nested, styles.thinking]}><MessageMarkdown text={row.text} /></View> : null}
    </View>
  );
}

/**
 * A folded run of tool calls, in its terminal's own words. Claude Code prints one
 * unbulleted line ("Read 2 files, ran 1 shell command"); Codex prints a bulleted
 * "Explored" over one line per call. Either opens onto the calls themselves.
 */
function ToolGroup({ provider, group }: { provider: LocalSessionProvider; group: ToolGroupItem }) {
  const [open, setOpen] = useState(false);
  const g = GLYPH[provider];
  // A thought on its own has no calls behind it to open.
  if (!group.rows.some((r) => r.kind === "tool")) {
    return (
      <View style={[styles.row, styles.line]} accessibilityLabel={`Native event ${group.seq}`}>
        <Text style={[styles.glyph, styles.thinkingGlyph]}> </Text>
        <Text style={styles.metaInline}>{group.summary}</Text>
      </View>
    );
  }
  return (
    <View style={styles.row}>
      <Toggle glyph={group.lines ? g.bullet : ""} label={group.summary} open={open} onPress={() => setOpen((o) => !o)} />
      {!open && group.lines?.map((line, i) => (
        <Hang key={i} glyph={i === 0 ? g.output : " "} style={styles.explored}>{line}</Hang>
      ))}
      {open && (
        <View style={[styles.nested, styles.groupRows]}>
          {group.rows.map((r) => r.kind === "tool" && <ToolRow key={r.seq} provider={provider} row={r} />)}
        </View>
      )}
    </View>
  );
}

/**
 * A line under a corner glyph (`⎿`, `└`): the glyph stands alone, so the second and
 * later lines of a multi-line body line up with the first line's text, not the glyph.
 */
function Hang({ glyph, style, children }: { glyph: string; style?: object; children: string }) {
  return (
    <View style={[styles.line, style]}>
      <Text style={styles.output} accessibilityElementsHidden importantForAccessibility="no">{`${glyph} `}</Text>
      <Text style={[styles.output, styles.hangText]}>{children}</Text>
    </View>
  );
}

/** A diff as both terminals print it: the line number (decorative, hidden from VoiceOver), the mark, the line. */
function Diff({ lines }: { lines: readonly DiffLine[] }) {
  const width = String(lines.reduce((max, line) => Math.max(max, line.n ?? 0), 0)).length;
  return lines.map((line, i) => (
    <View key={i} style={styles.line}>
      <Text style={[styles.output, styles.diffCtx]} accessibilityElementsHidden importantForAccessibility="no">
        {`${String(line.n ?? "").padStart(width)} `}
      </Text>
      <Text style={[styles.output, styles.hangText, line.mark === "+" ? styles.diffAdd : line.mark === "-" ? styles.diffDel : styles.diffCtx]}>
        {`${line.mark}${line.text}`}
      </Text>
    </View>
  ));
}

function ToolRow({ provider, row }: { provider: LocalSessionProvider; row: ToolTranscriptRow }) {
  const [open, setOpen] = useState(false);
  const g = GLYPH[provider];
  const { status, preview, full, numbered, summary, more, showOutput, truncated } = toolRowView(row);
  const isDiff = numbered.length > 0;
  const shown = open ? full || preview : preview;
  const body = truncated && shown ? `${shown}\n(truncated)` : shown;
  const hidden = isDiff ? Math.max(0, numbered.length - DIFF_LINES) : more;
  return (
    <View style={styles.row} accessibilityLabel={`Native event ${row.seq}`}>
      <View style={styles.line}>
        <Text style={styles.glyph}>{g.bullet}</Text>
        <Text style={styles.toolHeader}>
          {toolHeader(provider, row.call)}
          {status === "running" && <Text style={styles.metaInline}> {provider === "claude" ? "running…" : "…"}</Text>}
        </Text>
      </View>
      {status === "error" && <Text style={[styles.output, styles.errorText]}>{`${g.output} Error`}</Text>}
      {summary ? <Hang glyph={g.output} style={styles.summary}>{summary}</Hang> : null}
      {isDiff && (
        <View style={styles.outputBlock}>
          <Diff lines={open ? numbered : numbered.slice(0, DIFF_LINES)} />
          {truncated && <Text style={styles.output}>(truncated)</Text>}
        </View>
      )}
      {!isDiff && body ? (
        <View style={styles.outputBlock}>
          <Hang glyph={g.output}>{body}</Hang>
        </View>
      ) : null}
      {(hidden > 0 || (!isDiff && showOutput)) && (
        <Pressable onPress={() => setOpen((o) => !o)} accessibilityRole="button" accessibilityState={{ expanded: open }} hitSlop={8}>
          <Text style={styles.more}>{open ? "collapse" : hidden > 0 ? `… +${hidden} lines` : "show output"}</Text>
        </Pressable>
      )}
    </View>
  );
}

/** The trailing spinner line while a turn is open, with the seconds since it started. */
const CHIP_COLOR: Record<SessionReceiptChip["tone"], string> = { pending: colors.mute, ok: colors.green, warn: colors.warn, error: colors.errorText };

/** The one status chip a follow-up carries; the detail rides along for VoiceOver. */
export function ReceiptChipText({ chip }: { chip: SessionReceiptChip }) {
  return (
    <Text
      accessibilityLiveRegion="polite"
      accessibilityLabel={chip.detail ? `${chip.label}. ${chip.detail}` : chip.label}
      style={[styles.chip, { color: CHIP_COLOR[chip.tone], borderColor: CHIP_COLOR[chip.tone] }]}
    >
      {chip.label}
    </Text>
  );
}

/** A follow-up sent from here that the harness has not echoed yet: greyed, like Claude Code's queued prompt. */
export function PendingPromptRow({ provider, text, ownerName, chip }: {
  provider: LocalSessionProvider; text: string; ownerName: string; chip: SessionReceiptChip | null;
}) {
  return (
    <View style={[styles.row, styles.promptRow]} accessibilityLabel="Pending follow-up">
      <View style={[styles.promptBand, styles.pendingBand]}>
        <Text style={styles.glyph}>{GLYPH[provider].prompt}</Text>
        <Text style={[styles.promptText, styles.pendingText]}>{text}</Text>
      </View>
      <View style={styles.pendingMeta}>
        <Text style={styles.metaInline}>{ownerName}</Text>
        {chip && <ReceiptChipText chip={chip} />}
      </View>
    </View>
  );
}

export function WorkingRow({ provider, since }: { provider: LocalSessionProvider; since?: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const started = since ? Date.parse(since) : NaN;
  const seconds = Number.isFinite(started) ? Math.max(0, Math.round((now - started) / 1000)) : null;
  return (
    <View style={styles.line} accessibilityLabel="Working">
      <Text style={[styles.glyph, styles.workingGlyph]}>{GLYPH[provider].working}</Text>
      <Text style={styles.working}>
        {"Working…"}{seconds !== null && <Text style={styles.metaInline}>{` ${seconds}s`}</Text>}
      </Text>
    </View>
  );
}

const GLYPH_WIDTH = 18;

const styles = StyleSheet.create({
  row: { marginBottom: spacing.md },
  line: { flexDirection: "row", alignItems: "flex-start" },
  glyph: { width: GLYPH_WIDTH, fontFamily: fonts.mono, fontSize: 14, lineHeight: 20, color: colors.inkSoft },
  thinkingGlyph: { color: colors.mute },
  workingGlyph: { color: colors.green },
  promptRow: { marginTop: spacing.md },
  promptBand: {
    flexDirection: "row",
    alignItems: "flex-start",
    backgroundColor: colors.codeBg,
    borderLeftWidth: 2,
    borderLeftColor: colors.border,
    borderRadius: radii.sm,
    paddingVertical: spacing.xs,
    paddingHorizontal: spacing.sm,
  },
  promptText: { flex: 1, fontFamily: fonts.sans, fontSize: 15, lineHeight: 21, color: colors.ink },
  toggle: { flex: 1, fontFamily: fonts.sans, fontSize: 13, lineHeight: 20, color: colors.mute },
  nested: { marginLeft: GLYPH_WIDTH, marginTop: spacing.xs },
  groupRows: { paddingLeft: spacing.sm, borderLeftWidth: StyleSheet.hairlineWidth, borderLeftColor: colors.border },
  prose: { flex: 1 },
  thinking: { opacity: 0.7 },
  toolHeader: { flex: 1, fontFamily: fonts.mono, fontSize: 13, lineHeight: 20, color: colors.ink },
  outputBlock: {
    marginLeft: GLYPH_WIDTH,
    marginTop: 2,
    backgroundColor: colors.codeBg,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    borderRadius: radii.sm,
    paddingVertical: spacing.xs,
    paddingHorizontal: spacing.sm,
  },
  output: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 17, color: colors.inkSoft },
  hangText: { flex: 1 },
  summary: { marginLeft: GLYPH_WIDTH, marginTop: 2 },
  explored: { marginLeft: GLYPH_WIDTH },
  turnEnd: { marginBottom: spacing.md },
  errorText: { color: colors.error, marginLeft: GLYPH_WIDTH },
  diffAdd: { color: colors.green },
  diffDel: { color: colors.error },
  diffCtx: { color: colors.mutedSecondary },
  more: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 17, color: colors.forest, marginLeft: GLYPH_WIDTH, marginTop: 2 },
  meta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute, marginTop: 2, marginLeft: GLYPH_WIDTH },
  metaInline: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 20, color: colors.mute },
  working: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20, color: colors.ink },
  pendingBand: { borderStyle: "dashed" },
  pendingText: { color: colors.mute },
  pendingMeta: { flexDirection: "row", alignItems: "center", gap: spacing.sm, marginTop: 2, marginLeft: GLYPH_WIDTH },
  chip: {
    fontFamily: fonts.sansMedium,
    fontSize: 12,
    lineHeight: 16,
    paddingHorizontal: 6,
    borderWidth: StyleSheet.hairlineWidth,
    borderRadius: 999,
    overflow: "hidden",
  },
});
