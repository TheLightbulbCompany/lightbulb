import {
  GLYPH, lifecycleLabel, toolHeader, toolRowView,
  type LocalSessionProvider, type ToolTranscriptRow, type TranscriptRow,
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
 * One row of a shared session in its TUI's grammar — the phone twin of the
 * web NativeSessionTranscript. `name` resolves a prompt's author: null is the
 * session owner, otherwise a room member's id.
 */
export const TranscriptRowView = memo(function TranscriptRowView({ provider, row, name }: {
  provider: LocalSessionProvider;
  row: TranscriptRow;
  name: (who: string | null) => string;
}) {
  const g = GLYPH[provider];
  switch (row.kind) {
    case "prompt": {
      const at = time(row.ts);
      return (
        <View style={styles.row} accessibilityLabel={`Native event ${row.seq}`}>
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
      return (
        <View style={styles.row} accessibilityLabel={`Native event ${row.seq}`}>
          <View style={styles.line}>
            <Text style={[styles.glyph, styles.thinkingGlyph]}>{g.thinking}</Text>
            {row.text ? (
              <View style={[styles.prose, styles.thinking]}><MessageMarkdown text={row.text} /></View>
            ) : (
              <Text style={styles.metaInline}>Thinking…</Text>
            )}
          </View>
        </View>
      );
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

function ToolRow({ provider, row }: { provider: LocalSessionProvider; row: ToolTranscriptRow }) {
  const [open, setOpen] = useState(false);
  const g = GLYPH[provider];
  const { status, preview, full, diff, more, showOutput, truncated } = toolRowView(row);
  const isDiff = diff.length > 0;
  const shown = open ? full || preview : preview;
  const body = truncated && shown ? `${shown}\n(truncated)` : shown;
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
          {diff.map((line, i) => (
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
      {!isDiff && (more > 0 || showOutput) && (
        <Pressable onPress={() => setOpen((o) => !o)} accessibilityRole="button" accessibilityState={{ expanded: open }} hitSlop={8}>
          <Text style={styles.more}>{open ? "collapse" : more > 0 ? `… +${more} lines` : "show output"}</Text>
        </Pressable>
      )}
    </View>
  );
}

/** The trailing spinner line while a turn is open, with the seconds since it started. */
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
  row: { marginBottom: spacing.sm },
  line: { flexDirection: "row", alignItems: "flex-start" },
  glyph: { width: GLYPH_WIDTH, fontFamily: fonts.mono, fontSize: 14, lineHeight: 20, color: colors.inkSoft },
  glyphSpace: { width: GLYPH_WIDTH },
  thinkingGlyph: { color: colors.mute },
  workingGlyph: { color: colors.green },
  promptBand: {
    flexDirection: "row",
    alignItems: "flex-start",
    backgroundColor: colors.codeBg,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    borderRadius: radii.sm,
    paddingVertical: spacing.xs,
    paddingHorizontal: spacing.sm,
  },
  promptText: { flex: 1, fontFamily: fonts.mono, fontSize: 14, lineHeight: 20, color: colors.ink },
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
  working: { fontFamily: fonts.mono, fontSize: 14, lineHeight: 20, color: colors.ink },
});
