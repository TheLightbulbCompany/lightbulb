import { Button, LabeledContent, Picker, Section, Text } from "../ui/swiftui";
import { font, foregroundStyle, pickerStyle, tag } from "@expo/ui/swift-ui/modifiers";
import {
  canChangeListenMode,
  LISTEN_MODE_LABELS,
  listenModeOptions,
  roomDisplayName,
  soleChannelOwner,
  type ListenMode,
  type MemberRef,
  type RoomDetail,
  type RoomMember,
  type UpsertRoomMemberRequest,
} from "@lightbulb/shared";
import { router } from "expo-router";
import { useState } from "react";
import { Alert, Pressable, Text as RNText, useColorScheme, View } from "react-native";

import { chatPalette, fonts, spacing } from "../../config/theme";
import { PickerSheet } from "../teams/PickerSheet";
import { GroupedList } from "../ui/GroupedList";
import { RoomRenameSheet } from "./RoomRenameSheet";

const MENU = [pickerStyle("menu")];

const KIND_LABEL = { channel: "Project", "group-dm": "Group chat", dm: "Direct message" } as const;

export interface RoomDetailsProps {
  room: RoomDetail;
  currentUserId: string;
  /** Workspace people and agents Add members may offer. */
  candidates: readonly (MemberRef & { displayName: string })[];
  upsertMember: (request: UpsertRoomMemberRequest) => Promise<unknown>;
  removeMember: (member: MemberRef) => Promise<unknown>;
  /** Opens the existing picker immediately when launched from the conversation header. */
  initialAddPeopleOpen?: boolean;
  /** Leaves and navigates away; rejects on failure. */
  leaveRoom: () => Promise<unknown>;
  setArchived: (archived: boolean) => Promise<unknown>;
  renameRoom?: (name: string) => Promise<void>;
}

/**
 * Slack's conversation details: who is here, how each agent listens, and the
 * room's actions. Gates mirror the web's RoomPanel + MessagingWorkspaceContext;
 * the backend enforces every one of them again.
 */
