import { useAuth, useOrganization } from "@clerk/expo";
import { useFocusEffect } from "expo-router";
import {
  useCallback,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Animated,
  Linking,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  useColorScheme,
  View,
} from "react-native";
import {
  ApiError,
  createApiClient,
  useStatusLoaded,
  useRuntimeRpc,
} from "@lightbulb/shared";

import { usePulse } from "../../../components/chat/usePulse";
import { WEB_ORIGIN } from "../../../config/domain";
import { chatPalette, fonts, radii, spacing, type ChatPalette } from "../../../config/theme";
import { makeAdapter } from "../../../lib/adapter";
import { listCatalogModels } from "../../../lib/agentCatalog";
import { isManagedNoChoiceAccount, MANAGED_NO_CHOICE_LABEL } from "../../../lib/managedProvider";
import {
  resolveProviderStatus,
  statusToDotColor,
  type AuthStatusProvider,
  type ByoProvider,
  type DotColor,
  type ProviderChoice,
} from "../../../lib/providerAuthStatus";
import { useLiveEvents } from "../../../providers/live-events";

// Provider/model/credit management is a multi-step, purchase-adjacent surface
// (OAuth sign-in, legacy BYO-key display, Stripe top-up). Per D1 the mobile app renders
// current state read-only and deep-links out to the web for any change.
const MANAGE_LLM_URL = `${WEB_ORIGIN}/settings?panel=billing`;

// ---------------------------------------------------------------------------
// Contract shapes (w3-control-contract §7 + w2-billing-contract §2)
// ---------------------------------------------------------------------------

interface BillingAccount {
  provider_choice: ProviderChoice | null;
  byo_provider: ByoProvider | null;
  /** The managed model's daily limit tripped today (UTC); clears at the rollover. */
  capped_today?: boolean;
  /** Needed only to resolve a null provider_choice — see managedProvider.ts. */
  subscription_status?: string | null;
  plan?: string | null;
}

interface CreditsBalance {
  balance_microcents: number | null;
  /** Pre-formatted "%.2f" without "$", null when balance unavailable. */
  balance_dollars: string | null;
}

/** `models.authStatus` RPC → provider auth health list (drives the dot). */
interface AuthStatusResult {
  ts?: number;
  providers?: AuthStatusProvider[];
}

type BillingState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  /** 404 from /billing/account — pre-subscribe, not an error. */
  | { kind: "none" }
  /** 403 — billing managed by the org admin (contract §3 org gating). */
  | { kind: "restricted" }
  | { kind: "ready"; account: BillingAccount; credits: CreditsBalance | null };

// ---------------------------------------------------------------------------
// Provider label mapping (contract §7 / LLMPanel labels)
// ---------------------------------------------------------------------------

function providerLabel(account: BillingAccount): {
  title: string;
  detail: string | null;
} {
  // No stored provider_choice: a cardless free trial, and a paid Base/
  // Workspace/Ultra row that never picked one, both run on the managed
  // DeepSeek gate — "No provider selected" would wrongly tell either of
  // them to go pick one. Shared with settings.tsx (managedProvider.ts).
  if (isManagedNoChoiceAccount(account)) {
    return { ...MANAGED_NO_CHOICE_LABEL };
  }
  switch (account.provider_choice) {
    case "chatgpt_oauth":
      return {
        title: "Sign in with ChatGPT",
        detail: "Inference via your ChatGPT account",
      };
    case "openrouter_managed":
      return { title: "Lightbulb-managed models", detail: "Inference uses your workspace credits" };
    case "byo_key":
      if (account.byo_provider === "openai") {
        return { title: "Bring your own key", detail: "OpenAI API key" };
      }
      if (account.byo_provider === "anthropic") {
        return { title: "Bring your own key", detail: "Anthropic API key" };
      }
      return {
        title: "Bring your own key",
        detail: "Pick a provider to finish setting up",
      };
    default:
      return {
        title: "No provider selected",
        detail: "You haven't picked a provider yet.",
      };
  }
}

function dotFill(color: DotColor, p: ChatPalette): string {
  if (color === "green") return p.live;
  if (color === "amber") return p.warn;
  if (color === "red") return p.alert;
  return p.fgMute;
}

