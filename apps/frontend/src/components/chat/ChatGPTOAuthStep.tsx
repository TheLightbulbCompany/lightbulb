"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useChatGPTOAuth, type OAuthState } from "@/hooks/useChatGPTOAuth";
import { ChatGPTPlanFirst, ChatGPTPlanGate } from "./ChatGPTPlanGate";
import { replaceSearchParams } from "@/lib/shellNav";
import { externalLinkClick } from "@/lib/externalAuth";

type Props = {
  onComplete: () => void | Promise<void>;
  onCancel?: () => void;
};

/** Device-code expiry countdown as M:SS (floored at 0:00). */
function formatRemaining(ms: number): string {
  const totalSeconds = Math.max(0, Math.ceil(ms / 1000));
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** The open-URL / enter-code / countdown panel for the device-code sign-in. */
function DeviceCodePanel({
  state,
  heading,
  onRestart,
  footer,
}: {
  state: Extract<OAuthState, { status: "pending" }>;
  heading: string;
  onRestart: () => void;
  footer: React.ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  // Reset the "Copied" affordance after a beat. In an effect (not an inline
  // setTimeout) so it is cleared on unmount and never fires a state update on
  // a torn-down component.
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(t);
  }, [copied]);

  const copyCode = useCallback(async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
    } catch {
      // Clipboard blocked (permissions / insecure context) — the code is
      // still on screen for manual entry, so fail silently.
    }
  }, []);

  const remainingMs = state.expiresAt - now;
  const expired = remainingMs <= 0;
  return (
    <div className="flex flex-col items-center gap-4 py-8 max-w-md">
      <h3 className="text-xl font-semibold">{heading}</h3>
      <ol className="text-sm space-y-3 list-decimal list-inside">
        <li>
          Open{" "}
          <a
            href={state.verificationUri}
            target="_blank"
            rel="noreferrer"
            onClick={(e) => externalLinkClick(e, state.verificationUri)}
            className="text-primary underline"
          >
            {state.verificationUri}
          </a>{" "}
          and sign in to your ChatGPT account.
        </li>
        <li>
          Enter this code:
          <div className="mt-2 flex items-center gap-2">
            <code className="bg-muted px-3 py-2 rounded-sm font-mono text-lg tracking-caps">
              {state.userCode}
            </code>
            <button
              type="button"
              onClick={() => copyCode(state.userCode)}
              aria-label="Copy code"
              className="rounded-sm border border-muted-foreground/20 px-2 py-1 text-xs hover:bg-muted"
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        </li>
      </ol>
      {expired ? (
        <div className="flex flex-col items-center gap-2">
          <p className="text-xs text-destructive">
            This code expired. Get a new one to finish signing in.
          </p>
          <button
            onClick={onRestart}
            className="rounded-full bg-[var(--midnight)] hover:bg-[var(--midnight-soft)] text-cream px-5 py-2 text-sm font-medium"
          >
            Get a new code
          </button>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">
          Waiting for you to complete sign-in&hellip; code expires in{" "}
          {formatRemaining(remainingMs)}
        </p>
      )}
      {footer}
    </div>
  );
}

/** The onboarding ChatGPT step: the plan is checked before sign-in starts (a free signup that picked ChatGPT goes
 *  plan → Checkout → back here), then the device-code flow. */
export function ChatGPTOAuthStep(props: Props) {
  return <ChatGPTPlanGate then="chatgpt_onboarding"><ChatGPTOAuthSignIn {...props} /></ChatGPTPlanGate>;
}

function ChatGPTOAuthSignIn({ onComplete, onCancel }: Props) {
  // The hook owns the on-mount status probe + auto-start. If tokens are
  // already on file (e.g. user completed in another tab, or the wizard
  // re-mounted between /poll and the next gate refresh), the hook lands
  // in {status: "completed"} immediately.
  const { state, start } = useChatGPTOAuth();
  const searchParams = useSearchParams();
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const savingRef = useRef(false);
  const finishedRef = useRef(false);

  const finish = useCallback(async () => {
    if (savingRef.current || finishedRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    try {
      await onComplete();
      finishedRef.current = true;
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "Could not save your provider choice.");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }, [onComplete]);

  useEffect(() => {
    if (state.status === "completed") void finish();
  }, [state.status, finish]);

  // Embedded callers own the flow state and must unmount this step to stop polling.
  // Standalone onboarding still exits through ?provider=, keeping checkout and other params.
  const cancel = useCallback(() => {
    if (onCancel) {
      onCancel();
      return;
    }
    const params = new URLSearchParams(searchParams.toString());
    params.delete("provider");
    replaceSearchParams(params);
  }, [onCancel, searchParams]);

  const cancelControl = (
    <button
      type="button"
      onClick={cancel}
      className="text-xs text-muted-foreground underline hover:text-foreground"
    >
      Cancel and choose a different provider
    </button>
  );

  if (state.status === "pending") {
    return (
      <DeviceCodePanel
        state={state}
        heading="Almost there"
        onRestart={start}
        footer={cancelControl}
      />
    );
  }

  // /start refused 402 although billing read as paid (a stale billing cache, or the plan lapsed in between).
  if (state.status === "plan_required") return <ChatGPTPlanFirst then="chatgpt_onboarding" />;

  if (state.status === "error") {
    return (
      <div className="flex flex-col items-center gap-4 py-8">
        <p className="text-destructive">
          Connection failed: {state.message}
        </p>
        <button
          onClick={start}
          className="rounded-full bg-[var(--midnight)] hover:bg-[var(--midnight-soft)] text-cream px-6 py-3 font-medium"
        >
          Try again
        </button>
        {cancelControl}
      </div>
    );
  }

  if (state.status === "completed") {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-8 text-primary">
        <p>{saving ? "Saving your provider choice…" : "ChatGPT connected"}</p>
        {saveError && <p role="alert" className="text-sm text-destructive">{saveError}</p>}
        {saveError && <button type="button" onClick={() => void finish()} className="rounded-full border px-4 py-2 text-sm">Retry</button>}
      </div>
    );
  }

  // status === "idle" — auto-start hasn't fired yet (one render frame).
  // Render a neutral spinner instead of the deleted "Connect ChatGPT"
  // button so we don't ask the user for a click that's about to be
  // taken automatically.
  return (
    <div className="flex flex-col items-center gap-3 py-8">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-muted-foreground/20 border-t-[var(--midnight)]" />
      <p className="text-sm text-muted-foreground">Starting ChatGPT sign-in&hellip;</p>
    </div>
  );
}
