"use client";

/**
 * Two-path onboarding funnel shown to a first-time owner or org member
 * (trial_local free-tier signup or organization) while their company's
 * workspace provisions (spec §3). Submitting RECORDS the answer server-side
 * and returns; the seeding it drives (goal, project, BRIEF.md, Friday's
 * kickoff) is server-owned and server-retried once the workspace is up — see
 * core/services/onboarding_brief.py. So this posts once against a current
 * backend; the only retries left here are the deploy-skew shim below. It is
 * the committed onboarding front door — there is no skip, so submitting the
 * brief is the only way past it.
 */
import * as React from "react";
import { useOrganization, useUser } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { writeFirstRun } from "@/lib/firstRun";
import { FirstRunSteps, type Mode, type Path } from "@/components/onboarding/FirstRunSteps";
// DEPLOY-SKEW SHIM. The endpoint is durable intake now — it records the answer
// and returns, so there is nothing to wait out and no retry loop. But Vercel
// ships this file independently of (and usually ahead of) the ECS backend, and
// the OLD backend still 409s "not_ready" for roughly the first ~30s of every
// first-run signup while Paperclip seeds Friday. Without these two retries,
// every user who signs up inside that deploy window gets a red error on a
// signup that is working — the exact bug this whole change exists to kill.
// DELETE both constants and the loop once the durable-intake backend is
// deployed everywhere.
const SKEW_RETRY_MS = 3000;
const SKEW_RETRIES = 2;

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
// Exported for useLandingBriefReplay, which pre-fills an org workspace's
// funnel from a landing-page stash by writing this same owner-scoped key.
export const DRAFT_STORAGE_KEY = "isol8_first_run_draft_v1";

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

