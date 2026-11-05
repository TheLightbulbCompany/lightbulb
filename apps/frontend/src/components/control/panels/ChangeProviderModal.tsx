"use client";

import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useApi } from "@/lib/api";
import { externalLinkClick } from "@/lib/externalAuth";
import { useChatGPTOAuth } from "@/hooks/useChatGPTOAuth";
import { ChatGPTPlanFirst, ChatGPTPlanGate } from "@/components/chat/ChatGPTPlanGate";
import {
  commitProviderChoice,
  providerCommitErrorMessage,
  readableProvider,
  type ByoProvider,
} from "@/lib/providerCommit";
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
 * ProvisioningStepper, which presents workspace checkout before entitlement —
 * this modal targets `/billing/provider` and assumes an already-active
 * subscription.
 *
 * UX flow:
 *   1. Pick a provider card (every offered path, for personal and org owners
 *      alike; for orgs the subscribing admin's ChatGPT account backs the
 *      workspace). Cutover warning is rendered at the top so it's visible
 *      BEFORE the user commits.
 *   2. If ChatGPT OAuth → render the device-code flow inline (reuses
 *      useChatGPTOAuth, which auto-fires /status + /start on mount).
 *      On completion we POST /billing/provider.
 *   3. If BYO key → collect provider + key, PUT /settings/keys, then
 *      POST /billing/provider.
 *   4. If managed inference → POST /billing/provider directly (credits gate is
 *      enforced at provisioning time by provision_gate).
 *
 * `initialChoice` skips step 1: LLMPanel's empty state renders the same three
 * choices inline, so a user arriving from there has already picked and the
 * modal opens straight on the credential stage. It only ever names a
 * credential-collecting path — managed inference needs no stage, so the panel commits
 * it inline without opening the modal at all.
 *
 * Errors from /billing/provider carry a `{code, message}` payload from
 * the backend (ChangeProviderError). We surface message verbatim — the
 * codes (`byo_key_missing`, `oauth_required`, etc.) are stable so a
 * future per-code UI affordance can be wired up if useful.
 */

import {
  providerSwitchCards,
  type ProviderChoiceId as ProviderChoice,
} from "@/lib/providerCards";

type Stage =
  | { kind: "picker" }
  | { kind: "chatgpt-oauth" }
  | { kind: "byo-key" }
  | { kind: "submitting"; choice: ProviderChoice; byo?: ByoProvider }
  | { kind: "error"; message: string };

/** Paths that need a credential stage — the only valid `initialChoice` values. */
export type CredentialProviderChoice = Exclude<ProviderChoice, "openrouter_managed">;

interface ChangeProviderModalProps {
  open: boolean;
  /**
   * The owner's stored provider_choice — a plain string, not `ProviderChoice`:
   * it can carry a legacy value (e.g. a retired `bedrock_claude` row) that is
   * no longer offered by the picker below. `isCurrent` simply won't match it
   * against any card, which is the correct "no current pick highlighted"
   * fallback rather than a crash.
   */
  currentProvider?: string | null;
  currentByoProvider?: ByoProvider | null;
  managedInferenceAvailable?: boolean;
  /** Open past the picker, on this choice's credential stage. */
  initialChoice?: CredentialProviderChoice | null;
  /** Just back from a Checkout started for ChatGPT: wait for the plan to land before offering it again. */
  waitForPlan?: boolean;
  onClose: () => void;
  onCommitted: () => void;
}

export function ChangeProviderModal({
  open,
  currentProvider,
  currentByoProvider,
  managedInferenceAvailable = false,
  initialChoice = null,
  waitForPlan = false,
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
      <DialogContent className="max-w-2xl" showCloseButton>
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
            managedInferenceAvailable={managedInferenceAvailable}
            initialChoice={initialChoice}
            waitForPlan={waitForPlan}
            onClose={onClose}
            onCommitted={onCommitted}
          />
        )}
      </DialogContent>
    </Dialog>
  );
}

interface BodyProps {
  currentProvider?: string | null;
  currentByoProvider?: ByoProvider | null;
  managedInferenceAvailable: boolean;
  initialChoice?: CredentialProviderChoice | null;
  waitForPlan?: boolean;
  onClose: () => void;
  onCommitted: () => void;
}