function dotLabel(color: DotColor): string {
  if (color === "green") return "Connected";
  if (color === "amber") return "Expiring soon";
  if (color === "red") return "Needs auth";
  return "Unknown";
}

// ---------------------------------------------------------------------------
// Model catalog derivation from config.get (read-only, grouped by provider)
// ---------------------------------------------------------------------------

interface CatalogEntry {
  ref: string;
  name: string;
}

interface ProviderGroup {
  providerKey: string;
  label: string;
  models: CatalogEntry[];
}

const PROVIDER_LABELS: Record<string, string> = {
  "amazon-bedrock": "Amazon Bedrock",
  bedrock: "Amazon Bedrock",
  "openai-codex": "OpenAI (ChatGPT)",
  openai: "OpenAI",
  anthropic: "Anthropic",
};

function providerGroupLabel(key: string): string {
  return PROVIDER_LABELS[key] ?? key;
}

/** config.get returns `{path, exists, raw, config, hash?, valid}` — the real
 *  config lives under `.config` (fall back to the snapshot itself). */
function extractConfig(snapshot: unknown): unknown {
  if (snapshot && typeof snapshot === "object" && "config" in snapshot) {
    const inner = (snapshot as { config?: unknown }).config;
    if (inner && typeof inner === "object") return inner;
  }
  return snapshot;
}

/** Walk `config.models.providers` into per-provider model groups. Untyped
 *  runtime data from the container — every access is defensive. */
function listGroupedModels(config: unknown): ProviderGroup[] {
  const out: ProviderGroup[] = [];
  const models = (config as { models?: unknown } | null | undefined)?.models;
  const providers = (models as { providers?: unknown } | null | undefined)
    ?.providers;
  if (!providers || typeof providers !== "object") return out;
  for (const [providerKey, provider] of Object.entries(
    providers as Record<string, unknown>,
  )) {
    const list = (provider as { models?: unknown } | null | undefined)?.models;
    if (!Array.isArray(list)) continue;
    const entries: CatalogEntry[] = [];
    for (const entry of list) {
      const id = (entry as { id?: unknown } | null | undefined)?.id;
      const name = (entry as { name?: unknown } | null | undefined)?.name;
      if (typeof id !== "string" || !id) continue;
      entries.push({
        ref: `${providerKey}/${id}`,
        name: typeof name === "string" && name ? name : id,
      });
    }
    if (entries.length > 0) {
      out.push({
        providerKey,
        label: providerGroupLabel(providerKey),
        models: entries,
      });
    }
  }
  return out;
}

/** Owner-level default model ref (`agents.defaults.model`, string or
 *  `{primary}` object form). */
function readDefaultModelRef(config: unknown): string | undefined {
  const model = (
    config as { agents?: { defaults?: { model?: unknown } } } | null | undefined
  )?.agents?.defaults?.model;
  if (typeof model === "string") return model || undefined;
  const primary = (model as { primary?: unknown } | null | undefined)?.primary;
  return typeof primary === "string" && primary ? primary : undefined;
}

// ---------------------------------------------------------------------------
// REST hook — billing account + credits, refetched on focus. getTokenRef pins
// Clerk's churning getToken so `load` stays stable (settings.tsx lesson: a
// [getToken] dep is an infinite-refetch loop).
// ---------------------------------------------------------------------------

function useLlmBilling(orgRestricted: boolean) {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(
    () => createApiClient(makeAdapter(() => getTokenRef.current())),
    [],
  );
  const orgRestrictedRef = useRef(orgRestricted);
  orgRestrictedRef.current = orgRestricted;

  const [state, setState] = useState<BillingState>({ kind: "loading" });
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = loadSeq.current + 1;
    loadSeq.current = seq;
    // Client-side org gate: a non-admin org member only gets 403s back.
    if (orgRestrictedRef.current) {
      setState({ kind: "restricted" });
      return;
    }
    setState((prev) => (prev.kind === "ready" ? prev : { kind: "loading" }));
    try {
      const account = (await api.get("/billing/account")) as BillingAccount;
      // Credits are best-effort: failure never fails the card, and we only
      // need them for managed inference.
      const credits =
        account.provider_choice === "openrouter_managed"
          ? ((await api
              .get("/billing/credits/balance")
              .then((body) => body as CreditsBalance)
              .catch(() => null)) as CreditsBalance | null)
          : null;
      if (loadSeq.current !== seq) return;
      setState({ kind: "ready", account, credits });
    } catch (err) {
      if (loadSeq.current !== seq) return;
      if (err instanceof ApiError && err.status === 404) {
        setState({ kind: "none" });
      } else if (err instanceof ApiError && err.status === 403) {
        setState({ kind: "restricted" });
      } else {
        setState({
          kind: "error",
          message: err instanceof Error ? err.message : "Request failed",
        });
      }
    }
  }, [api]);

  useFocusEffect(
    useCallback(() => {
      void load();
    }, [load]),
  );

  return { state, retry: load };
}