export function FirstRunFunnel() {
  const { user } = useUser();
  // `isLoaded` is load-bearing, not decoration: Clerk resolves the org
  // context independently of the user, so there is a window where `user` is
  // present and `organization` is still undefined. Reading that as "personal"
  // would render — and let the user submit — a personal-workspace draft
  // inside what turns out to be an org. Until it resolves, the owner is
  // UNKNOWN and no draft is touched.
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const api = useApi();
  // Keyed to the OWNER, not the signed-in user — the same owner identity
  // ProvisioningStepper uses for its activation markers. Onboarding is a
  // per-CONTAINER fact and an org shares one container, so the draft belongs
  // to the workspace. Keying it on `user.id` alone would hand one workspace's
  // in-progress brief to another: someone who starts a brief in org A and
  // then activates an un-onboarded org B in the same tab would have A's text
  // restored and submitted under B's API context.
  //
  // NULL while the owner is unknown (org context still loading). Every draft
  // read and write below is gated on it, so an unknown owner means "no
  // draft", never "assume personal". A workspace switch produces a DIFFERENT
  // key, which is exactly the isolation this provides.
  const draftKey = orgLoaded
    ? `${DRAFT_STORAGE_KEY}:${organization?.id ?? user?.id ?? "anon"}`
    : null;
  // Lazy initial state, so a restored draft is present on the FIRST render —
  // hydrating it in an effect would paint the empty path-picker for a frame.
  const [restored] = React.useState(() => (draftKey ? readDraft(draftKey) : EMPTY_DRAFT));
  const [path, setPath] = React.useState<Path | null>(restored.path);
  const [input, setInput] = React.useState(restored.input);
  const [detail, setDetail] = React.useState(restored.detail);

  const [state, setState] = React.useState<"idle" | "submitting" | "error">("idle");
  // Which submission the user made, so Retry replays the same one. Passed
  // explicitly into submit() rather than read from here — this state only
  // exists to survive the round trip to the Retry button.
  const [mode, setMode] = React.useState<Mode>("idea");
  // Consecutive-failure tracking for the persistent-error switch. One
  // completed submit() that lands in state "error" == one failure; the
  // streak only grows while the upstream detail stays the same.
  const [failure, setFailure] = React.useState<{ detail: string; streak: number } | null>(null);
  // A workspace switch can change the owner WITHOUT unmounting this component
  // — Clerk's `setActive` re-renders in place. The lazy initializer above only
  // runs at mount, so without this the previous owner's text would stay on
  // screen AND the persist effect below would write it under the NEW owner's
  // key: the same cross-workspace leak the owner-scoped key exists to prevent,
  // by another route. Re-read for the new owner during render — React's
  // "adjust state when a prop changes" pattern, the same render-time-derivation
  // idiom ProvisioningStepper uses — so the switch lands on the new
  // workspace's own draft with no frame showing the old one.
  //
  // The SUBMISSION state resets with it, and that half is not cosmetic:
  // `submit()` deliberately never returns to "idle" on success (it expects the
  // status poll to unmount the funnel), so a partial reset would leave the new
  // workspace's funnel inheriting `state: "submitting"` — Continue disabled,
  // permanently, on the committed onboarding front door. `ownerRef` is the
  // matching half: a submit already in flight for the OLD owner checks it when
  // its POST resolves and abandons, so a late response can't write the new
  // workspace's metadata or clear its draft.
  const ownerRef = React.useRef(draftKey);
  const [draftOwner, setDraftOwner] = React.useState(draftKey);
  if (draftOwner !== draftKey) {
    const next = draftKey ? readDraft(draftKey) : EMPTY_DRAFT;
    // Written HERE, in render, not in an effect: an in-flight submit resolving
    // between this render and a post-commit effect would still read the OLD
    // owner and write the new workspace's metadata — the exact race this
    // guards. Same derived-state-from-transition pattern (and same rule
    // suppression) as ProvisioningStepper's `previousBackendPhaseRef`.
    ownerRef.current = draftKey;
    setDraftOwner(draftKey);
    setPath(next.path);
    setInput(next.input);
    setDetail(next.detail);
    setState("idle");
    setFailure(null);
  }

  // Mirror the in-progress draft to storage on every edit. Cheap (three
  // short strings, only while the funnel is on screen) and it means the
  // draft is already durable at the instant of an unexpected unmount —
  // there is no unmount hook that could be skipped.
  React.useEffect(() => {
    // Owner not yet known — writing now would file this text under a guessed
    // key and defeat the isolation above.
    if (!draftKey) return;
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
      // The owner this submission belongs to, re-checked against `ownerRef`
      // once the POST resolves: a workspace switch mid-flight (Clerk's
      // `setActive` re-renders in place) must not let this response write the
      // NEW workspace's metadata or clear its draft.
      const submitOwner = ownerRef.current;
      const ownerChanged = () => ownerRef.current !== submitOwner;
      if (!noIdea && input.trim().length < 3) return;
      setState("submitting");
      try {
        let resp: unknown;
        let status: string | undefined;
        // Single POST against a current backend. The loop is the deploy-skew
        // shim only: an old backend signals "not ready" two ways — a thrown
        // 409, and a 2xx (202) whose body carries no recognised status — and
        // both are retried a couple of times before we call it an error.
        for (let attempt = 0; ; attempt++) {
          try {
            // No input at all on the "you pick" button: the absent idea IS the
            // signal, and BRIEF.md tells Friday to choose one from what she can
            // find out about them. Sending "" instead would be a brief quoting
            // an empty string back at her.
            resp = await api.post("/onboarding/brief", {
              path,
              input: noIdea ? undefined : input.trim(),
              detail: !noIdea && path === "new" && detail.trim() ? detail.trim() : undefined,
            });
          } catch (err) {
            if (ownerChanged()) return;
            if ((err as { status?: number }).status === 409 && attempt < SKEW_RETRIES) {
              await new Promise((r) => setTimeout(r, SKEW_RETRY_MS));
              if (ownerChanged()) return;
              continue;
            }
            throw err;
          }
          if (ownerChanged()) return;
          status = (resp as { status?: string } | null)?.status;
          if (status === "staged" || status === "already_submitted") break;
          if (attempt < SKEW_RETRIES) {
            await new Promise((r) => setTimeout(r, SKEW_RETRY_MS));
            if (ownerChanged()) return;
            continue;
          }
          // Never dismiss the funnel over a brief that was never saved — the
          // one thing worse than showing the error is losing what they typed.
          recordFailure(
            typeof (resp as { detail?: unknown } | null)?.detail === "string"
              ? (resp as { detail: string }).detail
              : `Unexpected response: ${JSON.stringify(resp) ?? "empty"}`,
          );
          setState("error");
          return;
        }
        setFailure(null);
        // The brief is in. Drop the draft so a LATER funnel cycle — a
        // wiped / reprovisioned container legitimately re-shows it —
        // opens clean instead of resurrecting this answer.
        try {
          if (submitOwner) window.sessionStorage.removeItem(submitOwner);
        } catch {
          /* best-effort */
        }
        // The kickoff is server-owned (spec PR-6, BOOTSTRAP): the backend
        // seeds the goal/project and dispatches Friday's first-run kickoff
        // (observing its own chat.final) once the workspace is up — see
        // core/services/onboarding_brief.reconcile_pending_brief. This
        // metadata write is purely an optimistic pre-poll paint (dismisses
        // the funnel THIS session without waiting for the next
        // /container/status poll); it is never read as authority for
        // whether the kickoff ran.
        await writeFirstRun(user, {
          briefSubmitted: true,
          path: path ?? undefined,
        });
        // NO `first_run_brief_submitted` here. The backend emits it, and only
        // once the goal and project actually exist
        // (core/services/onboarding_brief.py). Emitting it from the client at
        // submit time would report a 100% onboarding funnel straight through a
        // fleet-wide fulfilment outage, and double-count every submit besides.
      } catch (err) {
        if (ownerChanged()) return;
        recordFailure(extractErrorDetail(err));
        setState("error");
      }
    },
    [api, user, path, input, detail, recordFailure],
  );

  const busy = state === "submitting";

  return (
    <div className="flex-1 flex items-center justify-center p-6 bg-[var(--cream)]">
      <div className="w-full max-w-md space-y-8">
        <FirstRunSteps
          path={path}
          input={input}
          detail={detail}
          busy={busy}
          mode={mode}
          error={state === "error" ? failure : null}
          onSetPath={setPath}
          onInput={setInput}
          onDetail={setDetail}
          onSubmit={submit}
        />
      </div>
    </div>
  );
}