function ChangeProviderBody({
  currentProvider,
  currentByoProvider,
  managedInferenceAvailable,
  initialChoice = null,
  waitForPlan = false,
  onClose,
  onCommitted,
}: BodyProps) {
  const api = useApi();
  // The body remounts on every open (parent renders it behind `open &&`), so a
  // plain initializer is enough to honor `initialChoice` — no effect needed,
  // which keeps react-hooks/set-state-in-effect happy.
  const [stage, setStage] = useState<Stage>(() =>
    initialChoice === "chatgpt_oauth"
      ? { kind: "chatgpt-oauth" }
      : initialChoice === "byo_key"
        ? { kind: "byo-key" }
        : { kind: "picker" },
  );

  const commit = useCallback(
    async (choice: ProviderChoice, byo?: ByoProvider) => {
      setStage({ kind: "submitting", choice, byo });
      try {
        await commitProviderChoice(api.post, choice, byo);
        onCommitted();
        onClose();
      } catch (e: unknown) {
        setStage({ kind: "error", message: providerCommitErrorMessage(e) });
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
          Changing providers may restart your workspace runtime. Conversation
          history is preserved.
        </DialogDescription>
      </DialogHeader>

      {stage.kind === "picker" && (
        <PickerStage
          currentProvider={currentProvider}
          currentByoProvider={currentByoProvider}
          managedInferenceAvailable={managedInferenceAvailable}
          onPick={handlePick}
        />
      )}

      {/* The plan is checked before any sign-in starts: a free workspace sees the plan step, never a device code. */}
      {stage.kind === "chatgpt-oauth" && (
        <ChatGPTPlanGate waitForPlan={waitForPlan}>
          <ChatGPTOAuthStage
            onComplete={() => void commit("chatgpt_oauth")}
            onCancel={() => setStage({ kind: "picker" })}
          />
        </ChatGPTPlanGate>
      )}

      {stage.kind === "byo-key" && (
        <ByoKeyStage
          currentByoProvider={currentByoProvider}
          onComplete={(byo) => void commit("byo_key", byo)}
          onCancel={() => setStage({ kind: "picker" })}
        />
      )}

      {stage.kind === "submitting" && (
        <div className="flex items-center justify-center gap-3 py-12 text-sm text-[var(--midnight-muted)]">
          <Loader2 size={16} className="animate-spin" />
          Switching to {readableProvider(stage.choice, stage.byo)}…
        </div>
      )}

      {stage.kind === "error" && (
        <div className="space-y-4 py-6">
          <p className="text-sm text-[var(--err)]">{stage.message}</p>
          <DialogFooter>
            <Button
              variant="outline"
              className="border-[var(--edge)] bg-transparent text-[var(--midnight)] hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
              onClick={() => setStage({ kind: "picker" })}
            >
              Back
            </Button>
          </DialogFooter>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Picker stage
// ---------------------------------------------------------------------------

interface PickerStageProps {
  currentProvider?: string | null;
  currentByoProvider?: ByoProvider | null;
  managedInferenceAvailable: boolean;
  onPick: (choice: ProviderChoice) => void;
}

function PickerStage({ currentProvider, currentByoProvider, managedInferenceAvailable, onPick }: PickerStageProps) {
  const isCurrent = (id: ProviderChoice) => {
    if (id !== currentProvider) return false;
    // BYO is "current" only if the user is on byo_key today; sub-provider
    // collected in the BYO stage. Marking byo_key "current" still lets the
    // user re-open BYO to rotate keys or swap openai↔anthropic.
    return true;
  };

  const cards = providerSwitchCards(managedInferenceAvailable);
  // Tailwind's `grid-cols-N` reserves N equal tracks regardless of item
  // count — with bedrock_claude gone, the base (unmanaged) picker is down
  // to two cards, so a hardcoded 3-column grid would leave a blank track.
  return (
    <div className={`grid grid-cols-1 gap-3 py-2 ${cards.length >= 3 ? "md:grid-cols-3" : "md:grid-cols-2"}`}>
      {cards.map((card) => {
        const current = isCurrent(card.id);
        return (
          <button
            key={card.id}
            type="button"
            onClick={() => onPick(card.id)}
            className={
              "rounded-xl border p-4 text-left transition-colors " +
              (current
                ? "border-ok bg-cream"
                : "border-[var(--edge)] bg-cream hover:bg-[var(--cream-deep)]")
            }
          >
            <div className="flex items-start justify-between gap-2">
              <div>
                <h3 className="font-medium text-[var(--midnight)]">{card.title}</h3>
                <p className="mt-1 text-xs text-[var(--midnight-muted)]">{card.subtitle}</p>
              </div>
              {current && (
                <span className="rounded-full border border-ok px-2 py-0.5 text-2xs font-medium text-ok">
                  Current
                </span>
              )}
            </div>
            {card.id === "byo_key" && current && currentByoProvider && (
              <p className="mt-2 text-caption text-[var(--midnight-muted)]">
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
        <div className="flex flex-col items-center gap-3 py-6 text-sm text-[var(--midnight-muted)]">
          <Loader2 size={20} className="animate-spin" />
          Starting ChatGPT sign-in…
        </div>
      )}

      {state.status === "pending" && (
        <ol className="list-decimal space-y-3 pl-5 text-sm text-[var(--midnight)] marker:text-[var(--midnight-muted)]">
          <li>
            Open{" "}
            <a
              href={state.verificationUri}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => externalLinkClick(e, state.verificationUri)}
              className="text-[var(--midnight)] underline underline-offset-2 hover:text-[var(--midnight)]"
            >
              {state.verificationUri}
            </a>{" "}
            and sign in to your ChatGPT account.
          </li>
          <li>
            Enter this code:
            <div className="mt-2">
              <code className="rounded-sm bg-[var(--cream-deep)] px-3 py-2 font-mono text-title tracking-caps text-[var(--midnight)]">
                {state.userCode}
              </code>
            </div>
          </li>
          <li className="text-xs text-[var(--midnight-muted)]">
            We&apos;ll switch you over automatically once you finish sign-in.
          </li>
        </ol>
      )}

      {state.status === "plan_required" && <ChatGPTPlanFirst />}

      {state.status === "error" && (
        <div className="space-y-3 py-4">
          <p className="text-sm text-[var(--err)]">Connection failed: {state.message}</p>
          <Button
            variant="outline"
            className="border-[var(--edge)] bg-transparent text-[var(--midnight)] hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
            onClick={start}
          >
            Try again
          </Button>
        </div>
      )}

      {state.status === "completed" && (
        <div className="flex items-center justify-center py-6 text-sm text-[var(--midnight)]">
          ✓ Connected — committing switch…
        </div>
      )}

      {/* Once sign-in completes the switch is committing — hide Back so the
          user can't bail into a half-committed state (the tokens are already
          stored; cancelling here would only confuse, not undo). */}
      {state.status !== "completed" && (
        <DialogFooter>
          <Button
            variant="ghost"
            className="text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
            onClick={onCancel}
          >
            Back
          </Button>
        </DialogFooter>
      )}
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
        <label className="flex items-center gap-2 text-sm text-[var(--midnight)]">
          <input
            type="radio"
            value="openai"
            checked={provider === "openai"}
            onChange={() => setProvider("openai")}
            className="accent-[var(--midnight)]"
          />
          OpenAI
        </label>
        <label className="flex items-center gap-2 text-sm text-[var(--midnight)]">
          <input
            type="radio"
            value="anthropic"
            checked={provider === "anthropic"}
            onChange={() => setProvider("anthropic")}
            className="accent-[var(--midnight)]"
          />
          Anthropic
        </label>
      </fieldset>

      <input aria-label="API key"
        type="password"
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        placeholder={provider === "openai" ? "sk-proj-…" : "sk-ant-…"}
        required
        autoComplete="off"
        spellCheck={false}
        className="w-full rounded-md border border-[var(--edge)] bg-cream-deep px-3 py-2 font-mono text-sm text-[var(--midnight)] placeholder:text-[var(--midnight-muted)]"
      />

      <p className="text-xs text-[var(--midnight-muted)]">
        Stored encrypted. We validate the key against the
        provider before saving — invalid keys are rejected here, not at chat time.
      </p>

      {error && <p className="text-sm text-[var(--err)]">{error}</p>}

      <DialogFooter>
        <Button
          type="button"
          variant="ghost"
          className="text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
          onClick={onCancel}
          disabled={submitting}
        >
          Back
        </Button>
        <Button type="submit" disabled={submitting || !apiKey}>
          {submitting ? "Validating…" : "Save and switch"}
        </Button>
      </DialogFooter>
    </form>
  );
}
