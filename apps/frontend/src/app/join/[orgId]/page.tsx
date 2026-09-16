"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useParams, useRouter } from "next/navigation";
import { SignInButton, useAuth, useOrganizationList } from "@clerk/nextjs";

import { ApiError, useApi } from "@/lib/api";
import { joinTokenKey } from "@/lib/workspaceJoin";
import { Button } from "@/components/ui/button";

// =============================================================================
// Join a workspace from a shared link: /join/{orgId}#{token}
//
// The token is a credential, so it lives in the fragment: browsers never send
// fragments in HTTP requests or Referer headers. On first render we move it to
// sessionStorage and scrub it from the address bar. A signed-out holder signs
// in through Clerk's modal on this same page, preserving sessionStorage without
// copying the credential into an auth redirect URL. The authenticated client
// then POSTs it in the redeem body.
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
    if (err.status === 409 && typeof detail === "string" && detail) {
      // The workspace's member cap is full; retrying cannot change that.
      return { message: detail, canRetry: false };
    }
    return {
      message: "We couldn't add you to that workspace just now. Try again in a moment.",
      canRetry: true,
    };
  }
  return { message: "Something went wrong. Try again in a moment.", canRetry: true };
}

export default function JoinWorkspacePage() {
  const params = useParams<{ orgId: string }>();
  const orgId = params?.orgId ?? "";

  const router = useRouter();
  const api = useApi();
  const { isLoaded, isSignedIn } = useAuth();
  const { isLoaded: orgListLoaded, setActive } = useOrganizationList();

  const tokenStorageKey = joinTokenKey(orgId);
  const subscribe = useCallback(() => () => {}, []);
  const readToken = useCallback((): string | null => {
    if (typeof window === "undefined") return null;
    if (!orgId) return "";
    const fragment = window.location.hash.slice(1);
    return fragment || window.sessionStorage.getItem(tokenStorageKey) || "";
  }, [orgId, tokenStorageKey]);
  const token = useSyncExternalStore(subscribe, readToken, () => null);

  const [phase, setPhase] = useState<Phase>({ kind: "working" });
  // Bumping this re-runs the effect below — the idiomatic way to re-trigger a
  // fetch without calling a setState-containing helper from inside the effect
  // (`react-hooks/set-state-in-effect` is an ERROR in this repo, not a warning).
  const [attempt, setAttempt] = useState(0);
  // Redeem is idempotent server-side, but StrictMode's double-invoke would
  // still fire two POSTs; one attempt per bump.
  const ran = useRef(-1);
  const settled = useRef(false);

  useEffect(() => {
    if (!token || typeof window === "undefined") return;
    if (window.location.hash) {
      window.sessionStorage.setItem(tokenStorageKey, token);
      window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    }
  }, [token, tokenStorageKey]);

  useEffect(
    () => () => {
      // Keep the captured token available while the outcome renders.
      // Navigation unmounts this page, which is the safe point to discard it;
      // after that the onboarding gate stops sending this tab back here.
      if (settled.current) window.sessionStorage.removeItem(tokenStorageKey);
    },
    [tokenStorageKey],
  );

  useEffect(() => {
    if (!isLoaded || !isSignedIn || !orgListLoaded || !setActive || !orgId || !token) return;
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
          await setActive({ organization: result.org_id });
          settled.current = true;
          router.push("/chat");
        } catch {
          settled.current = true;
          router.push("/onboarding");
        }
      })
      .catch((err) => {
        if (cancelled) return;
        const { message, canRetry } = joinErrorMessage(err);
        // Leaving through this page abandons the link (dropped on unmount). A
        // dead link can never succeed, so drop it now too: nothing may route
        // the tab back to it. A retryable one stays on purpose: a full page
        // load to /chat brings an org-less tab back here to retry, not to
        // /onboarding.
        settled.current = true;
        if (!canRetry) window.sessionStorage.removeItem(tokenStorageKey);
        setPhase({ kind: "failed", message, canRetry });
      });

    return () => {
      cancelled = true;
    };
  }, [
    api,
    attempt,
    isLoaded,
    isSignedIn,
    orgListLoaded,
    orgId,
    token,
    tokenStorageKey,
    setActive,
    router,
  ]);

  // An event handler, so setState here is exactly where React wants it.
  const retry = useCallback(() => {
    setPhase({ kind: "working" });
    setAttempt((n) => n + 1);
  }, []);

  if (!orgId) {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-4 px-6 text-center">
        <h1 className="font-serif text-2xl text-midnight">Couldn&apos;t join</h1>
        <p className="text-ui text-midnight-muted" role="alert">
          That join link is incomplete. Ask a workspace admin for a new one.
        </p>
      </main>
    );
  }

  if (token === null || !isLoaded || (isSignedIn && !orgListLoaded)) {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-4 px-6 text-center">
        <div className="settings-spinner" role="status" aria-label="Loading join link" />
      </main>
    );
  }

  // A dead link's token is dropped on failure; keep showing why.
  if (!token && phase.kind !== "failed") {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-4 px-6 text-center">
        <h1 className="font-serif text-2xl text-midnight">Couldn&apos;t join</h1>
        <p className="text-ui text-midnight-muted" role="alert">
          That join link is incomplete. Ask a workspace admin for a new one.
        </p>
      </main>
    );
  }

  if (!isSignedIn) {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col items-center justify-center gap-4 px-6 text-center">
        <h1 className="font-serif text-2xl text-midnight">Join this workspace</h1>
        <p className="text-ui text-midnight-muted">Sign in first, then we&apos;ll add you.</p>
        {/* A brand-new Google/Apple account finishes its sign-up on /sign-in's
            SSO callback, whose default is /chat. Pin both redirects back here,
            where the parked token is waiting. The path carries no token. */}
        <SignInButton
          mode="modal"
          forceRedirectUrl={`/join/${encodeURIComponent(orgId)}`}
          signUpForceRedirectUrl={`/join/${encodeURIComponent(orgId)}`}
        >
          <Button type="button">Sign in to join</Button>
        </SignInButton>
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
