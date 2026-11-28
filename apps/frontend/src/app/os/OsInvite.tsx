"use client";

import { SignUp, useAuth } from "@clerk/nextjs";
import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { useApi } from "@/lib/api";

import { CHANNEL, CODE, SparkGet, useOnPhone } from "./SparkDownload";

export const SOON = "Download coming soon.";

// The download button (or "coming soon" until the installer is published), and on a referral link
// (`/os?ref=CODE`) the invite: a signed-out visitor creates their account here, and a signed-in one has
// the invite recorded (POST /os/channel for `?c=CODE`, then POST /os/referral). The Spark account itself is created by the Mac app (the first
// account read starts the trial), so this page never calls the account route.
export function OsInvite({ ready, source }: { ready: boolean; source?: string }) {
  const params = useSearchParams();
  const ref = params?.get("ref") ?? "";
  const c = params?.get("c") ?? "";
  const code = CODE.test(ref) ? ref : "";
  const channel = CHANNEL.test(c) ? c : "";
  const download = <DownloadBlock ready={ready} source={source} />;
  if (!code && !channel) return download;
  return <Invite code={code} channel={channel} ready={ready} download={download} />;
}

// The plain hero: also the Suspense fallback, so the button is in the server HTML before the invite check.
export function DownloadBlock({ ready, source }: { ready: boolean; source?: string }) {
  return (
    <div className="mt-12">
      {ready ? <SparkGet source={source} /> : <p className="text-lg text-[var(--lb-ink)]">{SOON}</p>}
    </div>
  );
}

function Invite({ code, channel, ready, download }: { code: string; channel: string; ready: boolean; download: React.ReactNode }) {
  const { isLoaded, isSignedIn } = useAuth();
  const api = useApi();
  const onPhone = useOnPhone();
  const [recorded, setRecorded] = useState(false);
  const sent = useRef(false);
  const here = `/os?${new URLSearchParams({ ...(code && { ref: code }), ...(channel && { c: channel }) })}`;

  useEffect(() => {
    if (!isLoaded || !isSignedIn) return;
    // Signing up on this page flips isSignedIn in place, so only the signed-in request is guarded.
    if (sent.current) return;
    sent.current = true;
    // The answer does not change what the page shows; a failure still shows the download.
    // The channel goes first: the first set-once wins, so a creator code beats "referral".
    (channel ? api.post("/os/channel", { channel }).catch(() => undefined) : Promise.resolve())
      .then(() => (code ? api.post("/os/referral", { code }) : undefined))
      .catch(() => undefined)
      .finally(() => setRecorded(true));
  }, [isLoaded, isSignedIn, api, code, channel]);

  if (!isLoaded) return null;
  if (!isSignedIn) {
    return (
      <div className="mt-12">
        {code ? null : <h2 className="mb-2 text-xl text-[var(--lb-ink)]">Get Spark</h2>}
        <p className="mb-6 text-lg leading-8 text-[var(--lb-ink)]">
          {code ? "You've been invited to Spark. Create your account, then download." : "Create your account, then download."}
        </p>
        {/* The card's "Sign in" link goes to the /sign-in page, and Clerk's redirect_url brings the person back
            here to record the invite. Not a hash on this page: SignUp's hash router has no sign-in route. */}
        <SignUp
          routing="hash"
          forceRedirectUrl={here}
          signInForceRedirectUrl={here}
          signInUrl={`/sign-in?${new URLSearchParams({ redirect_url: here })}`}
        />
        {onPhone ? null : ready ? (
          <a
            href="/download/spark"
            className="mt-6 inline-block text-sm text-[var(--lb-ink-soft)] underline underline-offset-[3px]"
          >
            Skip, just download
          </a>
        ) : (
          <p className="mt-6 text-sm text-[var(--lb-ink-soft)]">{SOON}</p>
        )}
      </div>
    );
  }
  if (!recorded) return null;
  return (
    <>
      {download}
      <p className="mt-6 text-sm text-[var(--lb-ink-soft)]">
        Open Spark and sign in with the same Apple, Google or email.
      </p>
    </>
  );
}
