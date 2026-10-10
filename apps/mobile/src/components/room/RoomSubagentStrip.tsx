import {
  isSubagentGone,
  liveSubagents,
  sessionClock,
  subagentActivity,
  useSubagentTail,
  type MessagingClient,
  type RoomSubagents,
  type SubagentProgress,
} from "@lightbulb/shared";
import { useEffect, useState } from "react";
import { AccessibilityInfo, ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";

import { fonts, spacing, useChatPalette } from "../../config/theme";
import { BottomSheet } from "../ui/BottomSheet";

const PREVIEW = 3; // upstream Desktop previews three workers
const GONE = "That subagent has already finished.";
const ENDED: Record<string, string> = { completed: "Finished", failed: "Failed", interrupted: "Stopped" };

function useNow(on: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, [on]);
  return now;
}

function useReduceMotion() {
  const [reduce, setReduce] = useState(false);
  useEffect(() => {
    let live = true;
    void AccessibilityInfo.isReduceMotionEnabled?.()?.then((v) => { if (live) setReduce(v); }, () => {});
    return () => { live = false; };
  }, []);
  return reduce;
}

type Act = (agentId: string, id: string, text: string) => Promise<unknown>;

function Detail({ item, roomId, client, onSteer, onStop }: {
  item: SubagentProgress; roomId: string; client: MessagingClient;
  onSteer: Act; onStop: (agentId: string, id: string) => Promise<unknown>;
}) {
  const palette = useChatPalette();
  const live = item.status === "running" || item.status === "queued";
  // A finished child stops polling but keeps its last transcript on screen until the person closes the sheet.
  const { tail } = useSubagentTail(client, live ? { roomId, agentId: item.agentId, subagentId: item.id } : null);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const run = (call: () => Promise<unknown>, ok: string, after?: () => void) => {
    if (busy) return;
    setBusy(true);
    call().then(() => { after?.(); setNote(ok); },
      (e) => setNote(isSubagentGone(e) ? GONE : "Could not reach the agent. Try again."))
      .finally(() => setBusy(false));
  };
  const value = text.trim();
  const blocked = busy || !live;
  const off = blocked ? styles.disabled : null;
  return (
    <View style={styles.detail}>
      <Text style={[styles.title, { color: palette.fg }]} numberOfLines={2}>{item.goal}</Text>
      {!live ? <Text style={[styles.note, { color: palette.fgSoft }]}>{ENDED[item.status] ?? "Finished"}</Text> : null}
      <View style={[styles.steerRow, { backgroundColor: palette.surface, borderColor: palette.edge }]}>
        <TextInput
          style={[styles.input, { color: palette.fg }]}
          placeholder="Steer this subagent"
          placeholderTextColor={palette.fgMute}
          value={text}
          onChangeText={setText}
          editable={!blocked}
          accessibilityLabel={`Steer ${item.goal}`}
        />
        <Pressable
          style={[styles.button, { backgroundColor: palette.fg }, (!value || blocked) && styles.disabled]}
          disabled={!value || blocked}
          onPress={() => run(() => onSteer(item.agentId, item.id, value), "Steer queued", () => setText(""))}
          accessibilityRole="button"
          accessibilityLabel="Steer"
        >
          <Text style={[styles.buttonText, { color: palette.onFg }]} maxFontSizeMultiplier={1.3}>Steer</Text>
        </Pressable>
        <Pressable
          style={[styles.button, { borderColor: palette.edge, borderWidth: 1 }, off]}
          disabled={blocked}
          onPress={() => run(() => onStop(item.agentId, item.id), "Stopping…")}
          accessibilityRole="button"
          accessibilityLabel="Stop"
        >
          <Text style={[styles.buttonText, { color: palette.fg }]} maxFontSizeMultiplier={1.3}>Stop</Text>
        </Pressable>
      </View>
      {/* Once the child ends its final status above says it all: no "Stopping…" beside "Stopped". */}
      {note && live ? <Text accessibilityRole="alert" style={[styles.note, { color: palette.fgSoft }]}>{note}</Text> : null}
      <ScrollView style={[styles.tail, { backgroundColor: palette.raised }]}>
        <Text selectable style={[styles.mono, { color: palette.fgSoft }]}>
          {tail?.available ? tail.text : (!live && item.summary) || "No transcript yet."}
        </Text>
      </ScrollView>
    </View>
  );
}

