/**
 * Every product API needs an active workspace (a Clerk organization):
 * `resolve_owner_id` answers 409 `workspace_required` to a session with no org
 * claim. The app used to never pick one — sign-in's `setActive` names only the
 * session — so such a user reached Home and dead-ended on Retry (F4, mobile UX
 * audit 2026-09-23; also App Store 2.1, a reviewer who cannot get in).
 *
 * `WorkspaceGate` sits in (app)/_layout.tsx under the consent gate and above
 * everything that calls the API. With no active org:
 *
 *   0 memberships → "Create a workspace", which opens web onboarding
 *   1 membership  → activated silently
 *   2+            → a list to pick from
 *
 * Clerk keeps the chosen org on the session (`lastActiveOrganizationId`), so a
 * relaunch restores it with no storage of our own.
 */
import { useAuth, useOrganizationList } from "@clerk/expo";
import { Button, Section } from "../ui/swiftui";
import { foregroundStyle } from "@expo/ui/swift-ui/modifiers";
import { resetSwrCache } from "@lightbulb/shared";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  ActivityIndicator,
  Alert,
  AppState,
  Linking,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { WEB_ORIGIN } from "../../config/domain";
import { colors, fonts, radii, spacing } from "../../config/theme";
import { LogoMark } from "../LogoMark";
import { GroupedList, NavigationRow } from "../ui/GroupedList";

// ponytail: first 50 memberships only; page when someone belongs to more.
const MEMBERSHIPS = { pageSize: 50 };
const DANGER = [foregroundStyle(colors.danger)];

function useWorkspaces() {
  const { orgId } = useAuth();
  const { isLoaded, setActive, userMemberships } = useOrganizationList({
    userMemberships: MEMBERSHIPS,
  });
  const revalidate = useRef(userMemberships.revalidate);
  revalidate.current = userMemberships.revalidate;

  // Back from the browser: a workspace created on the web shows up here.
  useEffect(() => {
    const sub = AppState.addEventListener("change", (state) => {
      if (state === "active") void revalidate.current?.();
    });
    return () => sub.remove();
  }, []);

  // Always a real `setActive`, even for the current org: after a 409 it is how
  // the token picks the org claim back up. Cached reads belong to the old
  // workspace (runtime RPC keys and the status key carry none), so drop them.
  const activate = useCallback(
    async (organization: string): Promise<boolean> => {
      try {
        await setActive?.({ organization });
        resetSwrCache();
        return true;
      } catch (e) {
        Alert.alert("Couldn't open workspace", e instanceof Error ? e.message : "Try again.");
        return false;
      }
    },
    [setActive],
  );

  return {
    orgId: orgId ?? null,
    loaded: isLoaded && !userMemberships.isLoading,
    memberships: userMemberships.data ?? [],
    revalidate: () => void revalidate.current?.(),
    activate,
  };
}

export function WorkspaceGate({ children }: { children: ReactNode }) {
  const { orgId } = useAuth();
  if (orgId) return <>{children}</>;
  return <WorkspaceChooser />;
}

/**
 * The blocking chooser. `autoActivate={false}` is for a 409 while the client
 * already has an org (ProvisionGate): re-activating a lone membership there
 * could loop, so it is shown as a one-row list instead.
 */
export function WorkspaceChooser({ autoActivate = true }: { autoActivate?: boolean }) {
  const { loaded, memberships, revalidate, activate } = useWorkspaces();
  const onSignOut = useConfirmSignOut();
  // A failed silent activation falls back to the one-row list, so it can be retried.
  const [autoFailed, setAutoFailed] = useState(false);
  const lone =
    autoActivate && !autoFailed && memberships.length === 1 ? memberships[0].organization.id : null;
  const tried = useRef<string | null>(null);

  useEffect(() => {
    if (!lone || tried.current === lone) return;
    tried.current = lone;
    void activate(lone).then((ok) => {
      if (!ok) setAutoFailed(true);
    });
  }, [lone, activate]);

  if (!loaded || lone) return <Waiting />;
  if (memberships.length === 0) return <CreateWorkspace onCheckAgain={revalidate} />;

  return (
    <Page>
      <GroupedList>
        <Section title="Choose a workspace">
          {memberships.map(({ organization }) => (
            <NavigationRow
              key={organization.id}
              title={organization.name}
              icon="building.2"
              onPress={() => void activate(organization.id)}
            />
          ))}
        </Section>
        <Section>
          <Button label="Sign out" onPress={onSignOut} modifiers={DANGER} />
        </Section>
      </GroupedList>
    </Page>
  );
}

