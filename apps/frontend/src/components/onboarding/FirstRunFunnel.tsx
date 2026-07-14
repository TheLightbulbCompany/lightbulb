"use client";

/**
 * Two-path onboarding funnel shown while the trial container provisions
 * (spec §3). Submitting stages BRIEF.md server-side; the firstRun metadata
 * write flips ProvisioningStepper's branch off, falling through to the
 * cold-start card. It is the committed onboarding front door — there is no
 * skip, so submitting the brief is the only way past it.
 */
import * as React from "react";
import { useUser } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";
import { writeFirstRun } from "@/lib/firstRun";

type Path = "new" | "grow";
const RETRY_MS = 3000;
const MAX_RETRIES = 5;

// Brand-primary recipe used across the app (see ProviderPicker / landing
// .ld-btn): forest fill (#06402B), cream text, pill, hover #0a5138.
const CHOICE_BTN =
  "w-full rounded-full bg-[#06402B] hover:bg-[#0a5138] text-[#faf7f2] py-4 text-sm font-semibold tracking-wide transition-colors";
const INPUT =
  "w-full rounded-xl border border-[#e0dbd0] bg-white p-3 text-sm text-[#1a1a1a] placeholder:text-[#b5af9f] focus:outline-none focus:border-[#06402B]";

export function FirstRunFunnel() {
  const { user } = useUser();
  const api = useApi();
  const [path, setPath] = React.useState<Path | null>(null);
  const [input, setInput] = React.useState("");
  const [detail, setDetail] = React.useState("");
  const [state, setState] = React.useState<"idle" | "submitting" | "waiting" | "error">("idle");

  const submit = React.useCallback(async () => {
    if (!user || input.trim().length < 3) return;
    setState("submitting");
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      try {
        const resp = await api.post("/onboarding/brief", {
          path,
          input: input.trim(),
          detail: path === "new" && detail.trim() ? detail.trim() : undefined,
        });
        // The backend `_ctx` dependency can 202 while Paperclip is still
        // provisioning — the api client treats any 2xx as success, so we
        // check the body to tell "staged" apart from "not really". Anything
        // else (including that 202 shape) is retried via the same loop.
        const status = (resp as { status?: string } | null)?.status;
        if (status === "staged" || status === "already_submitted") {
          await writeFirstRun(user, { briefSubmitted: true, path: path ?? undefined });
          capture("first_run_brief_submitted", { path });
          return;
        }
        if (attempt < MAX_RETRIES) {
          setState("waiting");
          await new Promise((r) => setTimeout(r, RETRY_MS));
          continue;
        }
        setState("error");
        return;
      } catch (err) {
        const httpStatus = (err as { status?: number }).status;
        if (httpStatus === 409 && attempt < MAX_RETRIES) {
          setState("waiting");
          await new Promise((r) => setTimeout(r, RETRY_MS));
          continue;
        }
        setState("error");
        return;
      }
    }
  }, [api, user, path, input, detail]);

  const busy = state === "submitting" || state === "waiting";

  return (
    <div className="flex-1 flex items-center justify-center p-6 bg-[#faf7f2]">
      <div className="w-full max-w-md space-y-8">
        {path === null ? (
          <div className="space-y-8 text-center">
            <div className="space-y-2">
              <h1 className="text-3xl font-semibold tracking-tight text-[#1a1a1a] font-lora">
                Let&apos;s get started.
              </h1>
              <p className="text-sm text-[#8a8578]">
                Tell your CEO about your business — she&apos;ll research it and lay out your next moves.
              </p>
            </div>
            <div className="space-y-4">
              <div className="space-y-1.5">
                <button className={CHOICE_BTN} onClick={() => setPath("new")}>
                  Start something new
                </button>
                <p className="text-xs italic text-[#8a8578]">Start from scratch</p>
              </div>
              <div className="space-y-1.5">
                <button className={CHOICE_BTN} onClick={() => setPath("grow")}>
                  Grow my business
                </button>
                <p className="text-xs italic text-[#8a8578]">Already have a business</p>
              </div>
            </div>
          </div>
        ) : (
          <div className="space-y-6">
            <button
              className="text-xs text-[#8a8578] hover:text-[#1a1a1a] transition-colors"
              onClick={() => setPath(null)}
            >
              ← Back
            </button>
            <h1 className="text-2xl font-semibold tracking-tight text-[#1a1a1a] font-lora">
              {path === "new" ? "What's your idea?" : "What's your company's website?"}
            </h1>
            {path === "new" ? (
              <div className="space-y-3">
                <textarea
                  className={`${INPUT} min-h-28`}
                  placeholder="e.g. A social media agency for small restaurants"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                />
                <input
                  className={INPUT}
                  placeholder="Who's it for? (optional)"
                  value={detail}
                  onChange={(e) => setDetail(e.target.value)}
                />
              </div>
            ) : (
              <input
                className={INPUT}
                placeholder="yourcompany.com"
                value={input}
                onChange={(e) => setInput(e.target.value)}
              />
            )}
            <button
              className={`${CHOICE_BTN} disabled:opacity-50`}
              disabled={busy || input.trim().length < 3}
              onClick={submit}
            >
              {state === "waiting" ? "Setting things up…" : "Continue →"}
            </button>
            {state === "error" && (
              <p className="text-sm text-[#b32424]">
                Couldn&apos;t save that just yet.{" "}
                <button className="underline" onClick={submit}>
                  Retry
                </button>
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
