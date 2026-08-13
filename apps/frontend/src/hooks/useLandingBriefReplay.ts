"use client";

/**
 * Replays a landing-page funnel brief after signup. The public landing hero
 * stashes the visitor's answers in sessionStorage before redirecting into
 * Clerk signup; once /chat's ProvisioningStepper reaches the exact state
 * where the in-chat funnel would render, this hook submits the stash so the
 * user never re-answers.
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
import { useOrganization } from "@clerk/nextjs";
import { useApi } from "@/lib/api";
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
    parsed = JSON.parse(raw) as Partial<LandingBrief>;
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
  awaitingBrief,
  isTerminalError,
}: {
  gatePhase: ProvisioningPhase;
  awaitingBrief: boolean;
  isTerminalError: boolean;
}): { replaying: boolean } {
  // Same owner resolution as the funnel: while Clerk is still resolving the
  // org context the owner is UNKNOWN, and the stash must not be routed (an
  // org stash auto-POSTed as "personal" is exactly the org bypass the copy
  // path exists to prevent).
  const { organization, isLoaded: orgLoaded } = useOrganization();
  const api = useApi();
  const firedRef = React.useRef(false);
  // True from the moment a POST is dispatched. Never reset on success — the
  // funnel's render condition is identical to this hook's firing condition,
  // so it would flash during the ~1-2s until the next status poll reports the
  // pending brief as submitted. Reset only on failure, where the funnel IS
  // the fallback.
  const [replaying, setReplaying] = React.useState(false);

  React.useEffect(() => {
    if (firedRef.current) return;
    if (!orgLoaded) return;
    if (gatePhase !== "normal" || !awaitingBrief || isTerminalError) return;

    let raw: string | null = null;
    try {
      raw = window.sessionStorage.getItem(LANDING_BRIEF_STORAGE_KEY);
    } catch {
      return;
    }
    if (!raw) return;
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
      // owner-scoped draft key (same shape) so it renders filled in.
      try {
        window.sessionStorage.setItem(
          `${DRAFT_STORAGE_KEY}:${organization.id}`,
          JSON.stringify(brief),
        );
      } catch {
        /* best-effort */
      }
      return;
    }

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
        if (status === "staged" || status === "already_submitted") return;
        setReplaying(false);
      } catch {
        setReplaying(false);
      }
    })();
  }, [api, gatePhase, awaitingBrief, isTerminalError, orgLoaded, organization]);

  return { replaying };
}
