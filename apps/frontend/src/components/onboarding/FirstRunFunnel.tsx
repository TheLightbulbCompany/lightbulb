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
/** "noIdea" = they clicked "I don't know yet — you pick": submit with no input. */
type Mode = "idea" | "noIdea";
const RETRY_MS = 3000;
const MAX_RETRIES = 5;

// Brand-primary recipe used across the app (see ProviderPicker / landing
// .ld-btn): forest fill (#06402B), cream text, pill, hover #0a5138.
const CHOICE_BTN =
  "w-full rounded-full bg-[#06402B] hover:bg-[#0a5138] text-[#faf7f2] py-4 text-sm font-semibold tracking-wide transition-colors";
const INPUT =
  "w-full rounded-xl border border-[#e0dbd0] bg-white p-3 text-sm text-[#1a1a1a] placeholder:text-[#b5af9f] focus:outline-none focus:border-[#06402B]";
// Outline, not filled: this is the escape hatch for the undecided, and it
// should not compete with Continue for the user who already has an idea.
const SECONDARY_BTN =
  "w-full rounded-full border border-[#d6d0c3] bg-transparent hover:bg-[#f1ede4] text-[#1a1a1a] py-3 text-sm font-medium tracking-wide transition-colors disabled:opacity-50";

export function FirstRunFunnel() {
  const { user } = useUser();
  const api = useApi();
  const [path, setPath] = React.useState<Path | null>(null);
  const [input, setInput] = React.useState("");
  const [detail, setDetail] = React.useState("");
  const [state, setState] = React.useState<"idle" | "submitting" | "waiting" | "error">("idle");
  // Which submission the user made, so Retry replays the same one. Passed
  // explicitly into submit() rather than read from here — this state only
  // exists to survive the round trip to the Retry button.
  const [mode, setMode] = React.useState<Mode>("idea");

  const submit = React.useCallback(
    async (submitMode: Mode) => {
      const noIdea = submitMode === "noIdea";
      setMode(submitMode);
      if (!user) return;
      if (!noIdea && input.trim().length < 3) return;
      setState("submitting");
      for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
        try {
          // No input at all on the "you pick" button: the absent idea IS the
          // signal, and BRIEF.md tells Friday to choose one from what she can
          // find out about them. Sending "" instead would be a brief quoting an
          // empty string back at her.
          const resp = await api.post("/onboarding/brief", {
            path,
            input: noIdea ? undefined : input.trim(),
            detail: !noIdea && path === "new" && detail.trim() ? detail.trim() : undefined,
          });
          // The backend `_ctx` dependency can 202 while Paperclip is still
          // provisioning — the api client treats any 2xx as success, so we
          // check the body to tell "staged" apart from "not really". Anything
          // else (including that 202 shape) is retried via the same loop.
          const status = (resp as { status?: string } | null)?.status;
          if (status === "staged" || status === "already_submitted") {
            await writeFirstRun(user, { briefSubmitted: true, path: path ?? undefined });
            capture("first_run_brief_submitted", { path, noIdea });
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
    },
    [api, user, path, input, detail],
  );

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
            <div className="space-y-3">
              <button
                className={`${CHOICE_BTN} disabled:opacity-50`}
                disabled={busy || input.trim().length < 3}
                onClick={() => submit("idea")}
              >
                {state === "waiting" && mode === "idea" ? "Setting things up…" : "Continue →"}
              </button>
              {path === "new" && (
                <div className="space-y-1.5 pt-1">
                  <button
                    className={SECONDARY_BTN}
                    disabled={busy}
                    onClick={() => submit("noIdea")}
                  >
                    {busy && mode === "noIdea"
                      ? "Setting things up…"
                      : "I don’t know yet — you pick"}
                  </button>
                  <p className="text-xs italic text-center text-[#8a8578]">
                    Friday will look you up and choose something
                  </p>
                </div>
              )}
            </div>
            {state === "error" && (
              <p className="text-sm text-[#b32424]">
                Couldn&apos;t save that just yet.{" "}
                <button className="underline" onClick={() => submit(mode)}>
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
