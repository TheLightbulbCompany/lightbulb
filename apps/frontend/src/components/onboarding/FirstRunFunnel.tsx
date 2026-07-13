"use client";

/**
 * Two-path onboarding funnel shown while the trial container provisions
 * (spec §3). Submitting stages BRIEF.md server-side; the firstRun
 * metadata write flips ProvisioningStepper's branch off, falling through
 * to the cold-start card. Skippable — skipping falls back to today's flow.
 */
import * as React from "react";
import { useUser } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { capture } from "@/lib/analytics";
import { writeFirstRun } from "@/lib/firstRun";

type Path = "new" | "grow";
const RETRY_MS = 3000;
const MAX_RETRIES = 5;

export function FirstRunFunnel() {
  const { user } = useUser();
  const api = useApi();
  const [path, setPath] = React.useState<Path | null>(null);
  const [input, setInput] = React.useState("");
  const [detail, setDetail] = React.useState("");
  const [state, setState] = React.useState<"idle" | "submitting" | "waiting" | "error">("idle");
  const cancelledRef = React.useRef(false);

  const submit = React.useCallback(async () => {
    if (!user || input.trim().length < 3) return;
    cancelledRef.current = false;
    setState("submitting");
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      if (cancelledRef.current) return;
      try {
        const resp = await api.post("/onboarding/brief", {
          path,
          input: input.trim(),
          detail: path === "new" && detail.trim() ? detail.trim() : undefined,
        });
        // The backend `_ctx` dependency can 202 while Paperclip is still
        // provisioning — the api client treats any 2xx as success, so we
        // must check the body to tell "staged" apart from "not really".
        // Anything else (including that 202 shape) is treated exactly like
        // a 409: retry via the same loop, no second retry path.
        const status = (resp as { status?: string } | null)?.status;
        const ok = status === "staged" || status === "already_submitted";
        if (cancelledRef.current) return;
        if (ok) {
          await writeFirstRun(user, { briefSubmitted: true, path: path ?? undefined });
          if (cancelledRef.current) return;
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

  const skip = React.useCallback(async () => {
    if (!user) return;
    cancelledRef.current = true;
    await writeFirstRun(user, { skipped: true });
    capture("first_run_brief_skipped");
  }, [user]);

  return (
    <div className="flex-1 flex items-center justify-center p-6 bg-[#faf7f2]">
      <div className="w-full max-w-lg space-y-6">
        {path === null ? (
          <>
            <h1 className="text-3xl font-semibold text-center">Let&apos;s get started.</h1>
            <button
              className="w-full rounded-lg bg-black text-white py-4 text-sm tracking-wide"
              onClick={() => setPath("new")}
            >
              Start something new
            </button>
            <button
              className="w-full rounded-lg bg-black text-white py-4 text-sm tracking-wide"
              onClick={() => setPath("grow")}
            >
              Grow my business
            </button>
          </>
        ) : (
          <>
            <button className="text-xs text-neutral-500" onClick={() => setPath(null)}>
              ← Back
            </button>
            <h1 className="text-3xl font-semibold">
              {path === "new" ? "What's your idea?" : "What's your company's website?"}
            </h1>
            {path === "new" ? (
              <>
                <textarea
                  className="w-full border rounded-lg p-3 min-h-28"
                  placeholder="e.g. A social media agency for small restaurants"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                />
                <input
                  className="w-full border rounded-lg p-3"
                  placeholder="Who's it for? (optional)"
                  value={detail}
                  onChange={(e) => setDetail(e.target.value)}
                />
              </>
            ) : (
              <input
                className="w-full border rounded-lg p-3"
                placeholder="yourcompany.com"
                value={input}
                onChange={(e) => setInput(e.target.value)}
              />
            )}
            <button
              className="w-full rounded-lg bg-black text-white py-4 text-sm tracking-wide disabled:opacity-50"
              disabled={state === "submitting" || state === "waiting" || input.trim().length < 3}
              onClick={submit}
            >
              {state === "waiting" ? "Setting things up…" : "Continue →"}
            </button>
            {state === "error" && (
              <p className="text-sm text-red-600">
                Couldn&apos;t save that just yet.{" "}
                <button className="underline" onClick={submit}>
                  Retry
                </button>
              </p>
            )}
            <button
              className="block mx-auto text-xs text-neutral-400 disabled:opacity-50"
              disabled={state === "submitting" || state === "waiting"}
              onClick={skip}
            >
              Skip for now
            </button>
          </>
        )}
      </div>
    </div>
  );
}