export function RoomDetails({
  room,
  currentUserId,
  candidates,
  upsertMember,
  removeMember,
  initialAddPeopleOpen = false,
  leaveRoom,
  setArchived,
  renameRoom,
}: RoomDetailsProps) {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const palette = chatPalette[scheme];
  const BODY = [foregroundStyle(palette.fg)];
  const SECONDARY = [foregroundStyle(palette.fgMute)];
  const CAPTION = [font({ textStyle: "footnote" }), foregroundStyle(palette.fgMute)];
  const ERROR = [foregroundStyle(palette.alert)];
  const DANGER = [foregroundStyle(palette.alert)];

  const viewer = room.members.find((m) => m.kind === "human" && m.id === currentUserId);
  const others = room.members.filter((m) => !(m.kind === "human" && m.id === currentUserId));
  const isAdmin = viewer?.role === "owner" || viewer?.role === "admin";
  const isChannel = room.kind === "channel";
  const noun = room.kind === "group-dm" ? "group chat" : "project";
  const canChangeMode = canChangeListenMode(viewer?.role, room);
  const canManageMembers = isChannel && isAdmin;
  // A session's chat: its people follow who can read the session and it opens and closes with the
  // session, so the backend refuses every change by hand except its owner's agents.
  const managed = !!room.session;
  const canAdd = !room.shared && (!isChannel || isAdmin);
  const [error, setError] = useState<string | null>(null);
  // Keep URL-driven opening behind the same permission predicate as the
  // details button; a hand-edited query string must not reveal the picker.
  const [sheet, setSheet] = useState<"add" | "remove" | "rename" | null>(initialAddPeopleOpen && canAdd ? "add" : null);
  const removable = canManageMembers
    ? room.members.filter((m) => m.role !== "owner" && !((room.shared || managed) && m.kind === "human"))
    : [];
  const addable = candidates.filter((c) => !(managed && c.kind === "human") && !room.members.some((m) => m.kind === c.kind && m.id === c.id));
  // A group chat's creator holds no powers there, so anyone may leave or archive
  // one; a channel owner leaving hands it to another person, so only an owner alone
  // in it may not leave; never the shared room.
  const canLeave = !room.shared && !managed && (room.kind === "group-dm" || (isChannel && !!viewer && !soleChannelOwner(room, currentUserId)));
  const canArchive = !room.shared && !managed && !room.isDefault && (room.kind === "group-dm" || (isChannel && isAdmin));
  const canRename = Boolean(renameRoom && !room.shared && !managed && (room.kind === "group-dm" || (isChannel && isAdmin)));

  const run = (action: () => Promise<unknown>, fallback: string) => {
    setError(null);
    action().catch((caught: unknown) => setError(caught instanceof Error && caught.message ? caught.message : fallback));
  };

  const changeMode = (agent: RoomMember, listenMode: ListenMode) =>
    run(() => upsertMember({ kind: "agent", id: agent.id, role: "member", listenMode }), "The response setting could not be saved.");

  const confirm = (title: string, action: string, onConfirm: () => void) =>
    Alert.alert(title, undefined, [
      { text: "Cancel", style: "cancel" },
      { text: action, style: "destructive", onPress: onConfirm },
    ]);

  const title = roomDisplayName(room, others);
  const count = room.members.length;

  return (
    <View style={{ flex: 1, backgroundColor: palette.ground }}>
      <GroupedList colorScheme={scheme}>
        <Section footer={room.shared ? <Text>Shared with Lightbulb</Text> : undefined}>
          <Text modifiers={BODY}>{title}</Text>
          <Text modifiers={SECONDARY}>{`${KIND_LABEL[room.kind]} · ${count} ${count === 1 ? "member" : "members"}`}</Text>
          {room.topic ? <Text modifiers={CAPTION}>{room.topic}</Text> : null}
        </Section>

        <Section title="Members">
          {room.members.map((m) => {
            const key = `${m.kind}:${m.id}`;
            const name = m.kind === "human" && m.id === currentUserId ? `${m.displayName} (you)` : m.displayName;
            if (m.kind === "human" || room.kind === "dm") {
              const role = m.role === "owner" ? "Owner" : m.role === "admin" ? "Admin" : m.kind === "agent" ? "Agent" : "";
              return (
                <LabeledContent key={key} label={<Text modifiers={BODY}>{name}</Text>}>
                  <Text modifiers={SECONDARY}>{role}</Text>
                </LabeledContent>
              );
            }
            const mode = m.listenMode ?? "mentions";
            if (!canChangeMode) {
              return (
                <LabeledContent key={key} label={<Text modifiers={BODY}>{name}</Text>}>
                  <Text modifiers={SECONDARY}>{LISTEN_MODE_LABELS[mode]}</Text>
                </LabeledContent>
              );
            }
            return (
              <Picker
                key={key}
                label={name}
                selection={mode}
                onSelectionChange={(next: ListenMode) => changeMode(m, next)}
                modifiers={MENU}
              >
                {listenModeOptions().map((option) => (
                  <Text key={option.value} modifiers={[tag(option.value)]}>
                    {option.label}
                  </Text>
                ))}
              </Picker>
            );
          })}
        </Section>

        {canAdd || removable.length > 0 || canRename || canArchive || canLeave || error ? (
          <Section footer={error ? <Text modifiers={ERROR}>{error}</Text> : undefined}>
            {canAdd ? <Button label={isChannel ? "Add members" : "Add people"} onPress={() => setSheet("add")} /> : null}
            {removable.length > 0 ? <Button label="Remove members" onPress={() => setSheet("remove")} /> : null}
            {canRename ? <Button label={`Rename ${noun}`} onPress={() => setSheet("rename")} /> : null}
            {canArchive ? (
              <Button
                label={room.archivedAt ? `Unarchive ${noun}` : `Archive ${noun}`}
                onPress={() => run(() => setArchived(!room.archivedAt), `The ${noun} could not be archived.`)}
              />
            ) : null}
            {canLeave ? (
              <Button
                label={`Leave ${noun}`}
                modifiers={DANGER}
                onPress={() => confirm(`Leave this ${noun}?`, "Leave", () => run(leaveRoom, `You could not leave this ${noun}.`))}
              />
            ) : null}
          </Section>
        ) : null}
      </GroupedList>

      <PickerSheet
        visible={sheet === "add"}
        heading="Add people"
        searchPlaceholder="Search people"
        options={[
          ...room.members.map((member) => ({
            key: `current:${member.kind}:${member.id}`,
            label: `${member.displayName} (current member)`,
            section: "Current members",
            disabled: true,
          })),
          ...addable.map((c) => ({ key: `${c.kind}:${c.id}`, label: c.displayName, section: c.kind === "agent" ? "Eligible agents" : "Eligible people" })),
        ]}
        onSelect={(key) => {
          const c = addable.find((entry) => `${entry.kind}:${entry.id}` === key);
          if (!c) return;
          run(
            () => upsertMember({ kind: c.kind, id: c.id, role: "member", listenMode: c.kind === "agent" ? "active" : null }),
            `${c.displayName} could not be added.`,
          );
        }}
        onClose={() => setSheet(null)}
        // A session's chat takes no people by hand, so no invite there either.
        footer={managed ? null : (
          <Pressable
            accessibilityRole="button"
            onPress={() => {
              setSheet(null);
              router.push("/invite");
            }}
            style={{ minHeight: 44, justifyContent: "center", paddingHorizontal: spacing.sm }}
          >
            <RNText style={{ color: palette.link, fontFamily: fonts.sans, fontSize: 14 }}>
              Not in the workspace yet? Invite them by email
            </RNText>
          </Pressable>
        )}
      />
      <PickerSheet
        visible={sheet === "remove"}
        heading="Remove members"
        options={removable.map((m) => ({ key: `${m.kind}:${m.id}`, label: m.displayName }))}
        onSelect={(key) => {
          const m = removable.find((entry) => `${entry.kind}:${entry.id}` === key);
          if (!m) return;
          confirm(`Remove ${m.displayName}?`, "Remove", () =>
            run(() => removeMember({ kind: m.kind, id: m.id }), `${m.displayName} could not be removed.`));
        }}
        onClose={() => setSheet(null)}
      />
      {renameRoom ? (
        <RoomRenameSheet
          visible={sheet === "rename"}
          roomName={room.name ?? roomDisplayName(room, room.members)}
          onRename={renameRoom}
          onClose={() => setSheet(null)}
        />
      ) : null}
    </View>
  );
}
