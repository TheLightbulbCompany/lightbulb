import { useAuth, useOrganization } from "@clerk/clerk-expo";
import { createApiClient } from "@lightbulb/shared";
import { SymbolView } from "expo-symbols";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ActivityIndicator, Pressable, RefreshControl, ScrollView, Text, TextInput, View } from "react-native";

import { insetRowStyle, InsetSectionHeader } from "../../components/ui/InsetGroup";
import { fonts, radii, spacing, useChatPalette } from "../../config/theme";
import { makeAdapter } from "../../lib/adapter";
import {
  ADMIN_ONLY_TEXT,
  copyJoinLink,
  inviteResultText,
  isForbidden,
  listInvitations,
  revokeInvitation,
  sendInvitation,
  sentAgo,
  splitEmails,
  type Invitation,
} from "../../lib/workspaceInvites";

type Result = { email: string; text: string; ok: boolean };

/** Settings → Invite people: email invitations next to the join link (web: Settings → Members). */
export default function InviteScreen() {
  const palette = useChatPalette();
  const { getToken } = useAuth();
  // Stable client despite clerk-expo's per-render getToken (see settings.tsx).
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  const orgId = useOrganization().organization?.id ?? "";

  // undefined = loading, "forbidden" = not an admin, null = the read failed.
  const [invitations, setInvitations] = useState<Invitation[] | "forbidden" | null | undefined>(undefined);
  const [refreshing, setRefreshing] = useState(false);
  const [emails, setEmails] = useState("");
  const [sending, setSending] = useState(false);
  const [results, setResults] = useState<Result[]>([]);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  const [linkStatus, setLinkStatus] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!orgId) return;
    try {
      setInvitations(await listInvitations(api, orgId));
    } catch (error) {
      setInvitations(isForbidden(error) ? "forbidden" : null);
    }
  }, [api, orgId]);

  useEffect(() => {
    void load();
  }, [load]);

  const send = async () => {
    const list = splitEmails(emails);
    if (!list.length || sending) return;
    setSending(true);
    const outcomes = await Promise.all(
      list.map((email) =>
        sendInvitation(api, orgId, email).then(
          (invitation) => ({ email, invitation, error: null as unknown }),
          (error: unknown) => ({ email, invitation: null, error }),
        ),
      ),
    );
    setResults(outcomes.map(({ email, invitation, error }) =>
      invitation ? { email, text: "Invite sent", ok: true } : { email, text: inviteResultText(error), ok: false }));
    const sent = outcomes.flatMap(({ invitation }) => (invitation ? [invitation] : []));
    setInvitations((current) =>
      Array.isArray(current) ? [...sent, ...current.filter((row) => !sent.some((s) => s.id === row.id))] : current);
    // Failed addresses stay in the field to fix and resend.
    setEmails(outcomes.filter((o) => !o.invitation).map((o) => o.email).join(", "));
    setSending(false);
  };

  const revoke = (invitation: Invitation) => {
    if (!Array.isArray(invitations)) return;
    const index = invitations.indexOf(invitation);
    setRevokeError(null);
    setInvitations(invitations.filter((row) => row !== invitation));
    revokeInvitation(api, orgId, invitation.id).catch(() => {
      setRevokeError(`Couldn't revoke ${invitation.email}. Try again.`);
      setInvitations((current) => {
        if (!Array.isArray(current) || current.some((row) => row.id === invitation.id)) return current;
        const next = [...current];
        next.splice(Math.min(index, next.length), 0, invitation);
        return next;
      });
    });
  };

  const copyLink = async () => {
    setLinkStatus("Getting the link…");
    try {
      setLinkStatus(await copyJoinLink(api, orgId));
    } catch {
      setLinkStatus("Couldn't get the link. Try again.");
    }
  };

  const body = { color: palette.fg, fontFamily: fonts.sans, fontSize: 17 };
  const caption = { color: palette.fgMute, fontFamily: fonts.sans, fontSize: 13, lineHeight: 18 };

  if (invitations === "forbidden") {
    return (
      <View style={{ flex: 1, backgroundColor: palette.ground, padding: spacing.lg }}>
        <Text style={[body, { marginTop: spacing.xl, textAlign: "center" }]}>{`${ADMIN_ONLY_TEXT} Ask an admin.`}</Text>
      </View>
    );
  }

  const canSend = splitEmails(emails).length > 0 && !sending && !!orgId;
  const pending = Array.isArray(invitations) ? invitations : [];

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: palette.ground }}
      contentInsetAdjustmentBehavior="automatic"
      automaticallyAdjustKeyboardInsets
      keyboardShouldPersistTaps="handled"
      contentContainerStyle={{ paddingBottom: spacing.xl }}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={() => {
        setRefreshing(true);
        void load().finally(() => setRefreshing(false));
      }} />}
    >
      <InsetSectionHeader title="Invite by email" palette={palette} />
      <View style={{ marginHorizontal: spacing.lg, gap: spacing.sm }}>
        <TextInput
          accessibilityLabel="Email addresses"
          placeholder="name@company.com"
          placeholderTextColor={palette.fgMute}
          value={emails}
          onChangeText={setEmails}
          keyboardType="email-address"
          autoCapitalize="none"
          autoCorrect={false}
          autoComplete="email"
          multiline
          style={[body, { minHeight: 48, padding: spacing.md, borderRadius: radii.lg, borderWidth: 1,
            borderColor: palette.edge, backgroundColor: palette.surface }]}
        />
        <Text style={caption}>Separate several addresses with commas or spaces.</Text>
        <Pressable
          onPress={() => void send()}
          disabled={!canSend}
          accessibilityRole="button"
          accessibilityLabel="Send invites"
          accessibilityState={{ disabled: !canSend, busy: sending }}
          style={{ minHeight: 48, alignItems: "center", justifyContent: "center", borderRadius: radii.lg,
            backgroundColor: canSend ? palette.fg : palette.selected }}
        >
          {sending ? <ActivityIndicator color={palette.onFg} /> : (
            <Text style={{ color: canSend ? palette.onFg : palette.fgMute, fontFamily: fonts.sansMedium, fontSize: 15 }}>Send invites</Text>
          )}
        </Pressable>
        {results.map((result) => (
          <Text key={result.email} accessibilityLiveRegion="polite" style={[caption, { color: result.ok ? palette.live : palette.alert }]}>
            {`${result.email}: ${result.text}`}
          </Text>
        ))}
      </View>

      <InsetSectionHeader title="Join link" palette={palette} />
      <Pressable
        accessibilityRole="button"
        accessibilityLabel="Copy join link"
        onPress={() => void copyLink()}
        style={({ pressed }) => [insetRowStyle(palette, 0, 1), { flexDirection: "row", alignItems: "center", gap: spacing.md,
          minHeight: 44, paddingHorizontal: spacing.lg }, pressed && { backgroundColor: palette.selected }]}
      >
        <SymbolView accessibilityElementsHidden name="link" size={17} tintColor={palette.link} />
        <Text style={body}>Copy join link</Text>
      </Pressable>
      <Text style={[caption, { marginHorizontal: 32 }]}>
        {linkStatus ?? "Anyone with the link can join this workspace."}
      </Text>

      <InsetSectionHeader title="Pending invitations" palette={palette} />
      {invitations === undefined ? <ActivityIndicator accessibilityLabel="Loading invitations" /> : null}
      {invitations === null ? <Text style={[caption, { marginHorizontal: 32, color: palette.alert }]}>Couldn’t load invitations. Pull to try again.</Text> : null}
      {Array.isArray(invitations) && pending.length === 0 ? <Text style={[caption, { marginHorizontal: 32 }]}>No pending invitations.</Text> : null}
      {pending.map((invitation, index) => (
        <View key={invitation.id} style={[insetRowStyle(palette, index, pending.length), { flexDirection: "row", alignItems: "center",
          minHeight: 52, paddingLeft: spacing.lg }]}>
          <View style={{ flex: 1, paddingVertical: spacing.sm, borderBottomColor: palette.edge,
            borderBottomWidth: index === pending.length - 1 ? 0 : 1 }}>
            <Text style={body} numberOfLines={1}>{invitation.email}</Text>
            <Text style={caption}>{sentAgo(invitation.created_at)}</Text>
          </View>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Revoke invitation for ${invitation.email}`}
            onPress={() => revoke(invitation)}
            hitSlop={8}
            style={{ paddingHorizontal: spacing.lg, minHeight: 44, justifyContent: "center" }}
          >
            <Text style={{ color: palette.alert, fontFamily: fonts.sansMedium, fontSize: 15 }}>Revoke</Text>
          </Pressable>
        </View>
      ))}
      {revokeError ? <Text accessibilityRole="alert" style={[caption, { marginHorizontal: 32, color: palette.alert }]}>{revokeError}</Text> : null}
    </ScrollView>
  );
}
