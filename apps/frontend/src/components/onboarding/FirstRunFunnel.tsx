"use client";

/**
 * Two-path onboarding funnel shown to a first-time owner or org member
 * (trial_local free-tier signup or organization) while their company's
 * workspace provisions (spec §3). Submitting stages BRIEF.md server-side;
 * the firstRun metadata write flips ProvisioningStepper's branch off,
 * falling through to the cold-start card. It is the committed onboarding
 * front door — there is no skip, so submitting the brief is the only way
 * past it.
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
// After this many CONSECUTIVE failed submits carrying the same upstream
// error, the generic "Couldn't save that just yet. Retry" line — which
// implies a transient problem — gives way to a persistent error state that
// shows the upstream detail and points at support. A different error (or a
// success) resets the streak.
const PERSISTENT_FAILURE_THRESHOLD = 3;

// The funnel does NOT control its own lifetime. ProvisioningStepper renders
// it as one branch of a machine driven by /container/status, so anything that
// makes a single poll momentarily unreadable swaps it for a loader — which
// unmounts this component and discards its useState. That used to throw away
// whatever the user had typed and drop them back on the two path buttons.
//
// The poll-blip case is fixed at its root in `useOnboardingStatus` (a failed
// poll no longer clobbers the last good status), but the funnel is the front
// door of the product and must not depend on that being the only way it can
// ever be remounted. So the in-progress draft is persisted.
//
// sessionStorage, not localStorage: a draft is worth surviving a remount or a
// reload, NOT worth resurrecting in a brand-new tab days later. Owner-scoped
// on the same reasoning as ProvisioningStepper's activation markers — a
// shared browser must not hand one account's draft to the next.
const DRAFT_STORAGE_KEY = "isol8_first_run_draft_v1";

interface Draft {
  path: Path | null;
  input: string;
  detail: string;
}

const EMPTY_DRAFT: Draft = { path: null, input: "", detail: "" };

/** Best-effort draft read. Every field is re-validated rather than trusted:
 * this is user-writable storage, and a malformed entry must degrade to a
 * clean funnel, never to a crashed one. */
function readDraft(key: string): Draft {
  // This one runs inside a lazy `useState` initializer, which DOES execute
  // during SSR — unlike the persist effect below, which is client-only by
  // construction. Safe today only because ProvisioningStepper gates the
  // funnel behind a completed client-side fetch; guarded here so that
  // safety doesn't depend on a caller three files away.
  if (typeof window === "undefined") return EMPTY_DRAFT;
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return EMPTY_DRAFT;
    const parsed = JSON.parse(raw) as Partial<Draft>;
    return {
      path: parsed.path === "new" || parsed.path === "grow" ? parsed.path : null,
      input: typeof parsed.input === "string" ? parsed.input : "",
      detail: typeof parsed.detail === "string" ? parsed.detail : "",
    };
  } catch {
    return EMPTY_DRAFT;
  }
}

/** Best-effort upstream detail for a failed submit: FastAPI's body.detail,
 * else the error message, else the HTTP status. */