// ---------------------------------------------------------------------------
// Building blocks
// ---------------------------------------------------------------------------

function Card({ children }: { children: ReactNode }) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return <View style={styles.card}>{children}</View>;
}

function ManageLink({ label, url = MANAGE_LLM_URL }: { label: string; url?: string }) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  return (
    <Pressable
      style={({ pressed }) => [styles.linkRow, pressed && styles.linkRowPressed]}
      onPress={() => void Linking.openURL(url)}
      accessibilityRole="link"
    >
      <Text style={styles.linkText}>{label}</Text>
    </Pressable>
  );
}

function SkeletonRow({ delayMs, width }: { delayMs: number; width: `${number}%` }) {
  const styles = SHEETS[useColorScheme() === "dark" ? "dark" : "light"];
  const pulse = usePulse(true, 0.55, 700, delayMs);
  return <Animated.View style={[styles.skeletonLine, { width, opacity: pulse }]} />;
}

function SkeletonCards() {
  return (
    <>
      <Card>
        <SkeletonRow delayMs={150} width="30%" />
        <SkeletonRow delayMs={300} width="72%" />
        <SkeletonRow delayMs={450} width="55%" />
      </Card>
      <Card>
        <SkeletonRow delayMs={450} width="40%" />
        <SkeletonRow delayMs={600} width="64%" />
      </Card>
    </>
  );
}

// ---------------------------------------------------------------------------
// Screen
// ---------------------------------------------------------------------------

