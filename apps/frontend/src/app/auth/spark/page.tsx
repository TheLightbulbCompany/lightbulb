"use client";

import { useAuth, useClerk } from "@clerk/nextjs";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { REF_KEY, REF_PATTERN } from "@/app/os/RefCapture";

// Spark (the Mac app) opens this page to sign in. The signed-in person gets a one-time ticket,
// handed back to the app on its own URL scheme, which the app trades for its token.

const SCHEMES = new Set(["spark", "spark-dev", "eve"]);
// The app's one-time value for this sign-in: the ticket redeems only with it, so a ticket from someone
// else's sign-in cannot be pushed into this person's app.
const STATE = /^[A-Za-z0-9_-]{16,128}$/;

function SparkSignIn() {
  const { isLoaded, isSignedIn } = useAuth();
  const clerk = useClerk();
  const api = useApi();
  const params = useSearchParams();
  const scheme = params.get("scheme") ?? "";
  const state = params.get("state") ?? "";
  const allowed = SCHEMES.has(scheme) && STATE.test(state);
  const [link, setLink] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  // One ticket per page load: the account holds one ticket, so a second request would void the first.
  const asked = useRef(false);

  useEffect(() => {
    if (!allowed || !isLoaded) return;
    if (!isSignedIn) {
      // The app's own sign-in and sign-up pages, which return here (scheme and state checked again on return).
      const back = new URLSearchParams({ scheme, state });
      clerk.redirectToSignIn({ redirectUrl: `${window.location.origin}/auth/spark?${back}` });
      return;
    }
    if (asked.current) return;
    asked.current = true;
    let ref: string | null = null;
    try {
      ref = localStorage.getItem(REF_KEY);
    } catch {
      // storage blocked: sign in without a referral
    }
    const body = ref && REF_PATTERN.test(ref) ? { state, ref } : { state };
    (api.post("/os/ticket", body) as Promise<{ ticket: string }>)
      .then(({ ticket }) => {
        try {
          localStorage.removeItem(REF_KEY);
        } catch {
          // storage blocked: nothing was stored
        }
        const url = `${scheme}://auth?ticket=${encodeURIComponent(ticket)}&state=${encodeURIComponent(state)}`;
        setLink(url);
        // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- the app's URL scheme, not a page
        window.location.href = url;
      })
      .catch(() => setFailed(true));
  }, [allowed, isLoaded, isSignedIn, scheme, state, api, clerk]);

  let body: React.ReactNode;
  if (!allowed) {
    body = <h1 className="text-xl font-semibold">This sign-in link is not valid. Start again from Spark.</h1>;
  } else if (failed) {
    body = <h1 className="text-xl font-semibold">Could not sign in. Reload the page to try again.</h1>;
  } else if (link) {
    body = (
      <>
        <h1 className="text-xl font-semibold mb-2">You can go back to Spark</h1>
        <p className="text-sm text-muted-foreground mb-4">If Spark did not open, use the button.</p>
        <Button onClick={() => (window.location.href = link)}>Open Spark</Button>
      </>
    );
  } else {
    body = <h1 className="text-xl font-semibold">Signing in...</h1>;
  }

  return (
    <div className="flex justify-center items-center h-screen">
      <div className="text-center flex flex-col items-center">{body}</div>
    </div>
  );
}

export default function SparkSignInPage() {
  return (
    <Suspense>
      <SparkSignIn />
    </Suspense>
  );
}
