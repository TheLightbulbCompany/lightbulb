"use client";
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useChatGPTOAuth, type OAuthState } from "@/hooks/useChatGPTOAuth";
import { ChatGPTPlanFirst } from "./ChatGPTPlanGate";
import { replaceSearchParams } from "@/lib/shellNav";
import { externalLinkClick } from "@/lib/externalAuth";

type Props = {
  onComplete: () => void;
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

export function ChatGPTOAuthStep({ onComplete }: Props) {
  // The hook owns the on-mount status probe + auto-start. If tokens are
  // already on file (e.g. user completed in another tab, or the wizard
  // re-mounted between /poll and the next gate refresh), the hook lands
  // in {status: "completed"} immediately.
  const { state, start } = useChatGPTOAuth();
  const searchParams = useSearchParams();

  useEffect(() => {
    if (state.status === "completed") onComplete();
  }, [state.status, onComplete]);

  // The stepper's only "go back" mechanism is the `?provider=` URL param:
  // stripping it exits the provider step and re-enters the main flow (the
  // provider picker for a not-yet-subscribed owner, normal provisioning
  // otherwise). Mirrors CreditsStep.chooseDifferentProvider so other params
  // (e.g. checkout=success) survive. Re-entering the OAuth step 409s on an
  // already-active device session, so the escape hatch matters.
  const cancel = useCallback(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.delete("provider");
    replaceSearchParams(params);
  }, [searchParams]);

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

  // /start refused: no paid plan (the onboarding wizard reaches here after Checkout, so this is a safety net).
  if (state.status === "plan_required") return <ChatGPTPlanFirst />;

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
      <div className="flex items-center justify-center py-8 text-primary">
        &#10003; Connected
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
