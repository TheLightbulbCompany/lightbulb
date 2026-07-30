"use client";
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useChatGPTOAuth } from "@/hooks/useChatGPTOAuth";
import { replaceSearchParams } from "@/lib/shellNav";

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

export function ChatGPTOAuthStep({ onComplete }: Props) {
  // The hook owns the on-mount status probe + auto-start. If tokens are
  // already on file (e.g. user completed in another tab, or the wizard
  // re-mounted between /poll and the next gate refresh), the hook lands
  // in {status: "completed"} immediately and the useEffect below fires
  // onComplete; we never render the spinner branch in that case.
  const { state, start } = useChatGPTOAuth();
  const searchParams = useSearchParams();
  const [copied, setCopied] = useState(false);
  // Ticks once a second while a code is pending so the expiry countdown stays
  // live and flips to the "expired" affordance on its own.
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (state.status === "completed") onComplete();
  }, [state, onComplete]);

  useEffect(() => {
    if (state.status !== "pending") return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [state.status]);

  // Reset the "Copied" affordance after a beat. In an effect (not an inline
  // setTimeout) so it is cleared on unmount and never fires a state update on
  // a torn-down component.
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(t);
  }, [copied]);

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

  const copyCode = useCallback(async (code: string) => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
    } catch {
      // Clipboard blocked (permissions / insecure context) — the code is
      // still on screen for manual entry, so fail silently.
    }
  }, []);

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
    const remainingMs = state.expiresAt - now;
    const expired = remainingMs <= 0;
    return (
      <div className="flex flex-col items-center gap-4 py-8 max-w-md">
        <h3 className="text-xl font-semibold">Almost there</h3>
        <ol className="text-sm space-y-3 list-decimal list-inside">
          <li>
            Open{" "}
            <a
              href={state.verificationUri}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline"
            >
              {state.verificationUri}
            </a>{" "}
            and sign in to your ChatGPT account.
          </li>
          <li>
            Enter this code:
            <div className="mt-2 flex items-center gap-2">
              <code className="bg-muted px-3 py-2 rounded font-mono text-lg tracking-widest">
                {state.userCode}
              </code>
              <button
                type="button"
                onClick={() => copyCode(state.userCode)}
                aria-label="Copy code"
                className="rounded border border-muted-foreground/20 px-2 py-1 text-xs hover:bg-muted"
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
              onClick={start}
              className="rounded-full bg-[var(--forest)] hover:bg-[#0a5c3e] text-white px-5 py-2 text-sm font-medium"
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
        {cancelControl}
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="flex flex-col items-center gap-4 py-8">
        <p className="text-destructive">
          Connection failed: {state.message}
        </p>
        <button
          onClick={start}
          className="rounded-full bg-[var(--forest)] hover:bg-[#0a5c3e] text-white px-6 py-3 font-medium"
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
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-muted-foreground/20 border-t-[var(--forest)]" />
      <p className="text-sm text-muted-foreground">Starting ChatGPT sign-in&hellip;</p>
    </div>
  );
}