function useConfirmSignOut() {
  const { signOut } = useAuth();
  return () =>
    Alert.alert("Sign out", "Sign out of this account?", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive", onPress: () => void signOut() },
    ]);
}

function Page({ children, centered = false }: { children: ReactNode; centered?: boolean }) {
  const insets = useSafeAreaInsets();
  return (
    <View style={[styles.page, centered && styles.centered, { paddingTop: insets.top }]}>
      {children}
    </View>
  );
}

function Waiting() {
  return (
    <Page centered>
      <ActivityIndicator color={colors.forest} accessibilityLabel="Opening your workspace" />
    </Page>
  );
}

// App Store 3.1.1: setup only — no price, plan or billing words here.
function CreateWorkspace({ onCheckAgain }: { onCheckAgain: () => void }) {
  const onSignOut = useConfirmSignOut();
  return (
    <Page centered>
      <View style={styles.card}>
        <LogoMark size={48} />
        <Text style={styles.headline} accessibilityRole="header">
          Create a workspace
        </Text>
        <Text style={styles.body}>
          Your agents, projects and chats live in a workspace. Create one on the web, then come
          back here.
        </Text>
        <Text style={styles.body}>
          Joining a team that already uses Lightbulb? Open the join link they sent you in Safari,
          then tap Check again. Invited by email? Open that email on this phone and tap its link.
        </Text>
        <Pressable
          style={({ pressed }) => [styles.primary, pressed && styles.pressed]}
          onPress={() => void Linking.openURL(`${WEB_ORIGIN}/onboarding`)}
          accessibilityRole="link"
        >
          <Text style={styles.primaryText}>Continue on thelightbulb.company</Text>
        </Pressable>
        <Pressable
          style={({ pressed }) => [styles.secondary, pressed && styles.pressed]}
          onPress={onCheckAgain}
          accessibilityRole="button"
        >
          <Text style={styles.secondaryText}>Check again</Text>
        </Pressable>
        <Pressable
          style={({ pressed }) => [styles.secondary, pressed && styles.pressed]}
          onPress={onSignOut}
          accessibilityRole="button"
        >
          <Text style={styles.signOutText}>Sign out</Text>
        </Pressable>
      </View>
    </Page>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, backgroundColor: colors.cream },
  centered: { alignItems: "center", justifyContent: "center", padding: spacing.xl },
  card: { width: "100%", maxWidth: 400, alignItems: "center", gap: spacing.md },
  headline: {
    fontFamily: fonts.lora,
    fontSize: 20,
    lineHeight: 26,
    color: colors.ink,
    textAlign: "center",
  },
  body: {
    fontFamily: fonts.sans,
    fontSize: 14,
    lineHeight: 20,
    color: colors.mute,
    textAlign: "center",
  },
  primary: {
    alignSelf: "stretch",
    alignItems: "center",
    paddingVertical: spacing.md,
    borderRadius: radii.md,
    backgroundColor: colors.forest,
  },
  primaryText: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.white },
  secondary: { paddingHorizontal: spacing.lg, paddingVertical: spacing.sm },
  secondaryText: { fontFamily: fonts.sansMedium, fontSize: 14, lineHeight: 18, color: colors.forest },
  signOutText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 17, color: colors.mute },
  pressed: { opacity: 0.6 },
});
