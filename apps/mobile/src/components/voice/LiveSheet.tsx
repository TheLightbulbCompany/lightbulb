import {
  AGENTS_JOINING, agentA11yName, agentLiveStatus, agentStateLine, agentWorking, floorChange, floorHolder, LIVE_AGENT_IDENTITY, liveAgents,
  liveErrorText, liveHappening, liveHint, LISTENING_TO, nameCollides, orderAgentSlots, queuePosition, voiceFailedText, wakingText,
} from "@lightbulb/shared";
import { RoomContext, useIsSpeaking, useParticipants, useTracks } from "@livekit/components-react";
import { useTrackVolume } from "@livekit/react-native";
import { SymbolView } from "../ui/SymbolView";
import { Track, type Participant } from "livekit-client";
import { useCallback, useEffect, useRef, useState } from "react";
import { AccessibilityInfo, Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing, useChatPalette, type ChatPalette } from "../../config/theme";
import { useLive } from "../../providers/live";
import { Avatar } from "./Avatar";
import { LevelRing } from "./LevelRing";
import { NoticeBanner } from "./NoticeBanner";

const JOIN_TIMEOUT_MS = 45_000; // the worker joins in ~20 s; past this it isn't coming (the web's rule)

function PersonTile({ participant, palette }: { participant: Participant; palette: ChatPalette }) {
  const speaking = useIsSpeaking(participant);
  const name = participant.name || participant.identity;
  return (
    <View style={styles.person} accessible accessibilityLabel={speaking ? `${name}, speaking` : name}>
      <View style={[styles.speakRing, { borderColor: speaking ? palette.live : "transparent" }]}>
        <Avatar name={name} kind="human" size={52} palette={palette} />
      </View>
      <Text style={[styles.personName, { color: palette.fgSoft }]} numberOfLines={1}>{name}</Text>
    </View>
  );
}

const hasLimit = (code?: string) => code === "voice_limit" || code === "voice_busy";

/** One agent in the row: its state pip, queue badge or raised hand. The floor-holder's tap is a no-op. */
function AgentTile({ id, name, attrs, isHolder, disabled, ambiguous, failed, palette, onTalk }: {
  id: string; name: string; attrs: Readonly<Record<string, string>>; isHolder: boolean; disabled: boolean; ambiguous: boolean; failed: boolean; palette: ChatPalette; onTalk: (id: string) => void;
}) {
  const status = agentLiveStatus(attrs, id);
  const extras = {
    working: agentWorking(attrs, id), queuePos: queuePosition(attrs, id),
    listeningTo: attrs[LISTENING_TO] || undefined,
  };
  const live = status === "speaking" || status === "joining" || status === "listening";
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`${agentA11yName(name, status, extras)}${failed ? ", couldn't join" : ""}`}
      accessibilityHint={`Talks to ${name}. Or say "${name}"`}
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={() => { if (!isHolder) onTalk(id); }}
      style={styles.agent}
    >
      <View style={[styles.outline, { borderColor: ambiguous ? palette.link : "transparent" }]}>
        <Avatar name={name} kind="agent" size={56} palette={palette} />
      </View>
      {failed ? (
        <View style={[styles.pip, { backgroundColor: palette.alert }]} />
      ) : extras.queuePos ? (
        <View style={[styles.badge, { backgroundColor: palette.fg }]}><Text style={[styles.badgeText, { color: palette.onFg }]}>{extras.queuePos}</Text></View>
      ) : status === "has_news" ? (
        <View style={[styles.badge, { backgroundColor: palette.live }]}>
          <SymbolView accessibilityElementsHidden name="hand.raised.fill" size={12} tintColor={palette.onFg} />
        </View>
      ) : live || status === "working" ? (
        <View style={[styles.pip, { backgroundColor: live ? palette.live : palette.fgMute }]} />
      ) : null}
      <Text style={[styles.personName, { color: palette.fgSoft }]} numberOfLines={1}>{name}</Text>
    </Pressable>
  );
}

