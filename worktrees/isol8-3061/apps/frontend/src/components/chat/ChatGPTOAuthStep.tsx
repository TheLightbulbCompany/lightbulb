"use client";
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useChatGPTOAuth, type OAuthState } from "@/hooks/useChatGPTOAuth";
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

/** The open-URL / enter-code / countdown panel, shared by the provider
 * sign-in and the harness grant — both are the same device-code shape. */
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
  // The coding-harness grant: a SECOND device-code approval on the same
  // ChatGPT account, chained right after the provider connect while the
  // user is still signed in — so it reads as one flow, not a second
  // sign-in. autoStart:false — it begins only on the user's click; the
  // offer is skippable and provisioning is never blocked on it (the
  // container provision runs server-side off the provider tokens alone).
  const harness = useChatGPTOAuth({
    basePath: "/oauth/chatgpt/harness",
    autoStart: false,
  });
  const [harnessSkipped, setHarnessSkipped] = useState(false);
  const searchParams = useSearchParams();

  // Advance out of the step once the provider is connected AND the harness
  // offer is resolved: already-connected (re-entry), just-connected, or
  // skipped. harness.probed gates the decision so a re-entering user whose
  // harness is already connected auto-advances exactly like before the
  // offer existed.
  useEffect(() => {
    if (state.status !== "completed") return;
    if (!harness.probed) return;
    if (harness.state.status === "completed" || harnessSkipped) onComplete();
  }, [state.status, harness.probed, harness.state.status, harnessSkipped, onComplete]);

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

  const skipControl = (
    <button
      type="button"
      onClick={() => setHarnessSkipped(true)}
      className="text-xs text-muted-foreground underline hover:text-foreground"
    >
      Skip for now
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
    // Provider connected. Offer the coding-harness grant unless it's
    // already connected / just skipped (the advance effect above handles
    // those). While the harness status probe is in flight, hold a spinner
    // for the one beat it takes — advancing early would flash the offer
    // at users who already have the harness.
    if (!harness.probed || harness.state.status === "completed" || harnessSkipped) {
      return (
        <div className="flex items-center justify-center py-8 text-primary">
          &#10003; Connected
        </div>
      );
    }
    if (harness.state.status === "pending") {
      return (
        <DeviceCodePanel
          state={harness.state}
          heading="One more code to enable coding"
          onRestart={harness.start}
          footer={skipControl}
        />
      );
    }
    return (
      <div className="flex flex-col items-center gap-4 py-8 max-w-md text-center">
        <p className="text-primary">&#10003; ChatGPT connected</p>
        <h3 className="text-xl font-semibold">Enable coding for your agents?</h3>
        <p className="text-sm text-muted-foreground">
          Your agents can delegate real coding work to OpenCode, powered by
          this same ChatGPT subscription. It takes one more approval &mdash;
          you&apos;re already signed in, no password needed. The harness keeps
          its own credential, so it never interferes with your main connection.
        </p>
        {harness.state.status === "error" && (
          <p className="text-sm text-destructive">{harness.state.message}</p>
        )}
        <button
          onClick={() => void harness.start()}
          className="rounded-full bg-[var(--midnight)] hover:bg-[var(--midnight-soft)] text-cream px-6 py-3 font-medium"
        >
          {harness.state.status === "error" ? "Try again" : "Enable coding"}
        </button>
        {skipControl}
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
