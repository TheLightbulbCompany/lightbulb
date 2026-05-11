"use client";

import { useCallback, useEffect, useState } from "react";
import { useOrganization } from "@clerk/nextjs";
import { Loader2 } from "lucide-react";
import { ApiError, useApi } from "@/lib/api";
import { useChatGPTOAuth } from "@/hooks/useChatGPTOAuth";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

/**
 * Post-onboarding provider switch. Distinct from `ProviderPicker` in
 * ProvisioningStepper, which goes through `/billing/trial-checkout` and a
 * Stripe Checkout redirect — this modal targets `/billing/provider` and
 * assumes an already-active subscription.
 *
 * UX flow:
 *   1. Pick a provider card (3 for personal, 2 for org). Cutover warning
 *      is rendered at the top so it's visible BEFORE the user commits.
 *   2. If ChatGPT OAuth → render the device-code flow inline (reuses
 *      useChatGPTOAuth, which auto-fires /status + /start on mount).
 *      On completion we POST /billing/provider.
 *   3. If BYO key → collect provider + key, PUT /settings/keys, then
 *      POST /billing/provider.
 *   4. If Bedrock → POST /billing/provider directly (credits gate is
 *      enforced at provisioning time by provision_gate).
 *
 * Errors from /billing/provider carry a `{code, message}` payload from
 * the backend (ChangeProviderError). We surface message verbatim — the
 * codes (`byo_key_missing`, `oauth_required`, etc.) are stable so a
 * future per-code UI affordance can be wired up if useful.
 */

type ProviderChoice = "chatgpt_oauth" | "byo_key" | "bedrock_claude";
type ByoProvider = "openai" | "anthropic";

type Stage =
  | { kind: "picker" }
  | { kind: "chatgpt-oauth" }
  | { kind: "byo-key" }
  | { kind: "submitting"; choice: ProviderChoice; byo?: ByoProvider }
  | { kind: "error"; message: string };

interface ChangeProviderModalProps {
  open: boolean;
  currentProvider?: ProviderChoice | null;
  currentByoProvider?: ByoProvider | null;
  onClose: () => void;
  onCommitted: () => void;
}