export function RoomSubagentStrip({ state, roomId, agentName, client, onSteer, onStop, onVisible }: {
  state: RoomSubagents; roomId: string; agentName: (agentId: string) => string; client: MessagingClient;
  onSteer: Act; onStop: (agentId: string, id: string) => Promise<unknown>;
  onVisible: (visible: boolean) => void;
}) {
  const palette = useChatPalette();
  const live = liveSubagents(state);
  const [open, setOpen] = useState(true);
  const [all, setAll] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const reduceMotion = useReduceMotion();
  const now = useNow(live.length > 0);
  const visible = live.length > 0 && open;
  useEffect(() => { onVisible(visible); return () => onVisible(false); }, [visible, onVisible]);
  // From all items, not the live list: the sheet stays open on a child that just finished.
  const pickedItem = state.items.find((i) => i.id === picked);
  if (!live.length && !pickedItem) return null;
  const rows = all ? live : live.slice(0, PREVIEW);
  return (
    <View style={[styles.strip, { borderColor: palette.edge, backgroundColor: palette.raised }]} accessibilityLabel="Running subagents">
      <Pressable
        onPress={() => setOpen((o) => !o)}
        accessibilityRole="button"
        accessibilityState={{ expanded: open }}
        style={styles.heading}
      >
        <Text style={[styles.headingText, { color: palette.fgMute }]} maxFontSizeMultiplier={1.3}>Subagents ({live.length})</Text>
      </Pressable>
      {open ? (
        <>
          {rows.map((item) => (
            <Pressable
              key={item.id}
              style={styles.row}
              onPress={() => setPicked(item.id)}
              accessibilityRole="button"
              accessibilityLabel={`${agentName(item.agentId)} · ${item.goal}`}
            >
              {reduceMotion
                ? <Text style={{ color: palette.fgMute }} importantForAccessibility="no">○</Text>
                : <ActivityIndicator size="small" color={palette.fgMute} />}
              <View style={styles.rowText}>
                <Text style={[styles.goal, { color: palette.fg }]} numberOfLines={1} maxFontSizeMultiplier={1.3}>
                  {agentName(item.agentId)} · {item.goal}
                </Text>
                <Text style={[styles.activity, { color: palette.fgMute }]} numberOfLines={1} maxFontSizeMultiplier={1.3}>
                  {subagentActivity(item)}
                </Text>
              </View>
              <Text style={[styles.activity, { color: palette.fgMute }]} maxFontSizeMultiplier={1.3}>
                {sessionClock(now - item.startedAt, true)}
              </Text>
            </Pressable>
          ))}
          {!all && live.length > PREVIEW ? (
            <Pressable style={styles.row} onPress={() => setAll(true)} accessibilityRole="button">
              <Text style={[styles.goal, { color: palette.link }]} maxFontSizeMultiplier={1.3}>+{live.length - PREVIEW} more</Text>
            </Pressable>
          ) : null}
        </>
      ) : null}
      <BottomSheet visible={!!pickedItem} onClose={() => setPicked(null)} large>
        {pickedItem ? <Detail key={pickedItem.id} item={pickedItem} roomId={roomId} client={client} onSteer={onSteer} onStop={onStop} /> : null}
      </BottomSheet>
    </View>
  );
}

const styles = StyleSheet.create({
  strip: { borderTopWidth: StyleSheet.hairlineWidth, paddingHorizontal: spacing.lg, paddingVertical: spacing.xs },
  heading: { paddingVertical: spacing.xs },
  headingText: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16 },
  row: { flexDirection: "row", alignItems: "center", gap: spacing.sm, minHeight: 36, paddingVertical: spacing.xs },
  rowText: { flex: 1 },
  goal: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 18 },
  activity: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16 },
  detail: { flex: 1, paddingHorizontal: spacing.lg, gap: spacing.sm },
  title: { fontFamily: fonts.sans, fontSize: 16, lineHeight: 22 },
  steerRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm, borderWidth: 1, borderRadius: 12, padding: spacing.xs, paddingLeft: spacing.md },
  input: { flex: 1, fontFamily: fonts.sans, fontSize: 15, lineHeight: 20, paddingVertical: 0 },
  button: { minHeight: 32, paddingHorizontal: spacing.md, borderRadius: 16, alignItems: "center", justifyContent: "center" },
  buttonText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  disabled: { opacity: 0.5 },
  note: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 },
  tail: { flex: 1, borderRadius: 8, padding: spacing.sm },
  mono: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 17 },
});
