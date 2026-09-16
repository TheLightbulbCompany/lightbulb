import { SymbolView } from "expo-symbols";
import { frameText, type RoomRunView } from "@lightbulb/shared";
import { useCallback, useState } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { hapticImpact } from "../../lib/haptics";
import { AgentHead } from "../chat/AgentHead";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { ToolPill } from "../chat/ToolPill";

const STEER_FAILED = "This message could not be sent to the run.";
const ABORT_FAILED = "This run could not be stopped.";

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
}: {
  run: RoomRunView;
  name: string;
  onAbort?: () => Promise<unknown>;
  onSteer?: (text: string) => Promise<unknown>;
}) {
  const running = run.state === "running";

  // Latched so a second tap while the request is in flight can't fire a
  // double-abort; cleared on failure so the reader can retry.
  const [aborting, setAborting] = useState(false);
  const [abortFailed, setAbortFailed] = useState(false);
  const stop = useCallback(() => {
    if (!onAbort || aborting) return;
    hapticImpact();
    setAbortFailed(false);
    setAborting(true);
    onAbort()
      .catch(() => setAbortFailed(true))
      .finally(() => setAborting(false));
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
      <AgentHead name={name} active={running} />
      {run.tools.map((tool) => (
        <View key={tool.id} style={styles.pillGap}>
          <ToolPill
            tool={tool.name}
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
      ))}
      {run.text.trim() ? <MessageMarkdown text={run.text} /> : null}
      {running && (onAbort || onSteer) ? (
        <View style={styles.controls}>
          {onSteer ? (
            <View style={styles.steerRow}>
              <TextInput
                style={styles.steerInput}
                placeholder="Steer this run…"
                placeholderTextColor={colors.placeholder}
                value={steerText}
                onChangeText={setSteerText}
                editable={!steering}
                onSubmitEditing={submitSteer}
                returnKeyType="send"
                accessibilityLabel={`Steer ${name}`}
              />
              <Pressable
                style={[styles.iconButton, (!steerText.trim() || steering) && styles.iconDisabled]}
                onPress={submitSteer}
                disabled={!steerText.trim() || steering}
                accessibilityRole="button"
                accessibilityLabel={`Send steering message to ${name}`}
              >
                {steering ? (
                  <ActivityIndicator size="small" color={colors.white} />
                ) : (
                  <SymbolView accessibilityElementsHidden name="arrow.up" size={14} tintColor={colors.white} />
                )}
              </Pressable>
            </View>
          ) : null}
          {onAbort ? (
            <Pressable
              style={[styles.iconButton, aborting && styles.iconDisabled]}
              onPress={stop}
              disabled={aborting}
              accessibilityRole="button"
              accessibilityLabel={`Stop ${name}`}
            >
              {aborting ? (
                <ActivityIndicator size="small" color={colors.white} />
              ) : (
                <SymbolView accessibilityElementsHidden name="stop.fill" size={12} tintColor={colors.white} />
              )}
            </Pressable>
          ) : null}
        </View>
      ) : null}
      {steerFailed ? (
        <Text accessibilityRole="alert" style={styles.error}>
          {STEER_FAILED}
        </Text>
      ) : null}
      {abortFailed ? (
        <Text accessibilityRole="alert" style={styles.error}>
          {ABORT_FAILED}
        </Text>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  run: { marginTop: spacing.lg },
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
    backgroundColor: colors.white,
    borderRadius: radii.lg,
    borderWidth: 1,
    borderColor: colors.edge,
    paddingLeft: spacing.md,
    paddingRight: spacing.xs,
    paddingVertical: spacing.xs,
  },
  steerInput: {
    flex: 1,
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.ink,
    paddingVertical: 0,
  },
  // Fixed 28pt icon-only affordance — same convention as the composer's own
  // Stop/Send buttons. No label to scale: accessibilityLabel carries the name.
  iconButton: {
    width: 28,
    height: 28,
    borderRadius: 14,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
  iconDisabled: { opacity: 0.5 },
  error: {
    fontFamily: fonts.sans,
    fontSize: 13,
    lineHeight: 18,
    color: colors.errorText,
    marginTop: spacing.xs,
  },
});
