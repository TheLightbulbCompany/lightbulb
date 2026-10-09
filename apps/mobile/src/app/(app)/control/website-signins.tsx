import { useAuth, useOrganization } from "@clerk/expo";
import { ApiError, createApiClient } from "@lightbulb/shared";
import { useFocusEffect } from "expo-router";
import { SymbolView } from "expo-symbols";
import { useCallback, useMemo, useRef, useState } from "react";
import { ActionSheetIOS, ActivityIndicator, Alert, Platform, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { LogoMark } from "../../../components/LogoMark";
import { CredentialSheet, type CredentialField, type CredentialValues } from "../../../components/ui/CredentialSheet";
import { fonts, radii, spacing, useChatPalette } from "../../../config/theme";
import { makeAdapter } from "../../../lib/adapter";

// ---------------------------------------------------------------------------
// Contract (org admins only; a member gets 403):
//   GET    /website-logins/sites            -> { sites: WebsiteSite[] }
//   POST   /website-logins                  { origin, username, password } -> 201
//   PATCH  /website-logins/{id}             { username?, password? } -> 200
//   DELETE /website-logins/{id}             -> 204
//   POST   /website-logins/sites/sign-out   { site } -> 204
// No route ever returns a password. The login shape is the backend's
// `website_logins._public`. A 400/404/409 carries a readable `detail` (e.g. 409
// "An agent is using the browser. Try again in a moment."), which ApiError
// surfaces as its message and every failure alert shows.
//
// A backend without /sites (404) still has GET /website-logins -> { logins }
// plus POST and DELETE: the list is grouped here and only Add and Remove show.
// ---------------------------------------------------------------------------

export interface WebsiteLogin {
  id: string;
  origin: string;
  username: string;
  addedBy?: string | null;
  addedByName?: string | null;
  createdAt?: string | null;
  lastUsedAt?: string | null;
}

export interface WebsiteSite {
  site: string;
  signedIn: boolean;
  logins: WebsiteLogin[];
}

type State =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "restricted" }
  /** `basic`: the backend has only list / add / remove (no /sites, PATCH or sign-out). */
  | { kind: "ready"; sites: WebsiteSite[]; basic: boolean };

type Sheet =
  | { mode: "add" }
  | { mode: "username"; login: WebsiteLogin }
  | { mode: "password"; login: WebsiteLogin };

const SHEET_FIELDS: Record<Sheet["mode"], readonly CredentialField[]> = {
  add: ["site", "username", "password"],
  username: ["username"],
  password: ["password"],
};