function extractErrorDetail(err: unknown): string {
  if (err && typeof err === "object") {
    const body = (err as { body?: unknown }).body;
    if (body && typeof body === "object") {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.length > 0) return detail;
      if (detail !== undefined && detail !== null) {
        try {
          return JSON.stringify(detail);
        } catch {
          /* fall through */
        }
      }
    }
    const message = (err as { message?: unknown }).message;
    if (typeof message === "string" && message.length > 0) return message;
    const status = (err as { status?: unknown }).status;
    if (typeof status === "number") return `HTTP ${status}`;
  }
  return "Unknown error";
}

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
  // Stable for this mount: ProvisioningStepper only renders the funnel once
  // Clerk's `user` is truthy, so the id is resolved before we ever read the
  // draft — no key churn between the lazy init below and the persist effect.
  const draftKey = `${DRAFT_STORAGE_KEY}:${user?.id ?? "anon"}`;
  // Lazy initial state, so a restored draft is present on the FIRST render —
  // hydrating it in an effect would paint the empty path-picker for a frame.
  const [restored] = React.useState(() => readDraft(draftKey));
  const [path, setPath] = React.useState<Path | null>(restored.path);
  const [input, setInput] = React.useState(restored.input);
  const [detail, setDetail] = React.useState(restored.detail);
  const [state, setState] = React.useState<"idle" | "submitting" | "waiting" | "error">("idle");
  // Which submission the user made, so Retry replays the same one. Passed
  // explicitly into submit() rather than read from here — this state only
  // exists to survive the round trip to the Retry button.
  const [mode, setMode] = React.useState<Mode>("idea");
  // Consecutive-failure tracking for the persistent-error switch. One
  // completed submit() that lands in state "error" == one failure; the
  // streak only grows while the upstream detail stays the same.
  const [failure, setFailure] = React.useState<{ detail: string; streak: number } | null>(null);
  // Mirror the in-progress draft to storage on every edit. Cheap (three
  // short strings, only while the funnel is on screen) and it means the
  // draft is already durable at the instant of an unexpected unmount —
  // there is no unmount hook that could be skipped.
  React.useEffect(() => {
    try {
      if (path === null && !input && !detail) {
        window.sessionStorage.removeItem(draftKey);
        return;
      }
      window.sessionStorage.setItem(draftKey, JSON.stringify({ path, input, detail }));
    } catch {
      // Storage-disabled / quota-exceeded browser: the draft is a
      // best-effort convenience, never a correctness requirement.
    }
  }, [draftKey, path, input, detail]);

  const recordFailure = React.useCallback((detail: string) => {
    setFailure((prev) =>
      prev && prev.detail === detail
        ? { detail, streak: prev.streak + 1 }
        : { detail, streak: 1 },
    );
  }, []);

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
            setFailure(null);
            // The brief is in. Drop the draft so a LATER funnel cycle — a
            // wiped / reprovisioned container legitimately re-shows it —
            // opens clean instead of resurrecting this answer.
            try {
              window.sessionStorage.removeItem(draftKey);
            } catch {
              /* best-effort */
            }
            // "staged" and "already_submitted" both dismiss the funnel for
            // THIS user. The kickoff itself is server-owned now (spec PR-6,
            // BOOTSTRAP): the backend dispatches Friday's first-run kickoff
            // (and observes its own chat.final) once the funnel claim lands
            // and the runtime is up — see
            // core/services/bootstrap_dispatcher.py. `briefSubmitted` is
            // written here purely as an optimistic pre-poll paint (dismisses
            // the funnel THIS session without waiting for the next
            // /container/status poll to catch up); it is never read as
            // authority for whether the kickoff ran.
            await writeFirstRun(user, {
              briefSubmitted: true,
              path: path ?? undefined,
            });
            // Analytics only on a real seed: the backend emits this same
            // event name exclusively on "staged" (the actual goal/project
            // creation). Firing it here for "already_submitted" too would
            // inflate the count with non-seeding submitters who only ever
            // hit the pre-existing company claim.
            if (status === "staged") {
              // snake_case to match both the repo's property convention and
              // the backend's emission of this same event name.
              capture("first_run_brief_submitted", { path, no_idea: noIdea });
            }
            return;
          }
          if (attempt < MAX_RETRIES) {
            setState("waiting");
            await new Promise((r) => setTimeout(r, RETRY_MS));
            continue;
          }
          recordFailure(
            typeof (resp as { detail?: unknown } | null)?.detail === "string"
              ? ((resp as { detail: string }).detail)
              : `Unexpected response: ${JSON.stringify(resp) ?? "empty"}`,
          );
          setState("error");
          return;
        } catch (err) {
          const httpStatus = (err as { status?: number }).status;
          if (httpStatus === 409 && attempt < MAX_RETRIES) {
            setState("waiting");
            await new Promise((r) => setTimeout(r, RETRY_MS));
            continue;
          }
          recordFailure(extractErrorDetail(err));
          setState("error");
          return;
        }
      }
    },
    [api, user, path, input, detail, recordFailure, draftKey],
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
            {state === "error" &&
              (failure && failure.streak >= PERSISTENT_FAILURE_THRESHOLD ? (
                // Persistent failure: the same upstream error N times in a
                // row is not transient — stop implying a retry will fix it.
                // Show the upstream detail and route to support instead.
                <div className="space-y-1.5 text-sm text-[#b32424]">
                  <p>We couldn&apos;t save your brief.</p>
                  <p className="break-words text-xs text-[#8a8578]">{failure.detail}</p>
                  <p>
                    Please{" "}
                    <a className="underline" href="mailto:support@isol8.co">
                      contact support
                    </a>{" "}
                    and we&apos;ll get it sorted.
                  </p>
                </div>
              ) : (
                <p className="text-sm text-[#b32424]">
                  Couldn&apos;t save that just yet.{" "}
                  <button className="underline" onClick={() => submit(mode)}>
                    Retry
                  </button>
                </p>
              ))}
          </div>
        )}
      </div>
    </div>
  );
}
