import { useAuth, useOrganization } from "@clerk/expo";
import {
  buildSetupSteps,
  createApiClient,
  isBandComplete,
  isConnectorStep,
  type SetupBilling,
  type SetupConnection,
  type SetupParticipant,
  type SetupStep,
  type SetupStepId,
} from "@lightbulb/shared";
import { router, useFocusEffect } from "expo-router";
import * as WebBrowser from "expo-web-browser";
import { SymbolView, type SFSymbol } from "../ui/SymbolView";
import { useCallback, useMemo, useRef, useState, type ReactElement } from "react";
import { AppState, Linking, Pressable, Share, StyleSheet, Text, View } from "react-native";

import { fonts, radii, spacing, type, useChatPalette, type ChatPalette } from "../../config/theme";
import { APP_SCHEME } from "../../config/domain";
import { makeAdapter } from "../../lib/adapter";
import { isConnectionReturn } from "../../lib/deepLink";
import { LocalStore } from "../../lib/localStore";
import { getJoinLink, LEGACY_JOIN_LINK_TEXT } from "../../lib/workspaceInvites";

// =============================================================================
// The mobile twin of the web onboarding setup band
// (apps/frontend/src/components/messaging/OnboardingBand.tsx): Slack's "Get
// started" cards, in the starter channel only, above the oldest message.
//
// Every step's done-state is derived by the shared `buildSetupSteps`; the one
// stored bit is the per-viewer, per-workspace dismissal below.
// =============================================================================

/** The web key, verbatim, scoped per workspace (org id) — see the web band. */
export const dismissKey = (orgId: string) => `lightbulb.messaging.onboarding-band.dismissed.${orgId}`;
const dismissStore = new LocalStore<Record<string, string>>("onboarding-band");

const NON_ADMIN_HINT = "Ask an admin";

type InviteState = "idle" | "working" | "shared" | "failed" | "legacy";

function inviteStatus(state: InviteState, isAdmin: boolean): string | undefined {
  if (state === "working") return "Getting the link…";
  // Settings → Members is admin-only, so a member is sent to an admin instead.
  if (state === "legacy") return isAdmin ? "Reset the link in Settings" : "Ask an admin to reset the link";
  if (state === "shared") return "Link shared";
  if (state === "failed") return "Couldn’t get the link — try again";
  return undefined;
}

const STEP_SYMBOL: Record<SetupStepId, SFSymbol> = {
  "starter-model": "cpu",
  stripe: "link",
  github: "link",
  notion: "link",
  invite: "person.2",
  // Never rendered: mobile passes no `humanSpoke`/`approvalAnswered`, so buildSetupSteps omits them.
  "tell-ceo": "text.bubble",
  approval: "checkmark.square",
  "name-agents": "pencil",
  // Never rendered: mobile passes no `contextImported`, so buildSetupSteps omits the step.
  context: "doc.text",
};

