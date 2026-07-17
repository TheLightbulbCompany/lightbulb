"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";
import { useChatGPTOAuth } from "@/hooks/useChatGPTOAuth";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { CreditsSection } from "./CreditsSection";
import { ModelRoutingCard } from "./ModelRoutingCard";
import { OpenAIIcon, AnthropicIcon } from "@/components/chat/ProviderIcons";
import { Input } from "@/components/ui/input";
import { ChangeProviderModal } from "./ChangeProviderModal";
import {
  resolveProviderStatus,
  statusToDotColor,
  type AuthStatusProvider,
  type DotColor,
} from "./llm-auth-status";

import type { ProviderChoiceId as ProviderChoice } from "@/lib/providerCards";
type ByoProvider = "openai" | "anthropic";

type UserData = {
  provider_choice?: ProviderChoice | null;
  byo_provider?: ByoProvider | null;
};

const HERO_TILE = "h-12 w-12 rounded-lg bg-[#f3efe6] flex items-center justify-center flex-shrink-0";
const ACTION_CARD = "rounded-lg border border-[#e0dbd0] bg-white p-4 space-y-3";
const EYEBROW = "text-[10px] uppercase tracking-wider text-[#8a8578]/60";

// Dot color is driven by the container's live models.authStatus, not the
// chip label — the label states which provider is configured, the dot states
// whether its auth is currently usable.
const DOT_CLASS: Record<DotColor, string> = {
  green: "bg-[#2d8a4e]",
  amber: "bg-amber-500",
  red: "bg-red-500",
  neutral: "bg-[#8a8578]",
};

function StatusChip({ label, dotColor }: { label: string; dotColor: DotColor }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full bg-[#06402B]/10 text-[#06402B] px-2 py-0.5 text-xs font-medium">
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
    <div className="rounded-xl border border-[#e0dbd0] bg-white p-6 flex items-center gap-4">
      <div className={HERO_TILE}>{icon}</div>
      <div className="flex-1 min-w-0">
        <h3 className="font-medium text-[#1a1a1a]">{title}</h3>
        <p className="text-sm text-[#8a8578] truncate">{subtitle}</p>
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
        <p className="text-sm text-[#1a1a1a]">Connected via OAuth</p>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={onChangeProvider}
            className="rounded-md border border-[#e0dbd0] text-[#1a1a1a] hover:bg-[#f3efe6] px-3 py-1.5 text-sm"
          >
            Change provider
          </button>
          <button
            onClick={handleDisconnect}
            disabled={busy}
            className="rounded-md border border-[#e0dbd0] text-[#1a1a1a] hover:bg-[#f3efe6] px-3 py-1.5 text-sm disabled:opacity-50"
          >
            {busy ? "Disconnecting…" : "Disconnect"}
          </button>
        </div>
      </div>
    </>
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
        <p className="text-xs text-[#8a8578]">
          Stored encrypted in AWS Secrets Manager. Paste a new key to rotate.
        </p>
        <ReplaceKeyForm currentProvider={byoProvider} onReplaced={onReplaced} />
        <button
          onClick={onChangeProvider}
          className="rounded-md border border-[#e0dbd0] text-[#1a1a1a] hover:bg-[#f3efe6] px-3 py-1.5 text-sm"
        >
          Change provider
        </button>
      </div>
    </>
  );
}

function BedrockBlock({
  onChangeProvider,
  dotColor,
}: {
  onChangeProvider: () => void;
  dotColor: DotColor;
}) {
  return (
    <>
      <HeroCard
        icon={<AnthropicIcon size={32} />}
        title="Powered by Claude"
        subtitle="Anthropic Claude via AWS Bedrock"
        status="Active"
        dotColor={dotColor}
      />
      <div className={ACTION_CARD}>
        <span className={EYEBROW}>Provider</span>
        <p className="text-sm text-[#1a1a1a]">
          Credits and auto-reload are managed below.
        </p>
        <button
          onClick={onChangeProvider}
          className="rounded-md border border-[#e0dbd0] text-[#1a1a1a] hover:bg-[#f3efe6] px-3 py-1.5 text-sm"
        >
          Change provider
        </button>
      </div>
    </>
  );
}