export default function LlmPanel() {
  const scheme = useColorScheme() === "dark" ? "dark" : "light";
  const styles = SHEETS[scheme];
  const palette = chatPalette[scheme];
  const live = useLiveEvents();
  const { organization, membership } = useOrganization();
  const orgRestricted =
    organization != null && membership != null && membership.role !== "org:admin";
  const { state, retry } = useLlmBilling(orgRestricted);

  // Both reads are held until the runtime is DEFINITE — `undefined` is "not
  // known for this workspace", and guessing costs a microVM start on AgentCore.
  // The dot needs no runtime branch: the Hermes bridge reports the same provider
  // ids `providerAuthStatus` already resolves (M6 task 10, row 22).
  const statusLoaded = useStatusLoaded();
  // Provider auth dot — refreshed from canonical runtime invalidation events.
  const { data: authStatus } = useRuntimeRpc<AuthStatusResult>(
    live,
    statusLoaded ? "models.authStatus" : null,
  );
  // Model catalog + owner default, from the runtime config snapshot. Reading the
  // config is admin-only on the backend, so a member never asks for it.
  const { data: configSnapshot } = useRuntimeRpc<unknown>(
    live,
    statusLoaded && !orgRestricted ? "config.get" : null,
  );
  const { data: managedCatalog } = useRuntimeRpc<unknown>(
    live,
    statusLoaded && state.kind === "ready" && state.account.provider_choice === "openrouter_managed"
      ? "models.list"
      : null,
  );
  const config = useMemo(() => extractConfig(configSnapshot), [configSnapshot]);
  const managed = state.kind === "ready" && state.account.provider_choice === "openrouter_managed";
  const groups = useMemo(() => {
    if (!managed) return listGroupedModels(config);
    const models = listCatalogModels(managedCatalog);
    return models.length ? [{ providerKey: "custom", label: "Lightbulb managed", models }] : [];
  }, [config, managed, managedCatalog]);
  const defaultRef = managed
    ? (managedCatalog as { defaultId?: string } | undefined)?.defaultId
    : readDefaultModelRef(config);

  const restricted = orgRestricted || state.kind === "restricted";

  // Provider card ------------------------------------------------------------
  let providerCard: ReactNode;
  if (restricted) {
    providerCard = (
      <Card>
        <Text style={styles.overline}>PROVIDER</Text>
        <Text style={styles.bodyText}>
          Provider &amp; billing are managed by your organization admin
        </Text>
      </Card>
    );
  } else if (state.kind === "loading") {
    providerCard = <SkeletonCards />;
  } else if (state.kind === "error") {
    providerCard = (
      <View style={styles.errorCard}>
        <Text style={styles.errorText}>
          Couldn’t load provider: {state.message}
        </Text>
        <Pressable
          style={({ pressed }) => [
            styles.retryButton,
            pressed && styles.retryButtonPressed,
          ]}
          onPress={() => void retry()}
          accessibilityRole="button"
          accessibilityLabel="Retry"
        >
          <Text style={styles.retryText}>Retry</Text>
        </Pressable>
      </View>
    );
  } else if (state.kind === "none") {
    providerCard = (
      <Card>
        <Text style={styles.overline}>PROVIDER</Text>
        <Text style={styles.bodyText}>No provider selected yet</Text>
        <Text style={styles.captionText}>
          Finish setup on thelightbulb.company to pick a model provider.
        </Text>
        <ManageLink label="Set up on thelightbulb.company" />
      </Card>
    );
  } else {
    const { account } = state;
    const provider = providerLabel(account);
    // Resolve the live auth dot; omit it entirely when the gateway hasn't
    // reported a status yet (W2 §3 — provider label is valid without a dot).
    const status = resolveProviderStatus(
      account.provider_choice,
      account.byo_provider,
      authStatus?.providers,
    );
    const dot = status != null ? statusToDotColor(status) : null;
    providerCard = (
      <Card>
        <Text style={styles.overline}>PROVIDER</Text>
        <View style={styles.providerRow}>
          {dot != null && (
            <View style={[styles.statusDot, { backgroundColor: dotFill(dot, palette) }]} />
          )}
          <Text style={styles.bodyText}>{provider.title}</Text>
        </View>
        {dot != null && (
          <Text style={styles.captionText}>{dotLabel(dot)}</Text>
        )}
        {provider.detail != null && (
          <Text style={styles.captionText}>{provider.detail}</Text>
        )}
        {account.capped_today === true && (
          <Text style={styles.bodyText}>Daily allowance used — resets at 00:00 UTC</Text>
        )}
        <ManageLink label="Choose or change provider on thelightbulb.company" />
      </Card>
    );
  }

  // Credits card (managed inference) ----------------------------------------
  let creditsCard: ReactNode = null;
  if (
    !restricted &&
    state.kind === "ready" &&
    state.account.provider_choice === "openrouter_managed"
  ) {
    const credits = state.credits;
    creditsCard = (
      <Card>
        <Text style={styles.overline}>WORKSPACE CREDITS</Text>
        <Text style={styles.balanceText}>
          {credits?.balance_dollars != null ? `$${credits.balance_dollars}` : "—"}
        </Text>
        <Text style={styles.captionText}>
          {credits?.balance_dollars != null
            ? "Prepaid balance for managed inference."
            : "Balance unavailable right now."}
        </Text>
      </Card>
    );
  }

  // Model catalog card -------------------------------------------------------
  let catalogCard: ReactNode;
  if (restricted) {
    // The provider card already says the admin manages this; there is no catalog to load.
    catalogCard = null;
  } else if ((managed ? managedCatalog : configSnapshot) === undefined && groups.length === 0) {
    catalogCard = (
      <Card>
        <Text style={styles.overline}>MODEL CATALOG</Text>
        <Animated.View style={styles.skeletonLine} />
        <Animated.View style={[styles.skeletonLine, { width: "60%" }]} />
      </Card>
    );
  } else if (groups.length === 0) {
    catalogCard = (
      <Card>
        <Text style={styles.overline}>MODEL CATALOG</Text>
        <Text style={styles.captionText}>
          {managed
            ? "No approved models are available right now."
            : "No models are listed in your gateway config. This is normal for the ChatGPT plan and some bring-your-own-key setups."}
        </Text>
      </Card>
    );
  } else {
    catalogCard = (
      <Card>
        <Text style={styles.overline}>MODEL CATALOG</Text>
        {groups.map((group) => (
          <View key={group.providerKey} style={styles.group}>
            <Text style={styles.groupLabel}>{group.label}</Text>
            {group.models.map((model) => {
              const isDefault = model.ref === defaultRef;
              return (
                <View key={model.ref} style={styles.modelRow}>
                  <Text style={styles.modelName} numberOfLines={1}>
                    {model.name}
                  </Text>
                  {isDefault && (
                    <View style={styles.defaultBadge}>
                      <Text style={styles.defaultBadgeText}>DEFAULT</Text>
                    </View>
                  )}
                </View>
              );
            })}
          </View>
        ))}
        <Text style={styles.captionText}>
          Model routing is read-only here. Change the default on thelightbulb.company.
        </Text>
      </Card>
    );
  }

  return (
    <ScrollView
      contentInsetAdjustmentBehavior="automatic"
      style={styles.page}
      contentContainerStyle={styles.content}
    >
      {providerCard}
      {creditsCard}
      {catalogCard}
    </ScrollView>
  );
}