export function OnboardingBand({ participants }: { participants: readonly SetupParticipant[] }): ReactElement | null {
  const palette = useChatPalette();
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  const { organization, membership } = useOrganization();
  const orgId = organization?.id ?? "";

  // undefined = still loading. connections null = the read failed.
  const [connections, setConnections] = useState<SetupConnection[] | null | undefined>(undefined);
  const [agentNames, setAgentNames] = useState<Map<string, string> | undefined>(undefined);
  const [billing, setBilling] = useState<SetupBilling | null | undefined>(undefined);
  const [dismissedNow, setDismissedNow] = useState(false);
  // Read each render (the store serves from memory): orgId can arrive after mount.
  const dismissed = dismissedNow || (!!orgId && dismissStore.read()[dismissKey(orgId)] === "1");
  const [invite, setInvite] = useState<InviteState>("idle");
  const [connecting, setConnecting] = useState<SetupStepId | null>(null);
  const [connectFailed, setConnectFailed] = useState<SetupStepId | null>(null);
  const connectionOpened = useRef(false);

  const loadConnections = useCallback(
    (live: () => boolean = () => true) =>
      api
        .get("/connections")
        .then((body) => live() && setConnections((body as { connections?: SetupConnection[] }).connections ?? []))
        .catch(() => live() && setConnections(null)),
    [api],
  );

  // On focus, so coming back from the LLM or agents screen re-derives the band.
  // A refetch never resets to "loading": the band must not flicker away. A blur
  // (or unmount) drops this round's answers, so a slow response from an earlier
  // focus can never overwrite a newer one.
  useFocusEffect(
    useCallback(() => {
      let cancelled = false;
      const live = () => !cancelled;
      // Like web checkout, OAuth runs in the system browser. Returning by a
      // universal link or switching back only refetches server-owned status.
      const appState = AppState.addEventListener("change", (state) => {
        if (state === "active" && connectionOpened.current) {
          void loadConnections(live);
        }
      });
      const url = Linking.addEventListener("url", ({ url }) => {
        if (connectionOpened.current && isConnectionReturn(url)) {
          connectionOpened.current = false;
          void loadConnections(live);
        }
      });
      void loadConnections(live);
      void api
        .get("/teams/agents")
        .then((body) => (body as { agents?: { id: string; name?: string | null }[] }).agents ?? [])
        .catch(() => [])
        .then((agents) => live() && setAgentNames(new Map(agents.filter((a) => a.name).map((a) => [a.id, a.name as string]))));
      // Billing only has to settle: a 403/404/failure just omits its card.
      void api
        .get("/billing/account")
        .then((body) => live() && setBilling(body as SetupBilling))
        .catch(() => live() && setBilling(null));
      return () => {
        cancelled = true;
        appState?.remove();
        url.remove();
      };
    }, [api, loadConnections]),
  );

  const workspaceMembers = organization?.membersCount ?? 0;
  const steps = useMemo(
    () =>
      buildSetupSteps({
        participants,
        connections: connections ?? [],
        agentNames: agentNames ?? new Map(),
        workspaceMembers,
        billing,
      }),
    [participants, connections, agentNames, workspaceMembers, billing],
  );
  const isAdmin = membership?.role === "org:admin";

  const shareJoinLink = useCallback(async () => {
    if (!orgId) return;
    setInvite("working");
    try {
      const url = await getJoinLink(api, orgId);
      // A legacy link (exists, no join_url) is never rotated from here.
      if (!url) return setInvite("legacy");
      const result = await Share.share({ message: url });
      setInvite(result.action === Share.dismissedAction ? "idle" : "shared");
    } catch {
      setInvite("failed");
    }
  }, [api, orgId]);

  const connect = useCallback(
    async (provider: SetupStepId) => {
      setConnecting(provider);
      setConnectFailed(null);
      try {
        const { authorize_url } = (await api.get(`/connections/${encodeURIComponent(provider)}/authorize?return_to=ios`)) as {
          authorize_url: string;
        };
        connectionOpened.current = true;
        const result = await WebBrowser.openAuthSessionAsync(
          authorize_url,
          `${APP_SCHEME}://checkout_redirect/connected`,
        );
        connectionOpened.current = false;
        if (result.type === "success") {
          if (!isConnectionReturn(result.url)) throw new Error("Unexpected OAuth return");
          if (result.url.includes("/connect_failed")) setConnectFailed(provider);
          await loadConnections();
        }
      } catch {
        connectionOpened.current = false;
        setConnectFailed(provider);
      } finally {
        setConnecting(null);
      }
    },
    [api, loadConnections],
  );

  const onStep = useCallback(
    (step: SetupStep) => {
      if (step.id === "invite") return void shareJoinLink();
      if (step.id === "name-agents") return router.push("/(app)/control/agents");
      // App Store 3.1.3(f): the provider picker, never a checkout.
      if (step.id === "starter-model") return router.push("/(app)/control/llm");
      void connect(step.id);
    },
    [connect, shareJoinLink],
  );

  // Every read must have answered before judging what is left — see the web band.
  if (connections === undefined || connections === null || !agentNames || billing === undefined || !orgId) return null;
  if (dismissed || isBandComplete(steps)) return null;

  const starter = steps.find((step) => step.id === "starter-model");
  const inviteStep = steps.find((step) => step.id === "invite");
  const connectors = steps.filter(isConnectorStep);
  const later = steps.filter((step) => step.later);

  const card = (step: SetupStep, status?: string, busy?: boolean) => (
    <StepCard key={step.id} step={step} isAdmin={isAdmin} status={status} busy={busy} onSelect={onStep} palette={palette} />
  );

  return (
    <View style={[styles.band, { borderColor: palette.edge }]} accessibilityLabel="Set up your workspace">
      <View style={styles.head}>
        <View style={styles.headText}>
          <Text style={[styles.title, { color: palette.fg }]}>Get started</Text>
          <Text style={[styles.subtitle, { color: palette.fgMute }]}>Bring in the person you work with most, then connect what your team uses.</Text>
        </View>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Dismiss setup"
          hitSlop={12}
          onPress={() => {
            dismissStore.write({ ...dismissStore.read(), [dismissKey(orgId)]: "1" });
            dismissStore.flush();
            setDismissedNow(true);
          }}
        >
          <SymbolView accessibilityElementsHidden name="xmark" size={14} tintColor={palette.fgMute} />
        </Pressable>
      </View>

      {starter && card(starter)}
      {/* Invite first (08 §3): the second person is the activation event. */}
      {inviteStep && card(inviteStep, inviteStatus(invite, isAdmin), invite === "working")}
      {connectors.map((step) =>
        card(
          step,
          connecting === step.id ? "Connecting…" : connectFailed === step.id ? "Couldn’t connect — try again" : undefined,
          connecting === step.id,
        ),
      )}
      {later.map((step) => card(step))}

      {invite === "legacy" && (
        <Text style={[styles.detail, { color: palette.fgMute }]}>{LEGACY_JOIN_LINK_TEXT}</Text>
      )}
    </View>
  );
}

