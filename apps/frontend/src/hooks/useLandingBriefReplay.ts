"use client";

/**
 * Replays a landing-page funnel brief after signup. The public landing hero
 * stashes the visitor's answers in sessionStorage before redirecting into
 * Clerk signup; once /chat's ProvisioningStepper reaches the exact state
 * where the in-chat funnel would render, this hook submits the stash so the
 * user never re-answers. The pre-drop launch hold ("launch_pending") counts
 * as that state too — the endpoint is intake-only, so the idea must not die
 * with sessionStorage while the account waits for the drop.
 *
 * The stash is deleted at READ time — a breakpoint remount re-enters the
 * firing state, and a duplicate automatic submit is worse than a lost stash
 * (the in-chat funnel is the natural fallback; the user retypes).
 *
 * Org contexts NEVER auto-submit: the backend accepts first-writer-wins from
 * any member, and an invited member with a stale stash must not claim the
 * org's one-shot company brief. Instead the stash is copied into the funnel's
 * owner-scoped draft key so it renders pre-filled and a human click submits.
 */
import * as React from "react";
import { useOrganization, useUser } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
import { writeFirstRun } from "@/lib/firstRun";
import { DRAFT_STORAGE_KEY } from "@/components/onboarding/FirstRunFunnel";
import type { ProvisioningPhase } from "@/hooks/useProvisioningState";

/** Written by the landing hero funnel; shape {path, input, detail}. */
export const LANDING_BRIEF_STORAGE_KEY = "isol8_landing_brief_v1";

interface LandingBrief {
  path: "new" | "grow";
  input: string;
  detail: string;
}

/** Client-side mirror of the backend BriefBody rules — an invalid stash is
 * dropped silently rather than burned on a guaranteed 422. */
function parseBrief(raw: string): LandingBrief | null {
  let parsed: Partial<LandingBrief>;
  try {
    const value: unknown = JSON.parse(raw);
    // The stash is user-writable: JSON.parse admits "null", numbers, arrays —
    // anything non-object must drop to the funnel, not throw in a render effect.
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      return null;
    }
    parsed = value as Partial<LandingBrief>;
  } catch {
    return null;
  }
  if (parsed.path !== "new" && parsed.path !== "grow") return null;
  const input = typeof parsed.input === "string" ? parsed.input.trim() : "";
  const detail = typeof parsed.detail === "string" ? parsed.detail.trim() : "";
  // `new` may omit the idea (the "you pick" flow); `grow` may not omit the
  // website — and a present-but-tiny input 422s on either path.
  if (input.length === 0 && parsed.path === "grow") return null;
  if (input.length > 0 && input.length < 3) return null;
  if (input.length > 2000 || detail.length > 500) return null;
  return { path: parsed.path, input, detail };
}