/** One card per site from a flat login list, for a backend with no /sites route. */
export function groupLoginsBySite(logins: readonly WebsiteLogin[]): WebsiteSite[] {
  const bySite = new Map<string, WebsiteLogin[]>();
  for (const login of logins) {
    const host = login.origin.replace(/^[a-z]+:\/\//i, "").replace(/[:/].*$/, "").toLowerCase();
    // The exact host, not a guessed registrable domain: two-label guessing merges unrelated
    // "*.co.uk" sites into one card. The server's /sites does the real grouping once deployed.
    const site = host || login.origin;
    bySite.set(site, [...(bySite.get(site) ?? []), login]);
  }
  return [...bySite.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([site, grouped]) => ({ site, signedIn: false, logins: grouped }));
}

const message = (error: unknown) => (error instanceof Error ? error.message : "Request failed");

type Choice = { text: string; destructive?: boolean; onPress: () => void };

/** The system action sheet (the pattern RoomComposer's attach menu uses). */
function showMenu(title: string, choices: Choice[]) {
  if (Platform.OS === "ios") {
    const destructive = choices.findIndex((choice) => choice.destructive);
    ActionSheetIOS.showActionSheetWithOptions(
      {
        title,
        options: [...choices.map((choice) => choice.text), "Cancel"],
        cancelButtonIndex: choices.length,
        destructiveButtonIndex: destructive >= 0 ? destructive : undefined,
      },
      (index) => choices[index]?.onPress(),
    );
    return;
  }
  Alert.alert(title, undefined, [
    ...choices.map((choice) => ({ text: choice.text, style: choice.destructive ? ("destructive" as const) : ("default" as const), onPress: choice.onPress })),
    { text: "Cancel", style: "cancel" as const },
  ]);
}

export default function WebsiteSignIns() {
  const p = useChatPalette();
  const { getToken } = useAuth();
  // Pinned through a ref: Clerk's getToken identity churns (see integrations.tsx).
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  const { organization, membership } = useOrganization();
  const orgRestricted = organization != null && membership != null && membership.role !== "org:admin";
  const orgRestrictedRef = useRef(orgRestricted);
  orgRestrictedRef.current = orgRestricted;

  const [state, setState] = useState<State>({ kind: "loading" });
  const [sheet, setSheet] = useState<Sheet | null>(null);
  const [busy, setBusy] = useState(false);
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = (loadSeq.current += 1);
    if (orgRestrictedRef.current) return setState({ kind: "restricted" });
    setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
    try {
      let next: State;
      try {
        const resp = (await api.get("/website-logins/sites")) as { sites?: WebsiteSite[] };
        next = { kind: "ready", sites: resp.sites ?? [], basic: false };
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 404)) throw err;
        const resp = (await api.get("/website-logins")) as { logins?: WebsiteLogin[] };
        next = { kind: "ready", sites: groupLoginsBySite(resp.logins ?? []), basic: true };
      }
      if (loadSeq.current === seq) setState(next);
    } catch (err) {
      if (loadSeq.current !== seq) return;
      if (err instanceof ApiError && err.status === 404) setState({ kind: "ready", sites: [], basic: true });
      else if (err instanceof ApiError && err.status === 403) setState({ kind: "restricted" });
      else setState({ kind: "error", message: message(err) });
    }
  }, [api]);

  useFocusEffect(useCallback(() => void load(), [load]));

  /** Run one change, then re-read the list; a failure is said, never swallowed. */
  const mutate = useCallback(async (failed: string, change: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await change();
    } catch (err) {
      Alert.alert(failed, message(err));
    } finally {
      setBusy(false);
      void load();
    }
  }, [load]);

  const submitSheet = (values: CredentialValues) => {
    if (!sheet) return;
    if (sheet.mode === "add") {
      void mutate("Couldn’t add the sign-in", () =>
        api.post("/website-logins", { origin: values.site, username: values.username, password: values.password }));
    } else if (sheet.mode === "username") {
      void mutate("Couldn’t change the username", () => api.patch(`/website-logins/${sheet.login.id}`, { username: values.username }));
    } else {
      void mutate("Couldn’t replace the password", () => api.patch(`/website-logins/${sheet.login.id}`, { password: values.password }));
    }
  };

  const basic = state.kind === "ready" && state.basic;

  const confirm = (title: string, body: string, action: string, onPress: () => void) =>
    Alert.alert(title, body, [
      { text: "Cancel", style: "cancel" },
      { text: action, style: "destructive", onPress },
    ]);

  const openMenu = (site: WebsiteSite, login: WebsiteLogin | null) => {
    const choices: Choice[] = [];
    if (login && !basic) {
      choices.push(
        { text: "Edit username", onPress: () => setSheet({ mode: "username", login }) },
        { text: "Replace password", onPress: () => setSheet({ mode: "password", login }) },
      );
    }
    if (site.signedIn && !basic) {
      choices.push({
        text: "Sign out of the browser",
        onPress: () =>
          confirm("Sign out of the browser?", `Agents will be signed out of ${site.site} in the workspace browser.`, "Sign out", () =>
            void mutate("Couldn’t sign out", () => api.post("/website-logins/sites/sign-out", { site: site.site }))),
      });
    }
    if (login) {
      choices.push({
        text: "Remove",
        destructive: true,
        onPress: () =>
          confirm("Remove this sign-in?", `Agents will no longer be able to sign in to ${site.site} as ${login.username}.`, "Remove", () =>
            void mutate("Couldn’t remove the sign-in", () => api.del(`/website-logins/${login.id}`))),
      });
    }
    showMenu(login ? `${site.site} · ${login.username}` : site.site, choices);
  };

  const sites = state.kind === "ready" ? state.sites : [];
  const text = { color: p.fg };
  const mute = { color: p.fgMute };

  return (
    <View style={[styles.page, { backgroundColor: p.ground }]}>
      <ScrollView contentInsetAdjustmentBehavior="automatic" contentContainerStyle={styles.content}>
        {state.kind === "restricted" ? (
          <View style={[styles.card, { backgroundColor: p.surface, borderColor: p.edge }]}>
            <Text style={[styles.body, text]}>Website sign-ins are managed by your workspace admin.</Text>
          </View>
        ) : (
          <>
            <Pressable
              style={({ pressed }) => [styles.add, { backgroundColor: p.fg }, (pressed || busy) && styles.pressed]}
              onPress={() => setSheet({ mode: "add" })}
              disabled={busy}
              accessibilityRole="button"
              accessibilityLabel="Add a website sign-in"
            >
              <SymbolView accessibilityElementsHidden name="plus" size={15} tintColor={p.onFg} />
              <Text style={[styles.addText, { color: p.onFg }]}>Add sign-in</Text>
            </Pressable>
            {state.kind === "loading" ? (
              <ActivityIndicator testID="signins-loading" style={styles.spinner} color={p.fgMute} />
            ) : state.kind === "error" ? (
              <View style={[styles.card, { backgroundColor: p.raised, borderColor: p.alert }]}>
                <Text style={[styles.body, { color: p.alert }]}>Couldn’t load website sign-ins: {state.message}</Text>
                <Pressable onPress={() => void load()} accessibilityRole="button" accessibilityLabel="Retry">
                  <Text style={[styles.retry, { color: p.alert }]}>Retry</Text>
                </Pressable>
              </View>
            ) : sites.length === 0 ? (
              <View style={styles.empty}>
                <LogoMark size={48} />
                <Text style={[styles.dim, mute]}>No website sign-ins yet. When an agent needs one, it asks here.</Text>
              </View>
            ) : (
              sites.map((site) => (
                <View key={site.site} style={[styles.card, { backgroundColor: p.surface, borderColor: p.edge }]}>
                  <Text style={[styles.site, text]}>{site.site}</Text>
                  {site.signedIn && !basic ? (
                    <View style={styles.marker}>
                      <View style={[styles.dot, { backgroundColor: p.live }]} />
                      <Text style={[styles.caption, mute]}>Browser session saved</Text>
                    </View>
                  ) : null}
                  {(site.logins.length > 0 ? site.logins : [null]).map((login) => (
                    <View key={login?.id ?? "none"} style={styles.loginRow}>
                      <Text style={[styles.username, login ? { color: p.fgSoft } : mute]}>
                        {login ? login.username : "No saved password"}
                      </Text>
                      <Pressable
                        onPress={() => openMenu(site, login)}
                        disabled={busy}
                        hitSlop={10}
                        accessibilityRole="button"
                        accessibilityLabel={`Actions for ${login ? `${login.username} on ${site.site}` : site.site}`}
                        style={({ pressed }) => [styles.menu, pressed && styles.pressed]}
                      >
                        <SymbolView accessibilityElementsHidden name="ellipsis" size={18} tintColor={p.fgSoft} />
                      </Pressable>
                    </View>
                  ))}
                </View>
              ))
            )}
          </>
        )}
      </ScrollView>
      <CredentialSheet
        visible={sheet != null}
        title={
          sheet?.mode === "username" ? `Username for ${sheet.login.origin}`
            : sheet?.mode === "password" ? `New password for ${sheet.login.username}`
              : "Add a website sign-in"
        }
        hint={sheet?.mode === "add" ? "Agents use it to sign in to this site in the workspace browser." : null}
        fields={SHEET_FIELDS[sheet?.mode ?? "add"]}
        initialUsername={sheet?.mode === "username" ? sheet.login.username : ""}
        submitLabel={sheet?.mode === "add" ? "Add" : "Save"}
        onSubmit={submitSheet}
        onClose={() => setSheet(null)}
      />
    </View>
  );
}