function makeStyles(p: ChatPalette) {
  return StyleSheet.create({
    page: { flex: 1, backgroundColor: p.ground },
    content: {
      padding: spacing.lg,
      gap: spacing.md,
      paddingBottom: spacing.xl,
    },

    card: {
      backgroundColor: p.surface,
      borderRadius: radii.lg,
      borderWidth: 1,
      borderColor: p.edge,
      padding: spacing.lg,
      gap: spacing.sm,
    },

    overline: {
      fontFamily: fonts.sansMedium,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 1.9,
      color: p.fgMute,
    },
    bodyText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    captionText: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: p.fgMute },

    // Provider row (label + live auth dot).
    providerRow: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
    },
    statusDot: { width: 8, height: 8, borderRadius: 4 },

    // Credits.
    balanceText: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 20,
      lineHeight: 26,
      color: p.fg,
    },

    // Model catalog groups.
    group: { gap: spacing.xs },
    groupLabel: {
      fontFamily: fonts.mono,
      fontSize: 10,
      lineHeight: 14,
      letterSpacing: 0.6,
      color: p.fgMute,
    },
    modelRow: {
      flexDirection: "row",
      alignItems: "center",
      gap: spacing.sm,
      paddingVertical: 2,
    },
    modelName: {
      flex: 1,
      fontFamily: fonts.sans,
      fontSize: 13,
      lineHeight: 18,
      color: p.fg,
    },
    defaultBadge: {
      backgroundColor: p.raised,
      borderRadius: radii.xs,
      paddingHorizontal: spacing.xs,
      paddingVertical: 2,
    },
    defaultBadgeText: {
      fontFamily: fonts.sansSemiBold,
      fontSize: 9,
      lineHeight: 12,
      letterSpacing: 0.4,
      color: p.fg,
    },

    // Deep-link-out row.
    linkRow: {
      marginTop: spacing.xs,
      alignSelf: "flex-start",
      borderRadius: radii.xs,
    },
    linkRowPressed: { opacity: 0.6 },
    linkText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.link,
    },

    // Loading skeleton (§7).
    skeletonLine: {
      height: 14,
      borderRadius: radii.xs,
      backgroundColor: p.raised,
    },

    // Error card (§4 trio).
    errorCard: {
      padding: spacing.lg,
      backgroundColor: p.raised,
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.md,
      gap: spacing.md,
    },
    errorText: {
      fontFamily: fonts.sans,
      fontSize: 14,
      lineHeight: 20,
      color: p.alert,
    },
    retryButton: {
      alignSelf: "flex-start",
      borderWidth: 1,
      borderColor: p.alert,
      borderRadius: radii.sm,
      backgroundColor: p.surface,
      paddingHorizontal: spacing.md,
      paddingVertical: spacing.xs,
    },
    retryButtonPressed: { opacity: 0.6 },
    retryText: {
      fontFamily: fonts.sansMedium,
      fontSize: 13,
      lineHeight: 18,
      color: p.alert,
    },
  });
}

const SHEETS = { light: makeStyles(chatPalette.light), dark: makeStyles(chatPalette.dark) };
