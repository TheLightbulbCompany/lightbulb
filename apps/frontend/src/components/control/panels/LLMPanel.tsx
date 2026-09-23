"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { AlertCircle, KeyRound } from "lucide-react";
import { useApi } from "@/lib/api";
import { externalLinkClick } from "@/lib/externalAuth";
import { notifyApiError } from "@/lib/notify";
import { useChatGPTOAuth } from "@/hooks/useChatGPTOAuth";
import { useBilling } from "@/hooks/useBilling";
import { useStatusLoaded } from "@lightbulb/shared";
import { useRuntimeRpc, useRuntimeRpcMutation } from "@/hooks/useRuntimeRpc";
import { CreditsSection } from "./CreditsSection";
import { OpenAIIcon, AnthropicIcon } from "@/components/chat/ProviderIcons";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ChangeProviderModal,
  type CredentialProviderChoice,
} from "./ChangeProviderModal";
import type { ByoProvider } from "@/lib/providerCommit";
import {
  resolveProviderStatus,
  statusToDotColor,
  type AuthStatusProvider,
  type DotColor,
} from "./llm-auth-status";

import { PROVIDER_SWITCH_CARDS } from "@/lib/providerCards";
import { isFreeTierExpired } from "@/lib/planLabel";

type UserData = {
  // Plain string, not `ProviderChoice`: this is the owner's STORED choice,
  // which can carry a legacy value (e.g. a retired `bedrock_claude` row, of
  // which there are none left) no longer offered by the picker. Every render
  // branch below matches it by string comparison, so an unrecognized legacy
  // value simply matches no block instead of throwing on a type it no longer
  // recognizes as pickable.
  provider_choice?: string | null;
  byo_provider?: ByoProvider | null;
};

const HERO_TILE = "h-12 w-12 rounded-lg bg-[var(--cream-deep)] flex items-center justify-center shrink-0";
const ACTION_CARD = "rounded-lg border border-[var(--edge)] bg-cream p-4 space-y-3";
const EYEBROW = "label-caps";

// Dot color is driven by the container's live models.authStatus, not the
// chip label — the label states which provider is configured, the dot states
// whether its auth is currently usable.
const DOT_CLASS: Record<DotColor, string> = {
  green: "bg-ok",
  amber: "bg-warn",
  red: "bg-err",
  neutral: "bg-[var(--midnight-muted)]",
};

function StatusChip({ label, dotColor }: { label: string; dotColor: DotColor }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-[var(--cream-deep)] text-[var(--midnight)] px-2 py-0.5 text-xs font-medium">
      <span
        data-testid="provider-health-dot"
        data-color={dotColor}
        className={`h-1.5 w-1.5 rounded-full ${DOT_CLASS[dotColor]}`}
      />
      {label}
    </span>
  );
}

function HeroCard({
  icon,
  title,
  subtitle,
  status,
  dotColor,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  status: string;
  dotColor: DotColor;
}) {
  return (
    <div className="rounded-xl border border-[var(--edge)] bg-cream p-6 flex items-center gap-4">
      <div className={HERO_TILE}>{icon}</div>
      <div className="flex-1 min-w-0">
        <h3 className="font-medium text-[var(--midnight)]">{title}</h3>
        <p className="text-sm text-[var(--midnight-muted)] truncate">{subtitle}</p>
      </div>
      <StatusChip label={status} dotColor={dotColor} />
    </div>
  );
}