// Every height is a minimum and every row wraps, so large text grows the card.
const styles = StyleSheet.create({
  page: { flex: 1 },
  content: { padding: spacing.lg, gap: spacing.md, paddingBottom: spacing.xl, flexGrow: 1 },
  card: { borderWidth: 1, borderRadius: radii.lg, padding: spacing.lg, gap: spacing.sm },
  body: { fontFamily: fonts.sans, fontSize: 14, lineHeight: 20 },
  retry: { fontFamily: fonts.sansSemiBold, fontSize: 14 },
  add: {
    alignSelf: "flex-start",
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.xs,
    minHeight: 36,
    paddingHorizontal: spacing.md,
    borderRadius: 999,
  },
  addText: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 19 },
  spinner: { paddingVertical: spacing.xl },
  empty: { flexGrow: 1, alignItems: "center", justifyContent: "center", gap: spacing.sm, padding: spacing.xl },
  dim: { fontFamily: fonts.sans, fontSize: 15, lineHeight: 22, textAlign: "center" },
  site: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20 },
  marker: { flexDirection: "row", alignItems: "center", gap: spacing.xs },
  dot: { width: 8, height: 8, borderRadius: 4 },
  caption: { flexShrink: 1, fontFamily: fonts.sans, fontSize: 12, lineHeight: 16 },
  loginRow: { flexDirection: "row", alignItems: "center", gap: spacing.sm, minHeight: 32 },
  username: { flex: 1, fontFamily: fonts.sans, fontSize: 14, lineHeight: 19 },
  menu: { minWidth: 32, minHeight: 32, alignItems: "center", justifyContent: "center" },
  pressed: { opacity: 0.6 },
});
