import { SymbolView } from "expo-symbols";
import {
  browsingSite,
  collapseRepeats,
  delegationIdOf,
  frameText,
  resolveToolDisplay,
  toolRowLabel,
  type RoomRunView,
  type RoomSubagents,
  type RunToolCall,
} from "@lightbulb/shared";
import { useCallback, useState } from "react";
import {
  ActivityIndicator,
  Linking,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { fonts, spacing, useChatPalette } from "../../config/theme";
import { hapticImpact } from "../../lib/haptics";
import { browserPanelUrl } from "../../lib/webLinks";
import { Bubble, BubbleMeta, Chip, MetaText, WorkingBubble, AVATAR_GUTTER, BUBBLE_RADIUS } from "../conversation/bubbles";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { ToolPill } from "../chat/ToolPill";
import { Avatar } from "./Avatar";
import { DelegateCard } from "./DelegateCard";

const STEER_FAILED = "This message could not be sent to the run.";
const ABORT_FAILED = "This run could not be stopped.";

/** The same human row label web shows ("Mac browser · open example.com"), not the raw tool id. */
function toolLabel(tool: RunToolCall, running = tool.status === "running"): string {
  return tool.kind === "activity" ? tool.name : toolRowLabel(resolveToolDisplay(tool.name, tool.args), running);
}

/**
 * What the working bubble (or its accessibility label) says while a run has
 * not yet produced any reply text: the site it is browsing, the tool it is
 * currently running, or a plain "is working" fallback.
 */
export function runCaption(run: RoomRunView, name: string): string {
  const site = browsingSite(run);
  if (site !== undefined) return site ? `Browsing ${site}` : "Browsing";
  const runningTool = [...run.tools].reverse().find((tool) => tool.status === "running");
  if (runningTool) return toolLabel(runningTool);
  return `${name} is working`;
}

/**
 * A live (or just-finished, not-yet-persisted) agent run. Rendered on both room
 * screens, each filtering `rooms.runs` to its own half of the thread split.
 *
 * `onAbort`/`onSteer` are per-RUN closures the caller already binds to this
 * run's id (web parity — `RoomPanel.renderRun`'s per-run Stop + steer form). A
 * room can hold several running agents at once, so every running block gets
 * its own affordances instead of the composer's single first-running-run Stop.
 * Both are gated on `run.state === "running"` here, so the caller may pass
 * them unconditionally for every run in the list.
 */
export function RunBlock({
  run,
  name,
  onAbort,
  onSteer,
  subagents,
}: {
  run: RoomRunView;
  name: string;
  onAbort?: () => Promise<unknown>;
  onSteer?: (text: string) => Promise<unknown>;
  subagents?: RoomSubagents;
}) {
  const palette = useChatPalette();
  const running = run.state === "running";
  const site = browsingSite(run);
  const hasText = run.text.trim().length > 0;
  const [toolsExpanded, setToolsExpanded] = useState(false);

  // "Ended without a reply": the run stopped or failed with no text at all,
  // so there is no bubble to speak for it — this line does instead.
  const outcome = hasText
    ? null
    : run.state === "failed"
      ? "Didn't finish"
      : run.state === "aborted"
        ? run.forcedStop
          ? "Stopped — may finish its current step"
          : "Stopped"
        : null;

  // Latched until the run actually ends (the controls unmount on run_ended): an accepted
  // Stop is not a stopped run. Cleared on failure so the reader can retry.
  const [aborting, setAborting] = useState(false);
  const [abortFailed, setAbortFailed] = useState(false);
  const stop = useCallback(() => {
    if (!onAbort || aborting) return;
    hapticImpact();
    setAbortFailed(false);
    setAborting(true);
    onAbort().catch(() => {
      setAbortFailed(true);
      setAborting(false);
    });
  }, [onAbort, aborting]);

  const [steerText, setSteerText] = useState("");
  const [steering, setSteering] = useState(false);
  const [steerFailed, setSteerFailed] = useState(false);
  const submitSteer = useCallback(() => {
    const text = steerText.trim();
    if (!onSteer || !text || steering) return;
    setSteerFailed(false);
    setSteering(true);
    onSteer(text)
      .then(() => setSteerText(""))
      .catch(() => setSteerFailed(true))
      .finally(() => setSteering(false));
  }, [onSteer, steerText, steering]);

  return (
    <View style={styles.run}>
      <View style={styles.head}>
        <Avatar name={name} kind="agent" size={28} palette={palette} />
        <Text style={[styles.name, { color: palette.fgMute }]} numberOfLines={1}>
          {name}
        </Text>
      </View>
      <View style={styles.body}>
        {hasText ? (
          <Bubble mine={false} palette={palette} accessibilityLabel={`${name}: ${run.text}`}>
            <MessageMarkdown text={run.text} palette={palette} />
          </Bubble>
        ) : running ? (
          <WorkingBubble
            palette={palette}
            caption={runCaption(run, name)}
            accessibilityLabel={`${name}: ${runCaption(run, name)}`}
          />
        ) : null}
        <BubbleMeta align="start" palette={palette}>
          {run.tools.length > 0 ? (
            <Chip
              label={`${running ? "Running" : "Ran"} ${run.tools.length} ${run.tools.length === 1 ? "tool" : "tools"}`}
              palette={palette}
              icon="wrench.and.screwdriver"
              expanded={toolsExpanded}
              onPress={() => setToolsExpanded((expanded) => !expanded)}
            />
          ) : null}
          {running && site !== undefined ? (
            <Chip
              label={`Watch ${site ?? "browser"}`}
              palette={palette}
              icon="globe"
              onPress={() => void Linking.openURL(browserPanelUrl())}
              accessibilityLabel={`${name} is browsing${site ? ` ${site}` : ""}. Open the shared browser on the web`}
            />
          ) : null}
          {outcome ? (
            <MetaText palette={palette} tone={run.state === "failed" ? "alert" : "mute"}>
              {outcome}
            </MetaText>
          ) : null}
        </BubbleMeta>
        {toolsExpanded
          ? collapseRepeats(run.tools, (tool) => `${tool.status}|${toolLabel(tool, false)}`).map(({ item: tool, count }) => (
              <View key={tool.id} style={styles.pillGap}>
                <ToolPill
                  tool={count > 1 ? `${toolLabel(tool)} ×${count}` : toolLabel(tool)}
                  status={tool.status}
                  args={
                    tool.args && typeof tool.args === "object" && !Array.isArray(tool.args)
                      ? (tool.args as Record<string, unknown>)
                      : undefined
                  }
                  // frame.result is a content-block array, so String() yields
                  // [object Object]; frameText is exported from shared for this.
                  result={frameText(tool.result) ?? tool.progressText}
                />
              </View>
            ))
          : null}
        {subagents
          ? run.tools
              .filter((t) => t.name === "delegate_task")
              .map((t) => {
                const delegationId = delegationIdOf(t.result);
                return delegationId ? (
                  <DelegateCard key={t.id} state={subagents} agentId={run.agentId} delegationId={delegationId} agentLive={running} />
                ) : null;
              })
          : null}
        {running && (onAbort || onSteer) ? (
          <View style={styles.controls}>
            {onSteer ? (
              <View style={[styles.steerRow, { backgroundColor: palette.surface, borderColor: palette.edge }]}>
                <TextInput
                  style={[styles.steerInput, { color: palette.fg }]}
                  placeholder="Steer this run…"
                  placeholderTextColor={palette.fgMute}
                  value={steerText}
                  onChangeText={setSteerText}
                  editable={!steering}
                  onSubmitEditing={submitSteer}
                  returnKeyType="send"
                  accessibilityLabel={`Steer ${name}`}
                />
                <Pressable
                  hitSlop={8}
                  style={[
                    styles.iconButton,
                    { backgroundColor: palette.fg },
                    (!steerText.trim() || steering) && styles.iconDisabled,
                  ]}
                  onPress={submitSteer}
                  disabled={!steerText.trim() || steering}
                  accessibilityRole="button"
                  accessibilityLabel={`Send steering message to ${name}`}
                >
                  {steering ? (
                    <ActivityIndicator size="small" color={palette.onFg} />
                  ) : (
                    <SymbolView accessibilityElementsHidden name="arrow.up" size={14} tintColor={palette.onFg} />
                  )}
                </Pressable>
              </View>
            ) : null}
            {onAbort ? (
              <Pressable
                hitSlop={8}
                style={[styles.stopButton, { backgroundColor: palette.fg }, aborting && styles.iconDisabled]}
                onPress={stop}
                disabled={aborting}
                accessibilityRole="button"
                accessibilityLabel={aborting ? `Stopping ${name}` : `Stop ${name}`}
                accessibilityState={{ disabled: aborting }}
              >
                {aborting ? (
                  <ActivityIndicator size="small" color={palette.onFg} />
                ) : (
                  <SymbolView accessibilityElementsHidden name="stop.fill" size={12} tintColor={palette.onFg} />
                )}
                <Text style={[styles.stopLabel, { color: palette.onFg }]} maxFontSizeMultiplier={1.3}>
                  {aborting ? "Stopping…" : "Stop"}
                </Text>
              </Pressable>
            ) : null}
          </View>
        ) : null}
        {steerFailed ? (
          <Text accessibilityRole="alert" style={[styles.error, { color: palette.alert }]}>
            {STEER_FAILED}
          </Text>
        ) : null}
        {abortFailed ? (
          <Text accessibilityRole="alert" style={[styles.error, { color: palette.alert }]}>
            {ABORT_FAILED}
          </Text>
        ) : null}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  run: { marginTop: spacing.lg },
  head: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  name: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16 },
  body: { marginLeft: AVATAR_GUTTER, marginTop: spacing.xs },
  pillGap: { marginTop: spacing.sm },
  controls: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    marginTop: spacing.sm,
  },
  steerRow: {
    flex: 1,
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    borderRadius: BUBBLE_RADIUS,
    borderWidth: 1,
    paddingLeft: spacing.md,
    paddingRight: spacing.xs,
    paddingVertical: spacing.xs,
  },
  steerInput: {
    flex: 1,
    fontFamily: fonts.sans,
    fontSize: 15,
    lineHeight: 20,
    paddingVertical: 0,
  },
  // Fixed 28pt icon-only affordance — same convention as the composer's own
  // Stop/Send buttons. No label to scale: accessibilityLabel carries the name.
  iconButton: {
    width: 28,
    height: 28,
    borderRadius: 14,
    alignItems: "center",
    justifyContent: "center",
  },
  iconDisabled: { opacity: 0.5 },
  stopButton: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    minHeight: 28,
    paddingHorizontal: spacing.md,
    borderRadius: 14,
  },
  stopLabel: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  error: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    marginTop: spacing.xs,
  },
});
