"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useAuth, useOrganizationList } from "@clerk/nextjs";

import { ApiError, useApi } from "@/lib/api";
import { Button } from "@/components/ui/button";

// =============================================================================
// Join a workspace from a shared link: /join/{orgId}/{token}
//
// The route is in `isProtectedRoute` (src/middleware.ts), so a signed-out
// visitor is bounced to Clerk sign-in and returned to this exact URL — the link
// is an authorization grant, never an authentication bypass. Redeeming
// therefore always happens as a known Clerk user.
//
// Two path segments, never a query string: the token is a credential, and a
// `?t=` would land in every access log and Referer header along the way. It is
// read here as a route param and POSTed in a JSON body, so it never appears in
// a backend URL at all.
//
// The redeem call is idempotent on the backend (already-a-member is a success),
// which is what lets the owner click their own link and lets a reload after a
// flaky network be safe.
// =============================================================================

type RedeemResult = {
  status: "joined" | "already_member" | string;
  org_id: string;
  org_name?: string;
  org_slug?: string;
};

type Phase =
  | { kind: "working" }
  | { kind: "done"; result: RedeemResult }
  | { kind: "failed"; message: string; canRetry: boolean };

export function joinErrorMessage(err: unknown): { message: string; canRetry: boolean } {
  if (err instanceof ApiError) {
    const detail =
      err.body && typeof err.body === "object" && "detail" in err.body
        ? (err.body as { detail?: unknown }).detail
        : undefined;
    if (err.status === 404) {
      return {
        // The backend answers every rejection identically on purpose (revoked,
        // unknown, wrong token) so the endpoint can't be probed. Pass its
        // sentence through rather than guessing at a more specific one.
        message:
          typeof detail === "string" && detail
            ? detail
            : "This join link is no longer valid. Ask a workspace admin for a new one.",
        canRetry: false,
      };
    }
    if (err.status === 401 || err.status === 403) {
      return { message: "Sign in again, then reopen the link.", canRetry: false };
    }
    return {
      message: "We couldn't add you to that workspace just now. Try again in a moment.",
      canRetry: true,
    };
  }
  return { message: "Something went wrong. Try again in a moment.", canRetry: true };
}

export default function JoinWorkspacePage() {
  const params = useParams<{ orgId: string; token: string }>();
  const orgId = params?.orgId ?? "";
  const token = params?.token ?? "";

  const router = useRouter();
  const api = useApi();
  const { isLoaded, isSignedIn } = useAuth();
  const { setActive } = useOrganizationList();

  const [phase, setPhase] = useState<Phase>({ kind: "working" });
  // Bumping this re-runs the effect below — the idiomatic way to re-trigger a
  // fetch without calling a setState-containing helper from inside the effect
  // (`react-hooks/set-state-in-effect` is an ERROR in this repo, not a warning).
  const [attempt, setAttempt] = useState(0);
  // Redeem is idempotent server-side, but StrictMode's double-invoke would
  // still fire two POSTs; one attempt per bump.
  const ran = useRef(-1);

  useEffect(() => {
    // Middleware guarantees a signed-in user here; the check is for the render
    // that happens before Clerk hydrates.
    if (!isLoaded || !isSignedIn || !orgId || !token) return;
    if (ran.current === attempt) return;
    ran.current = attempt;

    let cancelled = false;
    api
      .post(`/orgs/${orgId}/join`, { token })
      .then(async (raw) => {
        if (cancelled) return;
        const result = raw as RedeemResult;
        setPhase({ kind: "done", result });
        // Make the workspace the active org so /chat opens inside it. If Clerk
        // can't activate it yet (the membership is seconds old), /onboarding is
        // the surface that resolves memberships, so fall through to it.
        try {
          await setActive?.({ organization: result.org_id });
          router.push("/chat");
        } catch {
          router.push("/onboarding");
        }
      })
      .catch((err) => {
        if (cancelled) return;
        const { message, canRetry } = joinErrorMessage(err);
        setPhase({ kind: "failed", message, canRetry });
      });

    return () => {
      cancelled = true;
    };
  }, [api, attempt, isLoaded, isSignedIn, orgId, token, setActive, router]);

  // An event handler, so setState here is exactly where React wants it.
  const retry = useCallback(() => {
    setPhase({ kind: "working" });
    setAttempt((n) => n + 1);
  }, []);

  if (!orgId || !token) {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-4 px-6 text-center">
        <h1 className="font-serif text-2xl text-midnight">Couldn&apos;t join</h1>
        <p className="text-ui text-midnight-muted" role="alert">
          That join link is incomplete. Ask a workspace admin for a new one.
        </p>
      </main>
    );
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-4 px-6 text-center">
      {phase.kind === "working" && (
        <>
          <div className="settings-spinner" role="status" aria-label="Joining workspace" />
          <p className="text-ui text-midnight-muted">Adding you to the workspace…</p>
        </>
      )}

      {phase.kind === "done" && (
        <>
          <h1 className="font-serif text-2xl text-midnight">
            {phase.result.status === "already_member"
              ? `You're already in ${phase.result.org_name || "this workspace"}`
              : `Welcome to ${phase.result.org_name || "the workspace"}`}
          </h1>
          <p className="text-ui text-midnight-muted">Taking you to your workspace…</p>
        </>
      )}

      {phase.kind === "failed" && (
        <>
          <h1 className="font-serif text-2xl text-midnight">Couldn&apos;t join</h1>
          <p className="text-ui text-midnight-muted" role="alert">
            {phase.message}
          </p>
          <div className="flex gap-2">
            {phase.canRetry && (
              <Button type="button" onClick={retry}>
                Try again
              </Button>
            )}
            <Button type="button" variant="outline" onClick={() => router.push("/chat")}>
              Go to Lightbulb
            </Button>
          </div>
        </>
      )}
    </main>
  );
}
