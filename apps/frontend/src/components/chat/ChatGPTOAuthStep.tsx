"use client";
import { useEffect } from "react";
import { useChatGPTOAuth } from "@/hooks/useChatGPTOAuth";

type Props = {
  onComplete: () => void;
};

export function ChatGPTOAuthStep({ onComplete }: Props) {
  // The hook owns the on-mount status probe + auto-start. If tokens are
  // already on file (e.g. user completed in another tab, or the wizard
  // re-mounted between /poll and the next gate refresh), the hook lands
  // in {status: "completed"} immediately and the useEffect below fires
  // onComplete; we never render the spinner branch in that case.
  const { state, start } = useChatGPTOAuth();

  useEffect(() => {
    if (state.status === "completed") onComplete();
  }, [state, onComplete]);

  if (state.status === "pending") {
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
            <div className="mt-2">
              <code className="bg-muted px-3 py-2 rounded font-mono text-lg tracking-widest">
                {state.userCode}
              </code>
            </div>
          </li>
        </ol>
        <p className="text-xs text-muted-foreground">
          Waiting for you to complete sign-in&hellip;
        </p>
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
          className="rounded-full bg-[#06402B] hover:bg-[#0a5c3e] text-white px-6 py-3 font-medium"
        >
          Try again
        </button>
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
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-muted-foreground/20 border-t-[#06402B]" />
      <p className="text-sm text-muted-foreground">Starting ChatGPT sign-in&hellip;</p>
    </div>
  );
}