function Stage() {
  const live = useLive();
  const palette = useChatPalette();
  const participants = useParticipants();
  const mics = useTracks([Track.Source.Microphone]);
  const people = participants.filter((p) => p.identity !== LIVE_AGENT_IDENTITY);
  const attrs = live.workerAttrs;
  const tiles = liveAgents(live.roster);
  const nameOf = useCallback((id: string) => live.roster.find((m) => m.id === id)?.displayName ?? id, [live.roster]);
  const ids = orderAgentSlots(tiles.map((m) => m.id), attrs);

  const holder = floorHolder(attrs);
  const stageId = holder ?? ids.find((id) => agentLiveStatus(attrs, id) === "joining") ?? live.lastSpeaker ?? tiles[0]?.id ?? null;
  const stageStatus = stageId ? agentLiveStatus(attrs, stageId) : "idle";
  const stageExtras = stageId
    ? { working: agentWorking(attrs, stageId), queuePos: queuePosition(attrs, stageId), listeningTo: attrs[LISTENING_TO] || undefined }
    : {};

  // Two agent tracks can coexist during a switch (C7): pick by name.
  const volume = useTrackVolume(stageId ? mics.find((t) => t.publication?.trackName === `agent-${stageId}`) : undefined);
  const latest = useRef(volume);
  useEffect(() => { latest.current = volume; }, [volume]);
  const level = useCallback(() => latest.current, []);

  const [reduced, setReduced] = useState(false);
  useEffect(() => { void AccessibilityInfo.isReduceMotionEnabled().then(setReduced).catch(() => {}); }, []);

  // Absent for 45 s -> "couldn't join" with Retry.
  const [gaveUp, setGaveUp] = useState(false);
  useEffect(() => {
    if (live.workerPresent || !live.lightbulb) return; // a people-only room: no agents are coming
    setGaveUp(false);
    const t = setTimeout(() => setGaveUp(true), JOIN_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [live.workerPresent, live.lightbulb]);

  // Announce each floor change once.
  const prev = useRef<{ attrs: typeof attrs; ids: string[] } | null>(null);
  const idsKey = ids.join(",");
  useEffect(() => {
    const p = prev.current;
    prev.current = { attrs, ids };
    if (!p) return;
    const msg = floorChange(p.attrs, attrs, p.ids, ids, nameOf);
    if (msg) AccessibilityInfo.announceForAccessibility(msg);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attrs, idsKey]);

  // Notices (UI §3, §6): a bare name that is also a person's -> a 6 s chip + outline; a failed voice -> an alert tile
  // until that agent's status changes; a tap on it is the retry.
  // Consumed once: a closed Modal unmounts the Stage, so an unconsumed notice would replay on every reopen.
  const { agentNotice, dismissAgentNotice } = live;
  const [ambiguous, setAmbiguous] = useState<string | null>(null);
  const [failedAt, setFailedAt] = useState<{ id: string; status: string } | null>(null);
  const attrsRef = useRef(attrs);
  attrsRef.current = attrs;
  useEffect(() => {
    if (!agentNotice) return;
    if (agentNotice.kind === "ambiguous_name") setAmbiguous(agentNotice.agentId);
    if (agentNotice.kind === "voice_failed") setFailedAt({ id: agentNotice.agentId, status: agentLiveStatus(attrsRef.current, agentNotice.agentId) });
    dismissAgentNotice();
  }, [agentNotice, dismissAgentNotice]);
  useEffect(() => {
    if (!ambiguous) return;
    const t = setTimeout(() => setAmbiguous(null), 6_000);
    return () => clearTimeout(t);
  }, [ambiguous]);
  const failedId = failedAt && agentLiveStatus(attrs, failedAt.id) === failedAt.status ? failedAt.id : null;

  const limited = hasLimit(live.error?.code);
  const stageName = stageId ? nameOf(stageId) : "";
  const hint = liveHint(tiles.map((m) => ({ name: m.displayName, collides: nameCollides(m.displayName, live.roster.filter((x) => x.kind === "human")) })));
  const happening = liveHappening(attrs, nameOf);
  const talk = live.talkStart;
  const retry = () => { if (live.roomId) void live.join(live.roomId, live.roomName ?? ""); };
  const avatar = <Avatar name={stageName || "Agent"} kind="agent" size={88} palette={palette} />;

  return (
    <>
      {live.lightbulb && stageId && (
        <View style={styles.lightbulb} accessible accessibilityLabel={agentA11yName(stageName, stageStatus, stageExtras)}>
          {stageStatus === "speaking" && !reduced ? <LevelRing level={level} size={88} color={palette.live}>{avatar}</LevelRing> : avatar}
          <Text style={[styles.lightbulbName, { color: palette.fg }]}>{stageName}</Text>
          <Text style={[styles.status, { color: palette.fgSoft }]} accessibilityLiveRegion="polite">
            {live.workerPresent ? agentStateLine(stageStatus, stageExtras) || (holder ? "" : "Tap someone or say their name") : gaveUp ? "" : AGENTS_JOINING}
          </Text>
          {!!happening && <Text style={[styles.status, { color: palette.fgSoft }]}>{happening}</Text>}
        </View>
      )}
      {live.lightbulb && !live.workerPresent && gaveUp && (
        <View style={styles.lightbulb}>
          <Text style={[styles.status, { color: palette.alert }]}>{liveErrorText({ code: "agents_absent" })}</Text>
          <Pressable accessibilityRole="button" onPress={retry} hitSlop={8}>
            <Text style={[styles.hide, { color: palette.link }]}>Retry</Text>
          </Pressable>
        </View>
      )}
      {!!ambiguous && (
        <Text style={[styles.hint, { color: palette.link }]} accessibilityRole="alert">
          Meant {nameOf(ambiguous)} the agent? Say &quot;hey {nameOf(ambiguous)}&quot; or tap
        </Text>
      )}
      {!!failedId && <Text style={[styles.hint, { color: palette.alert }]} accessibilityRole="alert">{voiceFailedText(nameOf(failedId))}</Text>}
      {live.lightbulb && ids.length > 0 && (
        <>
          <Text style={[styles.caption, { color: palette.fgMute }]}>Agents</Text>
          <ScrollView horizontal contentContainerStyle={styles.people} showsHorizontalScrollIndicator={false}>
            {ids.map((id) => (
              <AgentTile key={id} id={id} name={nameOf(id)} attrs={attrs} isHolder={id === holder} disabled={limited || !live.workerPresent}
                ambiguous={id === ambiguous} failed={id === failedId}
                palette={palette} onTalk={talk} />
            ))}
          </ScrollView>
        </>
      )}
      <Text style={[styles.caption, { color: palette.fgMute }]}>People</Text>
      <ScrollView horizontal contentContainerStyle={styles.people} showsHorizontalScrollIndicator={false}>
        {people.map((p) => <PersonTile key={p.identity} participant={p} palette={palette} />)}
      </ScrollView>
      {live.lightbulb && hint && <Text style={[styles.hint, { color: palette.fgMute }]}>{hint}</Text>}
      <View style={styles.controls}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={live.muted ? "Unmute" : "Mute"}
          accessibilityState={{ selected: live.muted }}
          onPress={() => live.setMuted(!live.muted)}
          style={[styles.round, { backgroundColor: live.muted ? palette.fg : palette.chip }]}
        >
          <SymbolView accessibilityElementsHidden name={live.muted ? "mic.slash.fill" : "mic.fill"} size={24} tintColor={live.muted ? palette.onFg : palette.fg} />
        </Pressable>
        <Pressable accessibilityRole="button" accessibilityLabel="Leave Live" onPress={live.leave} style={[styles.round, { backgroundColor: palette.alert }]}>
          <SymbolView accessibilityElementsHidden name="phone.down.fill" size={24} tintColor={palette.onFg} />
        </Pressable>
      </View>
    </>
  );
}

/** A room's Live: Lightbulb up front (unless people-only), people with speaking rings, the mic toggle and leave.
 *  Swiping it down hides it; the room and its audio stay until Leave. */
export function LiveSheet() {
  const live = useLive();
  const palette = useChatPalette();
  const wakingName = live.roster.find((m) => m.kind === "agent")?.displayName ?? "Your team";
  return (
    <Modal visible={live.open} animationType="slide" presentationStyle="pageSheet" onRequestClose={() => live.setOpen(false)}>
      <View style={[styles.sheet, { backgroundColor: palette.ground }]}>
        <View style={styles.header}>
          <Text style={[styles.title, { color: palette.fg }]} accessibilityRole="header" numberOfLines={1}>
            🔴 Live{live.roomName ? ` · ${live.roomName}` : ""}
          </Text>
          <Pressable accessibilityRole="button" onPress={() => live.setOpen(false)} hitSlop={8}>
            <Text style={[styles.hide, { color: palette.link }]}>Hide</Text>
          </Pressable>
        </View>
        {live.error && (
          <View style={[styles.error, { backgroundColor: palette.surface }]} accessibilityRole="alert">
            <Text style={[styles.errorText, { color: palette.alert }]}>{live.error.code === "workspace_not_ready" ? wakingText(wakingName) : liveErrorText(live.error)}</Text>
            <Pressable accessibilityRole="button" onPress={() => { live.dismissError(); if (!live.room) live.setOpen(false); }}>
              <Text style={[styles.hide, { color: palette.link }]}>{live.room ? "Dismiss" : "Close"}</Text>
            </Pressable>
          </View>
        )}
        <NoticeBanner />
        {live.room ? (
          <RoomContext.Provider value={live.room}><Stage /></RoomContext.Provider>
        ) : !live.error ? (
          <Text style={[styles.status, { color: palette.fgSoft }]}>Joining…</Text>
        ) : null}
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  sheet: { flex: 1, padding: spacing.xl, gap: spacing.xl },
  header: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.md },
  title: { fontFamily: fonts.sansSemiBold, fontSize: 20, flexShrink: 1 },
  hide: { fontSize: 17 },
  error: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: spacing.md, padding: spacing.md, borderRadius: radii.lg },
  errorText: { flex: 1, fontSize: 15 },
  lightbulb: { alignItems: "center", gap: spacing.xs },
  caption: { fontSize: 13, fontFamily: fonts.sansSemiBold, textTransform: "uppercase" },
  outline: { borderWidth: 2, borderRadius: 32, padding: 1 },
  agent: { alignItems: "center", width: 64, gap: spacing.xs },
  badge: { position: "absolute", top: -2, right: 2, minWidth: 18, height: 18, borderRadius: 9, alignItems: "center", justifyContent: "center" },
  badgeText: { fontSize: 11, fontFamily: fonts.sansSemiBold },
  pip: { position: "absolute", top: 2, right: 6, width: 10, height: 10, borderRadius: 5 },
  lightbulbName: { fontFamily: fonts.sansSemiBold, fontSize: 22 },
  status: { fontSize: 15, textAlign: "center" },
  people: { gap: spacing.lg, paddingHorizontal: spacing.xs },
  person: { alignItems: "center", width: 72, gap: spacing.xs },
  speakRing: { borderWidth: 3, borderRadius: radii.lg + 4, padding: 2 },
  personName: { fontSize: 13 },
  hint: { fontSize: 14, textAlign: "center" },
  controls: { flexDirection: "row", justifyContent: "center", gap: spacing.xl * 2 },
  round: { width: 64, height: 64, borderRadius: 32, alignItems: "center", justifyContent: "center" },
});
