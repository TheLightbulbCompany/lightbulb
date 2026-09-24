import { Button, LabeledContent, Picker, Section, Text } from "@expo/ui/swift-ui";
import { disabled, font, foregroundStyle, pickerStyle, tag } from "@expo/ui/swift-ui/modifiers";
import {
  ApiError,
  canChangeListenMode,
  LISTEN_MODE_LABELS,
  listenModeOptions,
  roomDisplayName,
  type ListenMode,
  type MemberRef,
  type RoomDetail,
  type RoomMember,
  type UpsertRoomMemberRequest,
} from "@lightbulb/shared";
import { useState } from "react";
import { Alert, View } from "react-native";

import { colors } from "../../config/theme";
import { defaultAgentListenMode } from "../../lib/subscription";
import { PickerSheet } from "../teams/PickerSheet";
import { GroupedList } from "../ui/GroupedList";

const BODY = [foregroundStyle(colors.ink)];
const SECONDARY = [foregroundStyle(colors.mute)];
const CAPTION = [font({ textStyle: "footnote" }), foregroundStyle(colors.mute)];
const ERROR = [foregroundStyle(colors.errorText)];
const DANGER = [foregroundStyle(colors.danger)];
const MENU = [pickerStyle("menu")];

const KIND_LABEL = { channel: "Channel", "group-dm": "Group chat", dm: "Direct message" } as const;
const PLAN_REQUIRED = "Listening is part of the paid plan. Free-tier agents reply when mentioned.";

/** A 403 PLAN_REQUIRED gets the plan copy; anything else the action's own line. */
function failure(error: unknown, fallback: string): string {
  if (error instanceof ApiError && JSON.stringify(error.body ?? "").includes("PLAN_REQUIRED")) return PLAN_REQUIRED;
  return fallback;
}

export interface RoomDetailsProps {
  room: RoomDetail;
  currentUserId: string;
  /** Billing's `is_subscribed`; undefined while loading or unreadable. */
  subscribed: boolean | undefined;
  /** Workspace people and agents Add members may offer. */
  candidates: readonly (MemberRef & { displayName: string })[];
  upsertMember: (request: UpsertRoomMemberRequest) => Promise<unknown>;
  removeMember: (member: MemberRef) => Promise<unknown>;
  /** Leaves and navigates away; rejects on failure. */
  leaveRoom: () => Promise<unknown>;
  setArchived: (archived: boolean) => Promise<unknown>;
}

/**
 * Slack's conversation details: who is here, how each agent listens, and the
 * room's actions. Gates mirror the web's RoomPanel + MessagingWorkspaceContext;
 * the backend enforces every one of them again.
 */
export function RoomDetails({
  room,
  currentUserId,
  subscribed,
  candidates,
  upsertMember,
  removeMember,
  leaveRoom,
  setArchived,
}: RoomDetailsProps) {
  const [error, setError] = useState<string | null>(null);
  const [sheet, setSheet] = useState<"add" | "remove" | null>(null);

  const viewer = room.members.find((m) => m.kind === "human" && m.id === currentUserId);
  const others = room.members.filter((m) => !(m.kind === "human" && m.id === currentUserId));
  const isAdmin = viewer?.role === "owner" || viewer?.role === "admin";
  const isChannel = room.kind === "channel";
  const noun = room.kind === "group-dm" ? "group chat" : "channel";
  // The web shows Listening as available until billing says otherwise, so a
  // paid room never flashes the lock; who may CHANGE it reads `=== true`.
  const listeningAvailable = subscribed ?? true;
  const canChangeMode = canChangeListenMode(viewer?.role, subscribed, room);
  const canManageMembers = isChannel && isAdmin;
  const canAdd = canManageMembers && !room.shared;
  const removable = canManageMembers
    ? room.members.filter((m) => m.role !== "owner" && !(room.shared && m.kind === "human"))
    : [];
  const addable = candidates.filter((c) => !room.members.some((m) => m.kind === c.kind && m.id === c.id));
  // A group chat's creator holds no powers there, so anyone may leave or archive
  // one; a channel owner may not leave; never the shared room.
  const canLeave = !room.shared && (room.kind === "group-dm" || (isChannel && !!viewer && viewer.role !== "owner"));
  const canArchive = !room.shared && !room.isDefault && (room.kind === "group-dm" || (isChannel && isAdmin));

  const run = (action: () => Promise<unknown>, fallback: string) => {
    setError(null);
    action().catch((cause: unknown) => setError(failure(cause, fallback)));
  };

  const changeMode = (agent: RoomMember, listenMode: ListenMode) =>
    run(() => upsertMember({ kind: "agent", id: agent.id, role: "member", listenMode }), "The response setting could not be saved.");

  const confirm = (title: string, action: string, onConfirm: () => void) =>
    Alert.alert(title, undefined, [
      { text: "Cancel", style: "cancel" },
      { text: action, style: "destructive", onPress: onConfirm },
    ]);

  const title = `${isChannel ? "#" : ""}${roomDisplayName(room, others)}`;
  const count = room.members.length;

  return (
    <View style={{ flex: 1 }}>
      <GroupedList>
        <Section footer={room.shared ? <Text>Shared with Lightbulb</Text> : undefined}>
          <Text modifiers={BODY}>{title}</Text>
          <Text modifiers={SECONDARY}>{`${KIND_LABEL[room.kind]} · ${count} ${count === 1 ? "member" : "members"}`}</Text>
          {room.topic ? <Text modifiers={CAPTION}>{room.topic}</Text> : null}
        </Section>

        <Section
          title="Members"
          footer={isAdmin && canChangeMode && subscribed === false
            ? <Text>Listening lets an agent reply without being mentioned. It is part of the paid plan.</Text>
            : undefined}
        >
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
            const stored = m.listenMode ?? "mentions";
            // On the free tier a stored Listening runs as Mentions, so show what it does.
            const mode = stored === "active" && !listeningAvailable ? "mentions" : stored;
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
                {listenModeOptions(listeningAvailable).map((option) => (
                  <Text key={option.value} modifiers={[tag(option.value), disabled(option.disabled)]}>
                    {option.label}
                  </Text>
                ))}
              </Picker>
            );
          })}
        </Section>

        {canAdd || removable.length > 0 || canArchive || canLeave || error ? (
          <Section footer={error ? <Text modifiers={ERROR}>{error}</Text> : undefined}>
            {canAdd ? <Button label="Add members" onPress={() => setSheet("add")} /> : null}
            {removable.length > 0 ? <Button label="Remove members" onPress={() => setSheet("remove")} /> : null}
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
                onPress={() => confirm(`Leave this ${noun}?`, "Leave", () => run(leaveRoom, "You could not leave this conversation."))}
              />
            ) : null}
          </Section>
        ) : null}
      </GroupedList>

      <PickerSheet
        visible={sheet === "add"}
        heading="Add members"
        options={addable.map((c) => ({ key: `${c.kind}:${c.id}`, label: c.displayName, section: c.kind === "agent" ? "Agents" : "People" }))}
        onSelect={(key) => {
          const c = addable.find((entry) => `${entry.kind}:${entry.id}` === key);
          if (!c) return;
          run(
            () => upsertMember({ kind: c.kind, id: c.id, role: "member", listenMode: c.kind === "agent" ? defaultAgentListenMode(subscribed) : null }),
            `${c.displayName} could not be added.`,
          );
        }}
        onClose={() => setSheet(null)}
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
    </View>
  );
}
