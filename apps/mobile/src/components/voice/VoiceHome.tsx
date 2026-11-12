import { createApiClient, usPhone, type RoomSummary } from "@lightbulb/shared";
import { router, useFocusEffect } from "expo-router";
import { useCallback, useMemo, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from "react-native";

import { fonts, useChatPalette } from "../../config/theme";
import { useTeamsConfig } from "../../lib/teamsData";
import { useLive } from "../../providers/live";
import { LogoMark } from "../LogoMark";

export interface VoiceTimelineItem {
  type: "call" | "meeting"; roomId: string; at: number;
  callerName?: string; caller?: string; title?: string; summary?: string; outcome?: string;
  steps?: { text: string; guessed?: boolean }[];
}

/** Only recorded, non-guessed activity can stand in for a missing summary. */
export function timelineSummary(item: VoiceTimelineItem): string {
  return item.summary?.trim() || [...(item.steps ?? [])].reverse().find((step) => !step.guessed && step.text.trim())?.text.trim() || "Open conversation";
}

export function assistantRoom(rooms: readonly RoomSummary[], agentId?: string | null): RoomSummary | undefined {
  return rooms.find((room) => room.firstRun) ?? (agentId ? rooms.find((room) => room.kind === "dm" && room.members.some((member) => member.kind === "agent" && member.id === agentId)) : undefined);
}

/** The member's own text chats with the assistant: a DM or chat whose only agent is it and no other person. */
export function textChats(rooms: readonly RoomSummary[], agentId?: string | null): RoomSummary[] {
  if (!agentId) return [];
  return rooms.filter((room) => (room.kind === "dm" || room.kind === "group-dm")
    && room.members.some((m) => m.kind === "agent" && m.id === agentId)
    && room.members.filter((m) => m.kind === "human").length <= 1);
}

type HomeRow = { key: string; roomId: string; title: string; summary: string; at: number };

/** Home: the orb, New chat, and the member's text chats listed with calls and meetings, newest first. */
export function VoiceHome({ rooms, agentId, onNewChat }: {
  rooms: readonly RoomSummary[]; agentId?: string | null; onNewChat?: () => Promise<string>;
}) {
  const palette = useChatPalette();
  const live = useLive();
  const room = assistantRoom(rooms, agentId);
  const adapter = useTeamsConfig();
  const api = useMemo(() => createApiClient(adapter), [adapter]);
  const [items, setItems] = useState<VoiceTimelineItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [creating, setCreating] = useState(false);
  const [createFailed, setCreateFailed] = useState(false);
  useFocusEffect(useCallback(() => {
    let alive = true;
    setLoading(true);
    void (api.get("/voice/timeline") as Promise<{ items: VoiceTimelineItem[] }>).then((result) => {
      if (alive) { setItems(result.items); setFailed(false); }
    }).catch(() => { if (alive) setFailed(true); }).finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [api, retry]));
  const rows: HomeRow[] = [
    ...items.map((item) => ({
      key: `${item.type}:${item.roomId}`, roomId: item.roomId, at: item.at, summary: timelineSummary(item),
      title: item.type === "meeting" ? item.title || "Meeting" : item.callerName || (item.caller ? usPhone(item.caller) : "Unknown caller"),
    })),
    ...textChats(rooms, agentId).map((chat) => ({
      key: `chat:${chat.roomId}`, roomId: chat.roomId, at: Date.parse(chat.lastMessageAt ?? chat.updatedAt) || 0,
      title: chat.name || "Chat with Lightbulb", summary: chat.lastMessage?.textPreview || "No messages yet",
    })),
  ].sort((a, b) => b.at - a.at).slice(0, 20);
  const newChat = async () => {
    if (!onNewChat || creating) return;
    setCreating(true); setCreateFailed(false);
    try { router.push(`/(app)/room/${await onNewChat()}`); } catch { setCreateFailed(true); } finally { setCreating(false); }
  };
  const active = !!room && live.roomId === room.roomId;
  const elsewhere = !!(live.roomId || live.joining) && !active && live.joining !== room?.roomId;
  const joining = !!room && live.joining === room.roomId;
  const text = { color: palette.fg, fontFamily: fonts.sans, fontSize: 16 };
  return <View style={styles.home}>
    <View style={styles.hero}>
      <Pressable accessibilityRole="button" accessibilityLabel={active ? "Open live conversation with Lightbulb" : "Talk to Lightbulb"}
        accessibilityHint="Starts a voice conversation using your microphone"
        accessibilityState={{ disabled: !room || elsewhere || joining, busy: joining }}
        disabled={!room || elsewhere || joining}
        onPress={() => { if (active) live.setOpen(true); else if (room) void live.join(room.roomId, "Lightbulb"); }}
        style={({ pressed }) => [styles.orb, { backgroundColor: palette.raised, borderColor: active ? palette.live : palette.edge, opacity: pressed ? 0.65 : 1 }]}>
        {joining ? <ActivityIndicator color={palette.fg} /> : <LogoMark size={64} decorative />}
      </Pressable>
      <Text style={[text, styles.title]}>Lightbulb</Text>
      <Text accessibilityLiveRegion="polite" style={[text, styles.subtitle, { color: palette.fgSoft }]}>
        {elsewhere ? "A live conversation is already open" : joining ? "Connecting…" : active ? "Your live conversation is open" : "Talk it through. Keep things moving."}
      </Text>
    </View>
    <View style={styles.links}>
      <Pressable accessibilityRole="button" accessibilityState={{ disabled: !onNewChat || !agentId || creating, busy: creating }}
        disabled={!onNewChat || !agentId || creating} onPress={() => void newChat()} style={styles.action}>
        <Text style={[text, { color: onNewChat && agentId ? palette.link : palette.fgMute }]}>{creating ? "Starting…" : "New chat"}</Text>
      </Pressable>
      <Pressable accessibilityRole="button" onPress={() => router.navigate("/(app)/(tabs)/activity")} style={styles.action}><Text style={[text, { color: palette.link }]}>Needs you</Text></Pressable>
    </View>
    {createFailed ? <Text accessibilityRole="alert" style={[text, { color: palette.alert }]}>Couldn’t start a chat. Try again.</Text> : null}
    <Text accessibilityRole="header" style={[text, styles.heading]}>Recent</Text>
    {loading && rows.length === 0 ? <ActivityIndicator accessibilityLabel="Loading recent calls, meetings and chats" color={palette.fg} /> : null}
    {failed ? <Pressable accessibilityRole="button" onPress={() => setRetry((n) => n + 1)} style={styles.action}><Text style={[text, { color: palette.alert }]}>Couldn’t load recent activity. Retry</Text></Pressable> : null}
    {!loading && !failed && rows.length === 0 ? <Text style={[text, { color: palette.fgSoft }]}>Calls, meetings and chats will appear here.</Text> : null}
    {rows.map((row) => <Pressable key={row.key} accessibilityRole="button" onPress={() => router.push(`/(app)/room/${row.roomId}`)}
      style={[styles.row, { borderColor: palette.edge }]}>
      <Text style={text}>{row.title}</Text>
      <Text style={[text, styles.subtitle, { color: palette.fgSoft }]} numberOfLines={2}>{row.summary}</Text>
      {row.at ? <Text style={[text, styles.date, { color: palette.fgMute }]}>{new Date(row.at).toLocaleString()}</Text> : null}
    </Pressable>)}
  </View>;
}

const styles = StyleSheet.create({
  home: { paddingHorizontal: 20, paddingTop: 12, paddingBottom: 12 },
  hero: { alignItems: "center", paddingVertical: 20, gap: 12 },
  orb: { height: 112, width: 112, borderRadius: 56, borderWidth: 1, alignItems: "center", justifyContent: "center" },
  title: { fontSize: 26, fontFamily: fonts.sansMedium },
  subtitle: { fontSize: 14, lineHeight: 21, textAlign: "left" },
  action: { minHeight: 44, justifyContent: "center", paddingHorizontal: 4 },
  links: { flexDirection: "row", flexWrap: "wrap", justifyContent: "space-between", gap: 12 },
  heading: { fontFamily: fonts.sansMedium, marginTop: 24, marginBottom: 12 },
  row: { paddingVertical: 14, borderBottomWidth: StyleSheet.hairlineWidth, gap: 5 },
  date: { fontSize: 12 },
});