export function ChangeProviderModal({
  open,
  currentProvider,
  currentByoProvider,
  onClose,
  onCommitted,
}: ChangeProviderModalProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent className="max-w-2xl">
        {/*
          Stage state lives inside the keyed body so each open mounts a
          fresh component — React resets `stage` to "picker" automatically,
          no synchronous setState-in-effect needed (avoids the
          react-hooks/set-state-in-effect lint rule). Pattern mirrors
          ExecApprovalDialog → ActiveDialog (commit c72b03f6).
        */}
        {open && (
          <ChangeProviderBody
            currentProvider={currentProvider}
            currentByoProvider={currentByoProvider}
            onClose={onClose}
            onCommitted={onCommitted}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

interface BodyProps {
  currentProvider?: ProviderChoice | null;
  currentByoProvider?: ByoProvider | null;
  onClose: () => void;
  onCommitted: () => void;
}

function ChangeProviderBody({
  currentProvider,
  currentByoProvider,
  onClose,
  onCommitted,
}: BodyProps) {
  const api = useApi();
  const { organization } = useOrganization();
  const isOrg = Boolean(organization?.id);
  const [stage, setStage] = useState<Stage>({ kind: "picker" });

  const commit = useCallback(
    async (choice: ProviderChoice, byo?: ByoProvider) => {
      setStage({ kind: "submitting", choice, byo });
      try {
        await api.post("/billing/provider", {
          provider_choice: choice,
          ...(byo ? { byo_provider: byo } : {}),
        });
        onCommitted();
        onClose();
      } catch (e: unknown) {
        // Backend ChangeProviderError surfaces as {code, message}. Render
        // the human message; the code stays in the network response for
        // ops debugging.
        let message = "Couldn't change provider.";
        if (e instanceof ApiError) {
          // FastAPI wraps the dict detail under `detail` — try both shapes.
          const data = e.body as { detail?: { message?: string } } | undefined;
          if (data?.detail?.message) message = data.detail.message;
          else if (e.message) message = e.message;
        } else if (e instanceof Error) {
          message = e.message;
        }
        setStage({ kind: "error", message });
      }
    },
    [api, onClose, onCommitted],
  );

  const handlePick = useCallback(
    (choice: ProviderChoice) => {
      // No-op pick: same provider AND the byo sub-provider isn't switching.
      // Bedrock and ChatGPT have no sub-provider so equality is exact;
      // BYO defers the no-op check until the user picks a sub-provider.
      if (choice !== "byo_key" && choice === currentProvider) {
        onClose();
        return;
      }
      if (choice === "chatgpt_oauth") {
        setStage({ kind: "chatgpt-oauth" });
        return;
      }
      if (choice === "byo_key") {
        setStage({ kind: "byo-key" });
        return;
      }
      void commit(choice);
    },
    [commit, currentProvider, onClose],
  );

  return (
    <>
      <DialogHeader>
        <DialogTitle>Change provider</DialogTitle>
        <DialogDescription>
          Switching ends your current chat session — your container restarts
          with the new provider. Conversation history is preserved.
        </DialogDescription>
      </DialogHeader>

      {stage.kind === "picker" && (
        <PickerStage
          isOrg={isOrg}
          currentProvider={currentProvider}
          currentByoProvider={currentByoProvider}
          onPick={handlePick}
        />
      )}

      {stage.kind === "chatgpt-oauth" && (
        <ChatGPTOAuthStage
          onComplete={() => void commit("chatgpt_oauth")}
          onCancel={() => setStage({ kind: "picker" })}
        />
      )}

      {stage.kind === "byo-key" && (
        <ByoKeyStage
          currentByoProvider={currentByoProvider}
          onComplete={(byo) => void commit("byo_key", byo)}
          onCancel={() => setStage({ kind: "picker" })}
        />
      )}

      {stage.kind === "submitting" && (
        <div className="flex items-center justify-center gap-3 py-12 text-sm text-[#5a5549]">
          <Loader2 className="h-4 w-4 animate-spin" />
          Switching to {readableProvider(stage.choice, stage.byo)}…
        </div>
      )}

      {stage.kind === "error" && (
        <div className="space-y-4 py-6">
          <p className="text-sm text-red-600">{stage.message}</p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setStage({ kind: "picker" })}>
              Back
            </Button>
          </DialogFooter>
        </div>
      )}
    </>
  );
}

function readableProvider(choice: ProviderChoice, byo?: ByoProvider): string {
  if (choice === "bedrock_claude") return "Bedrock-managed Claude";
  if (choice === "chatgpt_oauth") return "ChatGPT OAuth";
  if (choice === "byo_key" && byo) return `${byo === "openai" ? "OpenAI" : "Anthropic"} (BYO key)`;
  return "BYO key";
}

// ---------------------------------------------------------------------------
// Picker stage
// ---------------------------------------------------------------------------

interface PickerStageProps {
  isOrg: boolean;
  currentProvider?: ProviderChoice | null;
  currentByoProvider?: ByoProvider | null;
  onPick: (choice: ProviderChoice) => void;
}

function PickerStage({ isOrg, currentProvider, currentByoProvider, onPick }: PickerStageProps) {
  type Card = {
    id: ProviderChoice;
    title: string;
    subtitle: string;
  };
  const cards: Card[] = [
    {
      id: "bedrock_claude",
      title: "Powered by Claude",
      subtitle: "Bedrock-managed Claude credits",
    },
    {
      id: "chatgpt_oauth",
      title: "Sign in with ChatGPT",
      subtitle: "Inference via your ChatGPT subscription",
    },
    {
      id: "byo_key",
      title: "Bring your own API key",
      subtitle: "OpenAI or Anthropic — your key, your billing",
    },
  ];
  const visibleCards = isOrg ? cards.filter((c) => c.id !== "chatgpt_oauth") : cards;

  const isCurrent = (id: ProviderChoice) => {
    if (id !== currentProvider) return false;
    // BYO is "current" only if the user is on byo_key today; sub-provider
    // collected in the BYO stage. Marking byo_key "current" still lets the
    // user re-open BYO to rotate keys or swap openai↔anthropic.
    return true;
  };

  return (
    <div className="grid grid-cols-1 gap-3 py-2 md:grid-cols-3">
      {visibleCards.map((card) => {
        const current = isCurrent(card.id);
        return (
          <button
            key={card.id}
            type="button"
            onClick={() => onPick(card.id)}
            className={
              "rounded-xl border p-4 text-left transition-colors " +
              (current
                ? "border-[#06402B] bg-[#f3efe6]"
                : "border-[#e0dbd0] bg-white hover:bg-[#faf7f2]")
            }
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <h3 className="font-medium text-[#1a1a1a]">{card.title}</h3>
                <p className="mt-1 text-xs text-[#8a8578]">{card.subtitle}</p>
              </div>
              {current && (
                <span className="rounded-full bg-[#06402B] px-2 py-0.5 text-[10px] font-medium text-white">
                  Current
                </span>
              )}
            </div>
            {card.id === "byo_key" && current && currentByoProvider && (
              <p className="mt-2 text-[11px] text-[#8a8578]">
                Active sub-provider: {currentByoProvider === "openai" ? "OpenAI" : "Anthropic"}
              </p>
            )}
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ChatGPT OAuth stage — reuses useChatGPTOAuth (auto-fires /status + /start)
// ---------------------------------------------------------------------------

interface ChatGPTOAuthStageProps {
  onComplete: () => void;
  onCancel: () => void;
}

function ChatGPTOAuthStage({ onComplete, onCancel }: ChatGPTOAuthStageProps) {
  const { state, start } = useChatGPTOAuth();

  useEffect(() => {
    if (state.status === "completed") onComplete();
  }, [state, onComplete]);

  return (
    <div className="space-y-4 py-4">
      {state.status === "idle" && (
        <div className="flex flex-col items-center gap-3 py-6 text-sm text-[#5a5549]">
          <Loader2 className="h-5 w-5 animate-spin" />
          Starting ChatGPT sign-in…
        </div>
      )}

      {state.status === "pending" && (
        <ol className="list-decimal space-y-3 pl-5 text-sm text-[#1a1a1a]">
          <li>
            Open{" "}
            <a
              href={state.verificationUri}
              target="_blank"
              rel="noreferrer"
              className="text-[#06402B] underline"
            >
              {state.verificationUri}
            </a>{" "}
            and sign in to your ChatGPT account.
          </li>
          <li>
            Enter this code:
            <div className="mt-2">
              <code className="rounded bg-[#f3efe6] px-3 py-2 font-mono text-lg tracking-widest">
                {state.userCode}
              </code>
            </div>
          </li>
          <li className="text-xs text-[#8a8578]">
            We&apos;ll switch you over automatically once you finish sign-in.
          </li>
        </ol>
      )}

      {state.status === "error" && (
        <div className="space-y-3 py-4">
          <p className="text-sm text-red-600">Connection failed: {state.message}</p>
          <Button variant="outline" onClick={start}>
            Try again
          </Button>
        </div>
      )}

      {state.status === "completed" && (
        <div className="flex items-center justify-center py-6 text-sm text-[#06402B]">
          ✓ Connected — committing switch…
        </div>
      )}

      <DialogFooter>
        <Button variant="ghost" onClick={onCancel}>
          Back
        </Button>
      </DialogFooter>
    </div>
  );
}

// ---------------------------------------------------------------------------
// BYO key stage — PUT /settings/keys then commit
// ---------------------------------------------------------------------------

interface ByoKeyStageProps {
  currentByoProvider?: ByoProvider | null;
  onComplete: (byo: ByoProvider) => void;
  onCancel: () => void;
}

function ByoKeyStage({ currentByoProvider, onComplete, onCancel }: ByoKeyStageProps) {
  const api = useApi();
  const [provider, setProvider] = useState<ByoProvider>(currentByoProvider ?? "openai");
  const [apiKey, setApiKey] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      // /settings/keys/{provider} validates the key against the upstream
      // provider before storing. If validation fails the error bubbles
      // up here and we stay on the form — the user fixes the key and
      // re-submits without a wasted /billing/provider call.
      await api.put(`/settings/keys/${provider}`, { api_key: apiKey });
      onComplete(provider);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Couldn't save key");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-4 py-4">
      <fieldset className="flex gap-4">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="radio"
            value="openai"
            checked={provider === "openai"}
            onChange={() => setProvider("openai")}
          />
          OpenAI
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="radio"
            value="anthropic"
            checked={provider === "anthropic"}
            onChange={() => setProvider("anthropic")}
          />
          Anthropic
        </label>
      </fieldset>

      <input
        type="password"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        placeholder={provider === "openai" ? "sk-proj-…" : "sk-ant-…"}
        required
        autoComplete="off"
        spellCheck={false}
        className="w-full rounded-md border border-[#e0dbd0] bg-white px-3 py-2 font-mono text-sm"
      />

      <p className="text-xs text-[#8a8578]">
        Stored encrypted in AWS Secrets Manager. We validate the key against the
        provider before saving — invalid keys are rejected here, not at chat time.
      </p>

      {error && <p className="text-sm text-red-600">{error}</p>}

      <DialogFooter>
        <Button type="button" variant="ghost" onClick={onCancel} disabled={submitting}>
          Back
        </Button>
        <Button type="submit" disabled={submitting || !apiKey}>
          {submitting ? "Validating…" : "Save and switch"}
        </Button>
      </DialogFooter>
    </form>
  );
}