function ChatGPTOAuthBlock({
  onDisconnected,
  onChangeProvider,
  dotColor,
}: {
  onDisconnected: () => void;
  onChangeProvider: () => void;
  dotColor: DotColor;
}) {
  const { disconnect } = useChatGPTOAuth();
  const [busy, setBusy] = useState(false);

  const handleDisconnect = async () => {
    setBusy(true);
    try {
      await disconnect();
      onDisconnected();
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <HeroCard
        icon={<OpenAIIcon size={32} />}
        title="Sign in with ChatGPT"
        subtitle="Inference via your ChatGPT account"
        status="Connected"
        dotColor={dotColor}
      />
      <div className={ACTION_CARD}>
        <span className={EYEBROW}>Account</span>
        <p className="text-sm text-[var(--midnight)]">Connected via OAuth</p>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={onChangeProvider}
            className="rounded-md border border-[var(--edge)] text-[var(--midnight)] hover:bg-[var(--cream-deep)] px-3 py-1.5 text-sm"
          >
            Change provider
          </button>
          <button
            onClick={handleDisconnect}
            disabled={busy}
            className="rounded-md border border-[var(--edge)] text-[var(--midnight)] hover:bg-[var(--cream-deep)] px-3 py-1.5 text-sm disabled:opacity-50"
          >
            {busy ? "Disconnecting…" : "Disconnect"}
          </button>
        </div>
      </div>
    </>
  );
}

function CodingHarnessBlock() {
  // Second, independent ChatGPT sign-in dedicated to the OpenCode coding
  // harness. Sharing the provider's token chain is not an option — OpenAI
  // refresh tokens rotate on use, and two holders of one chain invalidate
  // each other. autoStart:false — opening the panel must not begin a
  // device-code session; the button does.
  const { state, start, disconnect } = useChatGPTOAuth({
    basePath: "/oauth/chatgpt/harness",
    autoStart: false,
  });
  const [busy, setBusy] = useState(false);
  // Ticks while a code is pending so the expired affordance appears on its
  // own — the device code lives ~15 minutes, and the poll loop reports
  // "pending" forever on an expired session, so without this the card would
  // wait indefinitely with no way to get a new code.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (state.status !== "pending") return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [state.status]);

  const handleDisconnect = async () => {
    setBusy(true);
    try {
      await disconnect();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={ACTION_CARD} data-testid="coding-harness-block">
      <span className={EYEBROW}>Coding harness</span>
      {state.status === "completed" && (
        <>
          <p className="text-sm text-[var(--midnight)]">
            OpenCode is connected. Your agents can delegate coding work using
            your ChatGPT subscription.
          </p>
          <button
            onClick={handleDisconnect}
            disabled={busy}
            className="self-start rounded-md border border-[var(--edge)] text-[var(--midnight)] hover:bg-[var(--cream-deep)] px-3 py-1.5 text-sm disabled:opacity-50"
          >
            {busy ? "Disconnecting…" : "Disconnect"}
          </button>
        </>
      )}
      {state.status === "pending" && (
        <>
          <p className="text-sm text-[var(--midnight)]">
            Open{" "}
            <a
              href={state.verificationUri}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => externalLinkClick(e, state.verificationUri)}
              className="underline"
            >
              {state.verificationUri}
            </a>{" "}
            and enter this code:
          </p>
          <p className="font-mono text-title tracking-caps text-[var(--midnight)]">{state.userCode}</p>
          {state.expiresAt - now <= 0 ? (
            <>
              <p className="text-xs text-[var(--err)]">
                This code expired. Get a new one to finish connecting.
              </p>
              <button
                onClick={() => void start()}
                className="self-start rounded-md border border-[var(--edge)] text-[var(--midnight)] hover:bg-[var(--cream-deep)] px-3 py-1.5 text-sm"
              >
                Get a new code
              </button>
            </>
          ) : (
            <p className="text-xs text-[var(--midnight-muted)]">
              Waiting for you to finish signing in — this updates automatically.
            </p>
          )}
        </>
      )}
      {(state.status === "idle" || state.status === "error") && (
        <>
          <p className="text-sm text-[var(--midnight-muted)]">
            Let your agents write code with OpenCode, powered by the ChatGPT
            subscription you already have. Requires one extra sign-in: the
            harness keeps its own credential so it never interferes with your
            main connection.
          </p>
          {state.status === "error" && (
            <p className="text-sm text-[var(--err)]">{state.message}</p>
          )}
          <button
            onClick={() => void start()}
            className="self-start rounded-md border border-[var(--edge)] text-[var(--midnight)] hover:bg-[var(--cream-deep)] px-3 py-1.5 text-sm"
          >
            {state.status === "error" ? "Try again" : "Connect coding harness"}
          </button>
        </>
      )}
    </div>
  );
}

function ByoKeyBlock({
  byoProvider,
  onReplaced,
  onChangeProvider,
  dotColor,
}: {
  byoProvider: ByoProvider;
  onReplaced: () => void;
  onChangeProvider: () => void;
  dotColor: DotColor;
}) {
  const isOpenAI = byoProvider === "openai";
  const title = isOpenAI ? "Bring your own OpenAI key" : "Bring your own Anthropic key";
  const icon = isOpenAI ? <OpenAIIcon size={32} /> : <AnthropicIcon size={32} />;

  return (
    <>
      <HeroCard
        icon={icon}
        title={title}
        subtitle="Your key, your billing"
        status="Active"
        dotColor={dotColor}
      />
      <div className={ACTION_CARD}>
        <span className={EYEBROW}>API key</span>
        <p className="text-xs text-[var(--midnight-muted)]">
          Stored encrypted in AWS Secrets Manager. Paste a new key to rotate.
        </p>
        <ReplaceKeyForm currentProvider={byoProvider} onReplaced={onReplaced} />
        <button
          onClick={onChangeProvider}
          className="rounded-md border border-[var(--edge)] text-[var(--midnight)] hover:bg-[var(--cream-deep)] px-3 py-1.5 text-sm"
        >
          Change provider
        </button>
      </div>
    </>
  );
}

function ManagedBlock({
  onChangeProvider,
  dotColor,
  creditsUnavailable = false,
}: {
  onChangeProvider: () => void;
  dotColor: DotColor;
  /** An explicit openrouter_managed row while prepaid credits can't be bought: every turn fails. */
  creditsUnavailable?: boolean;
}) {
  return (
    <>
      <HeroCard
        icon={<span className="text-xl font-semibold">✦</span>}
        title="Lightbulb-managed models"
        subtitle="Inference uses your Lightbulb workspace credits"
        status={creditsUnavailable ? "Unavailable" : "Active"}
        dotColor={creditsUnavailable ? "red" : dotColor}
      />
      <div className={ACTION_CARD}>
        <span className={EYEBROW}>Provider</span>
        {creditsUnavailable ? (
          <p className="text-sm text-err">
            Prepaid credits aren&apos;t available yet, so this workspace can&apos;t run. Switch to
            ChatGPT or your own API key.
          </p>
        ) : (
          <p className="text-sm text-[var(--midnight)]">Workspace credits are managed below.</p>
        )}
        <button
          onClick={onChangeProvider}
          className="rounded-md border border-[var(--edge)] text-[var(--midnight)] hover:bg-[var(--cream-deep)] px-3 py-1.5 text-sm"
        >
          Change provider
        </button>
      </div>
    </>
  );
}

// byo_key spans OpenAI *and* Anthropic, so a vendor mark would misrepresent
// it — a key glyph is the honest icon there.
const PROVIDER_ICONS: Record<CredentialProviderChoice, React.ReactNode> = {
  chatgpt_oauth: <OpenAIIcon size={24} />,
  byo_key: <KeyRound size={20} className="text-[var(--midnight-muted)]" />,
};

/**
 * Inline credential picker. The choices are rendered on the panel rather than
 * behind a button so an owner can see what the options are; a pick hands off to
 * ChangeProviderModal opened on that credential stage, which owns the OAuth
 * device-code and BYO-key flows. `disabled` = free trial, where a switch 409s
 * (use_trial_checkout) until the workspace is on a paid plan.
 */
function ProviderChoiceCards({
  onPick,
  disabled = false,
}: {
  onPick: (choice: CredentialProviderChoice) => void;
  disabled?: boolean;
}) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {PROVIDER_SWITCH_CARDS.map((card) => {
        const id = card.id as CredentialProviderChoice;
        return (
          <button
            key={id}
            type="button"
            onClick={() => onPick(id)}
            disabled={disabled}
            className="rounded-xl border border-[var(--edge)] bg-cream p-4 text-left transition-colors hover:border-[var(--midnight)] hover:bg-[var(--cream-deep)] disabled:opacity-50 disabled:hover:border-[var(--edge)] disabled:hover:bg-cream"
          >
            {/* Icon beside the copy on narrow widths, above it once the grid
                goes multi-column. */}
            <div className="flex items-start gap-3 sm:block">
              <div className={`${HERO_TILE} sm:mb-3`}>{PROVIDER_ICONS[id]}</div>
              <div className="min-w-0">
                <h3 className="text-sm font-medium text-[var(--midnight)]">{card.title}</h3>
                <p className="mt-1 text-xs text-[var(--midnight-muted)]">{card.subtitle}</p>
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}

/**
 * No provider_choice: the workspace runs on Lightbulb-managed DeepSeek within its
 * daily allowance while `trial_cap.is_managed_capped` holds (a live trial, or an
 * active/trialing plan). `blocked` names why it doesn't (expired trial, lapsed
 * subscription); then nothing runs and every switch 409s, so both point at Billing.
 * The credential paths are alternatives only on a paid, live plan.
 */
function ManagedAllowanceBlock({
  onPick,
  trial,
  blocked,
  prepaidSoon,
}: {
  onPick: (choice: CredentialProviderChoice) => void;
  trial: boolean;
  blocked: string | null;
  prepaidSoon: boolean;
}) {
  const plansLink = (
    <Link href="/settings?panel=billing" className="underline">
      See plans
    </Link>
  );
  return (
    <>
      <HeroCard
        icon={<span className="text-xl font-semibold">✦</span>}
        title="Lightbulb-managed models"
        subtitle="DeepSeek within a daily allowance"
        status={blocked ? "Paused" : "Active"}
        dotColor={blocked ? "red" : "green"}
      />
      {blocked && (
        <p className="text-sm text-err">
          {blocked} {plansLink}
        </p>
      )}
      {prepaidSoon && !blocked && (
        <p className="text-xs text-[var(--midnight-muted)]">
          More models with prepaid credits are coming soon.
        </p>
      )}
      <div className="space-y-3">
        <p className="text-sm text-[var(--midnight)]">
          Or use your own provider
          {trial && !blocked && <> on a paid plan. {plansLink}</>}
        </p>
        <ProviderChoiceCards onPick={onPick} disabled={trial || blocked !== null} />
      </div>
    </>
  );
}

export interface LLMPanelProps {
  /**
   * One-shot intent to auto-open the change-provider modal. ChatErrorCard's
   * rate_limit action and the provider_required gate's action_href both route
   * here relying on this auto-open. The shell reads its own channel
   * (``?action=change-provider`` on mobile) and threads it down. null = no intent.
   */
  initialAction?: "change-provider" | null;
  /** Fired once the intent is honored so the shell strips its channel. */
  onActionConsumed?: () => void;
}

export function LLMPanel({ initialAction = null, onActionConsumed }: LLMPanelProps = {}) {
  const api = useApi();
  const { account, isLoading: billingLoading } = useBilling();
  const managedAvailable = account?.managed_inference_available === true;
  const status = account?.subscription_status ?? null;
  const trial = status === "trial_local";
  // Mount snapshot: render must stay pure, and day granularity makes it fresh enough.
  const [nowMs] = useState(() => Date.now());
  // Mirrors backend is_managed_capped + TRIAL_EXPIRED for a row with no provider.
  // A null trial_end is NOT expired (it is stamped at first ready).
  const blocked = isFreeTierExpired(status, account?.trial_end, nowMs)
    ? "Your free trial has ended."
    : trial || status === "active" || status === "trialing"
      ? null
      : "No active subscription.";
  // A trial row reads plan "standard" (the backend's default), so exclude it explicitly.
  const prepaidSoon =
    !managedAvailable && !trial && (account?.plan === "standard" || account?.plan === "ultra_pro");
  const { data: user, mutate } = useSWR<UserData | null>(
    "/users/me",
    (p: string) => api.get(p) as Promise<UserData | null>,
  );
  // Auto-open the change-provider modal when the shell delivers the intent.
  // Seed from the prop on first render (no effect-driven setState, which would
  // trip react-hooks/set-state-in-effect), and — so a second command to an
  // already-mounted panel re-pops the modal (the old initializer-only read
  // ignored it) — re-open on a null→change-provider transition via the
  // render-time prev-compare pattern. The companion effect reports consumption
  // so the shell strips its channel; it does not touch the modal's open state.
  const [modalOpen, setModalOpen] = useState(
    () => initialAction === "change-provider",
  );
  // Which credential stage the modal should open on. null = the full picker,
  // which is what the "Change provider" buttons and the ?action= intent want.
  const [modalChoice, setModalChoice] =
    useState<CredentialProviderChoice | null>(null);
  const [prevInitialAction, setPrevInitialAction] = useState(initialAction);
  if (initialAction !== prevInitialAction) {
    setPrevInitialAction(initialAction);
    if (initialAction === "change-provider") {
      setModalChoice(null);
      setModalOpen(true);
    }
  }

  useEffect(() => {
    if (initialAction !== "change-provider") return;
    onActionConsumed?.();
  }, [initialAction, onActionConsumed]);

  // Live runtime auth health for the provider dot. The panel mounts under the
  // control surface's GatewayProvider, so the RPC resolves. These hooks sit
  // above the early return below so hook order stays stable across renders.
  // Held while the runtime is unknown (`undefined`), which on AgentCore is the
  // difference between a settings read and a microVM start. Provider IDS are not
  // branched: the Hermes bridge reports the same names `llm-auth-status.ts`
  // already matches (`custom`, `anthropic`, `openai-api`, `openai-codex`).
  const statusLoaded = useStatusLoaded();
  const {
    data: authStatus,
    error: authStatusError,
    mutate: revalidateAuthStatus,
  } = useRuntimeRpc<{
    ts: number;
    providers: AuthStatusProvider[];
  }>(statusLoaded ? "models.authStatus" : null);
  const refreshAuthStatus = useRuntimeRpcMutation();
  const providerStatus = resolveProviderStatus(
    user?.provider_choice ?? null,
    user?.byo_provider ?? null,
    authStatus?.providers,
  );
  const dotColor: DotColor = statusToDotColor(providerStatus);

  const handlePick = (choice: CredentialProviderChoice) => {
    setModalChoice(choice);
    setModalOpen(true);
  };

  if (!user || (billingLoading && (!user.provider_choice || (user.provider_choice === "byo_key" && !user.byo_provider)))) {
    return <div className="p-6 text-sm">Loading…</div>;
  }

  const openModal = () => {
    setModalChoice(null);
    setModalOpen(true);
  };

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="font-editorial-serif text-title">Model & Credits</h2>
        <button
          type="button"
          // The read hook only auto-revalidates on a matching gateway *event*,
          // and models.authStatus is an RPC (nothing pushes a matching event),
          // so Refresh must explicitly revalidate the read key. Order matters:
          // first bust the gateway's 60s cache snapshot via the refresh RPC,
          // then revalidate the read hook so its SWR cache (and the dot) pulls
          // the now-fresh snapshot.
          onClick={() =>
            void refreshAuthStatus("models.authStatus", { refresh: true }).then(() =>
              revalidateAuthStatus(),
            )
          }
          className="rounded-md border border-[var(--edge)] text-[var(--midnight-muted)] hover:bg-[var(--cream-deep)] px-2.5 py-1 text-xs"
        >
          Refresh status
        </button>
      </div>

      {/* H4: models.authStatus failing silently rendered as a neutral dot
          indistinguishable from "still loading" — surface it so a broken
          credential reads as an error, not a spinner that never resolves. */}
      {authStatusError && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-cream border border-err">
          <AlertCircle size={16} className="text-err shrink-0" />
          <span className="text-xs text-err">Couldn&apos;t check provider status.</span>
          <Button
            variant="outline"
            size="sm"
            className="ml-auto h-6 text-xs"
            onClick={() => void revalidateAuthStatus()}
          >
            Retry
          </Button>
        </div>
      )}

      {user.provider_choice === "chatgpt_oauth" && (
        <>
          <ChatGPTOAuthBlock
            onDisconnected={() => mutate()}
            onChangeProvider={openModal}
            dotColor={dotColor}
          />
          <CodingHarnessBlock />
        </>
      )}

      {user.provider_choice === "byo_key" && user.byo_provider && (
        <ByoKeyBlock
          byoProvider={user.byo_provider}
          onReplaced={() => mutate()}
          onChangeProvider={openModal}
          dotColor={dotColor}
        />
      )}

      {/* byo_key without byo_provider — webhook persisted provider_choice but
          backend never wrote byo_provider (see backend memory). Without this
          recovery state the panel renders blank for the affected user. */}
      {user.provider_choice === "byo_key" && !user.byo_provider && (
        <div className="space-y-3">
          <p className="text-sm text-[var(--midnight)]">
            Your bring-your-own-key configuration is incomplete. Pick a provider to finish setting up.
          </p>
          <ProviderChoiceCards onPick={handlePick} />
        </div>
      )}

      {user.provider_choice === "openrouter_managed" && (
        <ManagedBlock
          onChangeProvider={openModal}
          dotColor={dotColor}
          creditsUnavailable={!managedAvailable}
        />
      )}

      {!user.provider_choice && (
        <ManagedAllowanceBlock onPick={handlePick} trial={trial} blocked={blocked} prepaidSoon={prepaidSoon} />
      )}

      {/* Credits live inline for managed owners — the former Credits panel.
          BYO providers bill through their own account, so nothing
          renders for them. */}
      {user.provider_choice === "openrouter_managed" && managedAvailable && <CreditsSection />}

      <ChangeProviderModal
        open={modalOpen && !billingLoading}
        currentProvider={user.provider_choice ?? null}
        currentByoProvider={user.byo_provider ?? null}
        managedInferenceAvailable={managedAvailable}
        initialChoice={modalChoice}
        onClose={() => setModalOpen(false)}
        onCommitted={() => {
          // SWR refetch picks up the new provider_choice. The container
          // restart fires asynchronously in the dispatch_provision task;
          // the existing chat surface re-evaluates the provision gate on
          // its next /container/status poll and shows the right blocked
          // state if needed.
          void mutate();
        }}
      />
    </div>
  );
}

function ReplaceKeyForm({
  currentProvider,
  onReplaced,
}: {
  currentProvider: ByoProvider;
  onReplaced: () => void;
}) {
  const api = useApi();
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    try {
      await api.put(`/settings/keys/${currentProvider}`, { api_key: apiKey });
      setApiKey("");
      onReplaced();
    } catch (err) {
      notifyApiError(err, "Couldn't save key");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <form onSubmit={submit} className="flex gap-2">
        <Input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={currentProvider === "openai" ? "sk-proj-…" : "sk-ant-…"}
          className="flex-1 font-mono text-sm"
        />
        <button
          type="submit"
          disabled={submitting || !apiKey}
          className="rounded-full bg-[var(--midnight)] hover:opacity-90 text-cream px-4 py-2 text-sm disabled:opacity-50"
        >
          Save
        </button>
      </form>
    </div>
  );
}
