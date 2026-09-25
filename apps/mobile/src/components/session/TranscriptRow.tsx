import {
  GLYPH, PREVIEW_LINES, lifecycleLabel, thinkingLabel, toolHeader, toolRowView,
  type LocalSessionProvider, type SessionReceiptChip, type ThinkingTranscriptRow, type ToolTranscriptRow, type TranscriptItem,
} from "@lightbulb/shared";
import { memo, useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { MessageMarkdown } from "../chat/MessageMarkdown";

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
export const TranscriptRowView = memo(function TranscriptRowView({ provider, row, name, live = false }: {
  provider: LocalSessionProvider;
  row: TranscriptItem;
  name: (who: string | null) => string;
  live?: boolean;
}) {
  const g = GLYPH[provider];
  switch (row.kind) {
    case "tools":
      return <ToolGroup provider={provider} rows={row.rows} summary={row.summary} />;
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
    case "prose": {
      const final = provider === "codex" && row.final;
      return (
        <View style={styles.row} accessibilityLabel={`Native event ${row.seq}`}>
          {final && <Text style={styles.codexLabel}>codex</Text>}
          <View style={styles.line}>
            {final ? <View style={styles.glyphSpace} /> : <Text style={styles.glyph}>{g.bullet}</Text>}
            <View style={styles.prose}><MessageMarkdown text={row.text} /></View>
          </View>
        </View>
      );
    }
    case "thinking":
      return <ThinkingRow provider={provider} row={row} live={live} />;
    case "tool":
      return <ToolRow provider={provider} row={row} />;
    case "notice":
      return <Text style={styles.meta} accessibilityLabel={`Native event ${row.seq}`}>{row.text}</Text>;
    case "lifecycle": {
      const label = lifecycleLabel(row);
      return label ? <Text style={styles.meta} accessibilityLabel={`Native event ${row.seq}`}>{label}</Text> : null;
    }
  }
});

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

/** A folded run of tool calls: one summary line that opens onto the individual rows. */
function ToolGroup({ provider, rows, summary }: { provider: LocalSessionProvider; rows: (ToolTranscriptRow | ThinkingTranscriptRow)[]; summary: string }) {
  const [open, setOpen] = useState(false);
  return (
    <View style={styles.row}>
      <Toggle glyph={GLYPH[provider].bullet} label={summary} open={open} onPress={() => setOpen((o) => !o)} />
      {open && (
        <View style={[styles.nested, styles.groupRows]}>
          {rows.map((r) => (r.kind === "tool" ? <ToolRow key={r.seq} provider={provider} row={r} /> : <ThinkingRow key={r.seq} provider={provider} row={r} live={false} />))}
        </View>
      )}
    </View>
  );
}

function ToolRow({ provider, row }: { provider: LocalSessionProvider; row: ToolTranscriptRow }) {
  const [open, setOpen] = useState(false);
  const g = GLYPH[provider];
  const { status, preview, full, diff, more, showOutput, truncated } = toolRowView(row);
  const isDiff = diff.length > 0;
  const shown = open ? full || preview : preview;
  const body = truncated && shown ? `${shown}\n(truncated)` : shown;
  const hidden = isDiff ? Math.max(0, diff.length - PREVIEW_LINES) : more;
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
      {isDiff && (
        <View style={styles.outputBlock}>
          {(open ? diff : diff.slice(0, PREVIEW_LINES)).map((line, i) => (
            <Text key={i} style={[styles.output, line.startsWith("+") ? styles.diffAdd : line.startsWith("-") ? styles.diffDel : styles.diffCtx]}>
              {line}
            </Text>
          ))}
          {truncated && <Text style={styles.output}>(truncated)</Text>}
        </View>
      )}
      {!isDiff && body ? (
        <View style={styles.outputBlock}>
          <Text style={styles.output}>{`${g.output} ${body}`}</Text>
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
  glyphSpace: { width: GLYPH_WIDTH },
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
  codexLabel: { fontFamily: fonts.sansSemiBold, fontSize: 11, lineHeight: 14, color: colors.mute, marginBottom: 2 },
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
