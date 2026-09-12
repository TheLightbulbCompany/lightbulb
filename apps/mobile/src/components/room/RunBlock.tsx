import { frameText, type RoomRunView } from "@lightbulb/shared";
import { StyleSheet, View } from "react-native";

import { spacing } from "../../config/theme";
import { AgentHead } from "../chat/AgentHead";
import { MessageMarkdown } from "../chat/MessageMarkdown";
import { ToolPill } from "../chat/ToolPill";

/**
 * A live (or just-finished, not-yet-persisted) agent run. Rendered on both room
 * screens, each filtering `rooms.runs` to its own half of the thread split.
 */
export function RunBlock({ run, name }: { run: RoomRunView; name: string }) {
  return (
    <View style={styles.run}>
      <AgentHead name={name} active={run.state === "running"} />
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
    </View>
  );
}

const styles = StyleSheet.create({
  run: { marginTop: spacing.lg },
  pillGap: { marginTop: spacing.sm },
});