function StepCard({ step, isAdmin, onSelect, status, busy, palette }: {
  step: SetupStep;
  isAdmin: boolean;
  onSelect: (step: SetupStep) => void;
  status?: string;
  busy?: boolean;
  palette: ChatPalette;
}): ReactElement {
  const blocked = step.adminOnly && !isAdmin;
  const disabled = blocked || !!busy || (step.done && !step.repeatable);
  const detail = step.done ? step.doneDetail : blocked ? NON_ADMIN_HINT : status ?? step.detail;
  return (
    <Pressable
      style={({ pressed }) => [
        styles.card,
        { backgroundColor: palette.raised },
        pressed && !disabled && { backgroundColor: palette.hover },
        disabled && styles.disabled,
      ]}
      accessibilityRole="button"
      accessibilityLabel={`${step.label}. ${detail}`}
      accessibilityState={{ disabled }}
      disabled={disabled}
      onPress={() => onSelect(step)}
    >
      <SymbolView
        accessibilityElementsHidden
        name={step.done ? "checkmark" : STEP_SYMBOL[step.id]}
        size={16}
        tintColor={step.done ? palette.live : palette.fg}
      />
      <View style={styles.cardText}>
        <Text style={[styles.label, { color: palette.fg }]}>{step.label}</Text>
        <Text style={[styles.detail, { color: palette.fgMute }]}>{detail}</Text>
      </View>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  band: {
    borderWidth: 1,
    borderRadius: radii.md,
    padding: spacing.md,
    marginBottom: spacing.lg,
    gap: spacing.sm,
  },
  head: { flexDirection: "row", alignItems: "flex-start", gap: spacing.sm },
  headText: { flex: 1, gap: spacing.xs },
  title: { ...type.body, fontFamily: fonts.sansSemiBold },
  subtitle: type.caption,
  card: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.md,
    minHeight: 44,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: radii.md,
  },
  disabled: { opacity: 0.55 },
  cardText: { flex: 1 },
  label: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20 },
  detail: type.caption,
});