function EmptyStateCard({
  message,
  onChangeProvider,
}: {
  message?: string;
  onChangeProvider?: () => void;
}) {
  return (
    <div className={ACTION_CARD}>
      <p className="text-sm text-[#1a1a1a]">
        {message ?? "You haven’t picked a provider yet."}
      </p>
      {/* When a billing row already exists (e.g. user disconnected ChatGPT
          OAuth — provider_choice was cleared but the subscription is still
          active) ``onChangeProvider`` opens the picker in-place. The
          ``/onboarding`` link is the pre-subscription fallback. */}
      {onChangeProvider ? (
        <button
          onClick={onChangeProvider}
          className="inline-flex rounded-full bg-[#06402B] hover:bg-[#0a5c3e] text-white px-4 py-2 text-sm"
        >
          Pick a provider →
        </button>
      ) : (
        <Link
          href="/onboarding"
          className="inline-flex rounded-full bg-[#06402B] hover:bg-[#0a5c3e] text-white px-4 py-2 text-sm"
        >
          Re-onboard →
        </Link>
      )}
    </div>
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
  const [prevInitialAction, setPrevInitialAction] = useState(initialAction);
  if (initialAction !== prevInitialAction) {
    setPrevInitialAction(initialAction);
    if (initialAction === "change-provider") setModalOpen(true);
  }

  useEffect(() => {
    if (initialAction !== "change-provider") return;
    onActionConsumed?.();
  }, [initialAction, onActionConsumed]);

  // Live container auth health for the provider dot. The panel mounts under the
  // control surface's GatewayProvider, so the RPC resolves. These hooks sit
  // above the early return below so hook order stays stable across renders.
  const { data: authStatus, mutate: revalidateAuthStatus } = useGatewayRpc<{
    ts: number;
    providers: AuthStatusProvider[];
  }>("models.authStatus");
  const refreshAuthStatus = useGatewayRpcMutation();
  const providerStatus = resolveProviderStatus(
    user?.provider_choice ?? null,
    user?.byo_provider ?? null,
    authStatus?.providers,
  );
  const dotColor: DotColor = statusToDotColor(providerStatus);

  if (!user) return <div className="p-6 text-sm">Loading…</div>;

  const openModal = () => setModalOpen(true);

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Model & Credits</h2>
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
          className="rounded-md border border-[#e0dbd0] text-[#8a8578] hover:bg-[#f3efe6] px-2.5 py-1 text-xs"
        >
          Refresh status
        </button>
      </div>

      {user.provider_choice === "chatgpt_oauth" && (
        <ChatGPTOAuthBlock
          onDisconnected={() => mutate()}
          onChangeProvider={openModal}
          dotColor={dotColor}
        />
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
        <EmptyStateCard
          message="Your bring-your-own-key configuration is incomplete. Pick a provider to finish setting up."
          onChangeProvider={openModal}
        />
      )}

      {user.provider_choice === "bedrock_claude" && (
        <BedrockBlock onChangeProvider={openModal} dotColor={dotColor} />
      )}

      {!user.provider_choice && <EmptyStateCard onChangeProvider={openModal} />}

      {/* Credits live inline for Bedrock owners — the former Credits panel.
          Non-Bedrock providers bill through their own account, so nothing
          renders for them. */}
      {user.provider_choice === "bedrock_claude" && <CreditsSection />}

      {/* Owner-level model routing (default + overhead model). Provider-aware:
          renders dropdowns when there's a routable catalog (Bedrock), a fixed
          summary on the flat-rate ChatGPT plan, and hides for catalog-less BYO. */}
      <ModelRoutingCard providerChoice={user.provider_choice ?? null} />

      <ChangeProviderModal
        open={modalOpen}
        currentProvider={user.provider_choice ?? null}
        currentByoProvider={user.byo_provider ?? null}
        onClose={() => setModalOpen(false)}
        onCommitted={() => {
          // SWR refetch picks up the new provider_choice. The container
          // restart fires asynchronously in the background_provision task;
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
          className="rounded-full bg-[#06402B] hover:bg-[#0a5c3e] text-white px-4 py-2 text-sm disabled:opacity-50"
        >
          Save
        </button>
      </form>
    </div>
  );
}
