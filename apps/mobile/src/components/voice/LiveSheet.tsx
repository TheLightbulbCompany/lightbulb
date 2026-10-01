import { LIVE_AGENT_IDENTITY, liveErrorText, liveStatus } from "@lightbulb/shared";
import { RoomContext, useIsSpeaking, useParticipantAttributes, useParticipants, useTracks } from "@livekit/components-react";
import { useTrackVolume } from "@livekit/react-native";
import { SymbolView } from "expo-symbols";
import { Track, type Participant } from "livekit-client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing, useChatPalette, type ChatPalette } from "../../config/theme";
import { useLive } from "../../providers/live";
import { Avatar } from "../room/Avatar";
import { LevelRing } from "./LevelRing";

const LIGHTBULB = "Lightbulb";
const TALK = `Talk to ${LIGHTBULB}`;
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

/** Lightbulb up front. A tap asks it in (one talk_start, no hold); saying "Lightbulb, …" does the same.
 *  `participant` is undefined until the worker joins: useIsSpeaking needs one, so the status lives in LightbulbStatus. */
function LightbulbTile({ participant, gaveUp, palette, onTalk, onRetry }: {
  participant: Participant | undefined; gaveUp: boolean; palette: ChatPalette; onTalk: () => void; onRetry: () => void;
}) {
  const mics = useTracks([Track.Source.Microphone]);
  const volume = useTrackVolume(mics.find((t) => t.participant.identity === LIVE_AGENT_IDENTITY));
  const latest = useRef(volume);
  useEffect(() => { latest.current = volume; }, [volume]);
  const level = useCallback(() => latest.current, []);
  return (
    <View style={styles.lightbulb}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={TALK}
        accessibilityHint={`Or say "${LIGHTBULB}, …"`}
        disabled={!participant}
        onPress={onTalk}
      >
        <LevelRing level={level} size={88} color={palette.live}>
          <Avatar name={LIGHTBULB} kind="agent" size={88} palette={palette} />
        </LevelRing>
      </Pressable>
      <Text style={[styles.lightbulbName, { color: palette.fg }]}>{LIGHTBULB}</Text>
      <Text style={[styles.status, { color: palette.fgSoft }]} accessibilityLiveRegion="polite">
        {participant ? <LightbulbStatus participant={participant} /> : gaveUp ? `${LIGHTBULB} didn't join.` : `${LIGHTBULB} is joining…`}
      </Text>
      {!participant && gaveUp && (
        <Pressable accessibilityRole="button" onPress={onRetry} hitSlop={8}>
          <Text style={[styles.hide, { color: palette.link }]}>Retry</Text>
        </Pressable>
      )}
    </View>
  );
}

function LightbulbStatus({ participant }: { participant: Participant }) {
  const speaking = useIsSpeaking(participant);
  const { attributes } = useParticipantAttributes({ participant });
  return <>{speaking ? "Speaking" : liveStatus(attributes)}</>;
}

function Stage() {
  const live = useLive();
  const palette = useChatPalette();
  const participants = useParticipants();
  const ceo = participants.find((p) => p.identity === LIVE_AGENT_IDENTITY);
  const people = participants.filter((p) => p.identity !== LIVE_AGENT_IDENTITY);
  const [gaveUp, setGaveUp] = useState(false);
  useEffect(() => {
    if (ceo || !live.lightbulb) return; // a people-only room: Lightbulb isn't coming
    setGaveUp(false);
    const t = setTimeout(() => setGaveUp(true), JOIN_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [ceo, live.lightbulb]);

  return (
    <>
      {live.lightbulb && (
        <LightbulbTile participant={ceo} gaveUp={gaveUp} palette={palette} onTalk={live.talkStart}
          onRetry={() => { if (live.roomId) void live.join(live.roomId, live.roomName ?? ""); }} />
      )}
      <ScrollView horizontal contentContainerStyle={styles.people} showsHorizontalScrollIndicator={false}>
        {people.map((p) => <PersonTile key={p.identity} participant={p} palette={palette} />)}
      </ScrollView>
      {live.lightbulb && (
        <Text style={[styles.hint, { color: palette.fgMute }]}>Say &ldquo;{LIGHTBULB}, …&rdquo; or tap {LIGHTBULB} to talk to it.</Text>
      )}
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
            <Text style={[styles.errorText, { color: palette.alert }]}>{liveErrorText(live.error)}</Text>
            <Pressable accessibilityRole="button" onPress={() => { live.dismissError(); if (!live.room) live.setOpen(false); }}>
              <Text style={[styles.hide, { color: palette.link }]}>{live.room ? "Dismiss" : "Close"}</Text>
            </Pressable>
          </View>
        )}
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