export function useLandingBriefReplay({
  gatePhase,
  blockedCode,
  awaitingBrief,
  isTerminalError,
}: {
  gatePhase: ProvisioningPhase;
  /** `blocked.code` when gatePhase is "blocked", else null. */
  blockedCode?: string | null;
  awaitingBrief: boolean;
  isTerminalError: boolean;
}): { replaying: boolean; orgDraftVersion: number } {
  // Same owner resolution as the funnel: while Clerk is still resolving the
  // org context the owner is UNKNOWN, and the stash must not be routed (an
  // org stash auto-POSTed as "personal" is exactly the org bypass the copy
  // path exists to prevent).
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const { user } = useUser();
  const api = useApi();
  const firedRef = React.useRef(false);
  // True from the moment a POST is dispatched. Never reset on success — the
  // funnel's render condition is identical to this hook's firing condition,
  // so it would flash during the ~1-2s until the next status poll reports the
  // pending brief as submitted. Reset only on failure, where the funnel IS
  // the fallback.
  const [replaying, setReplaying] = React.useState(false);
  // Bumped after the org-path draft copy. The funnel initializes its state
  // from sessionStorage at MOUNT, and it has already mounted by the time this
  // passive effect writes the draft — the consumer must remount it (key prop)
  // for the copied answers to become visible.
  const [orgDraftVersion, setOrgDraftVersion] = React.useState(0);

  React.useEffect(() => {
    if (firedRef.current) return;
    if (!orgLoaded) return;
    // Personal path needs the Clerk user resolved BEFORE the stash is
    // consumed: the POST itself doesn't need it, but the first-run metadata
    // mirror does, and a transiently-null user would silently skip it.
    if (!organization && !user) return;
    // A pre-drop held user is blocked with no container, but the stash must
    // still land: POST /onboarding/brief is intake only (a pending-brief row),
    // it needs no container, and it is what /launch/me's `idea` teaser reads
    // back. Every OTHER blocked code is a real dead end for a brief.
    const gateAllows = gatePhase === "normal" || (gatePhase === "blocked" && blockedCode === "launch_pending");
    if (!gateAllows || !awaitingBrief || isTerminalError) return;

    let raw: string | null = null;
    try {
      raw = window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY);
    } catch {
      return;
    }
    if (!raw) return;
    // Narrowed to `string` here so the failure-path closure below (which TS
    // cannot narrow a captured `let` inside) can restore it verbatim.
    const rawStash: string = raw;
    firedRef.current = true;
    try {
      window.sessionStorage.removeItem(LANDING_BRIEF_STORAGE_KEY);
    } catch {
      /* best-effort */
    }

    const brief = parseBrief(raw);
    if (!brief) return;

    if (organization) {
      // Pre-fill, never submit: land the stash under the funnel's own
      // owner-scoped draft key (same shape) so it renders filled in. An
      // EXISTING org draft wins — it is deliberate company-scoped intent,
      // and a stale personal landing stash must never replace it.
      try {
        const draftKey = `${DRAFT_STORAGE_KEY}:${organization.id}`;
        if (!window.sessionStorage.getItem(draftKey)) {
          window.sessionStorage.setItem(draftKey, JSON.stringify(brief));
          setOrgDraftVersion((v) => v + 1);
        }
      } catch {
        /* best-effort */
      }
      return;
    }

    // The stash is already gone (deleted at read time) and firedRef is set —
    // without recovery an unsuccessful POST loses the brief for good, and
    // held users now sit on this path for days before the drop. Put it back
    // (unless something newer landed in the meantime) and clear firedRef so
    // the next gate change retries.
    const restoreStash = () => {
      try {
        if (window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY) === null) {
          window.sessionStorage.setItem(LANDING_BRIEF_STORAGE_KEY, rawStash);
        }
      } catch {
        /* best-effort */
      }
      firedRef.current = false;
    };

    setReplaying(true);
    void (async () => {
      try {
        const resp = await api.post("/onboarding/brief", {
          path: brief.path,
          // Empty input on the `new` path is the "you pick" flow — absent,
          // never "", mirroring the funnel's own submit.
          input: brief.input || undefined,
          detail: brief.path === "new" && brief.detail ? brief.detail : undefined,
        });
        const status = (resp as { status?: string } | null)?.status;
        if (status === "staged" || status === "already_submitted") {
          // Mirror the funnel's successful-submit metadata: GetStarted gates
          // its auto-open on this, and a replayed first-run user must not get
          // the legacy guide over Friday's already-running kickoff.
          if (user) {
            void writeFirstRun(user, {
              briefSubmitted: true,
              path: brief.path,
            }).catch(() => {
              /* presentation cache only — never authority */
            });
          }
          return;
        }
        // A resolved response with any other status is not a success — the
        // brief must not be silently dropped, so it gets the same
        // restore-and-retry treatment as a rejected POST.
        setReplaying(false);
        restoreStash();
      } catch {
        setReplaying(false);
        restoreStash();
      }
    })();
  }, [api, gatePhase, blockedCode, awaitingBrief, isTerminalError, orgLoaded, organization, user]);

  return { replaying, orgDraftVersion };
}
