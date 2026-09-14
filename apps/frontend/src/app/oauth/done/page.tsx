"use client";

import { Suspense, useEffect } from "react";
import { useAuth } from "@clerk/nextjs";
import { useRouter, useSearchParams } from "next/navigation";

/**
 * /oauth/done — where the backend's OAuth callback lands the browser.
 *
 * Public on purpose (it is outside the Clerk matcher in middleware.ts): the
 * browser that finishes a connect may be the owner's own tab OR the agent's
 * container Chrome, which has no Clerk session. A protected landing would
 * bounce the agent's Chrome to a sign-in page right after the connect
 * succeeded — and an owner who signed in there would hand the agent's browser
 * an Isol8 session. So: a signed-in viewer continues to /settings/connections
 * exactly as before (same query string, same toasts) — that route forwards on
 * to the workspace Connections panel, which owns the banner; anyone else gets
 * a close-this-tab note and nothing else. No data is fetched here.
 */
function Done() {
  const { isLoaded, isSignedIn } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const connected = params.get("connected");
  const error = params.get("error");

  useEffect(() => {
    if (isLoaded && isSignedIn) router.replace(`/settings/connections?${params.toString()}`);
  }, [isLoaded, isSignedIn, router, params]);

  if (!isLoaded || isSignedIn) return null;
  return (
    <main className="flex min-h-screen items-center justify-center p-8 text-center">
      <div>
        <h1 className="text-xl font-semibold">
          {error ? "Connection failed" : `${connected ?? "Service"} connected`}
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          {error === "already_connected"
            ? "This service was already connected, so nothing changed. You can close this tab."
            : error
              ? `Something went wrong (${error}). Ask your agent to try again.`
              : "You can close this tab — your agent can use it now."}
        </p>
      </div>
    </main>
  );
}

export default function OAuthDonePage() {
  return (
    <Suspense fallback={null}>
      <Done />
    </Suspense>
  );
}
