"use client";

import { SignUp, useAuth } from "@clerk/nextjs";
import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { capture } from "@/lib/analytics";
import { useApi } from "@/lib/api";

import { SparkDownload } from "./SparkDownload";

// A Spark referral code: the referrer's Clerk user id without its `user_` prefix.
export const CODE = /^[A-Za-z0-9]{1,64}$/;

const SOON = "Download coming soon.";

// The download button (or "coming soon" until the installer is published), and on a referral link
// (`/os?ref=CODE`) the invite: a signed-out visitor creates their account here, and a signed-in one has
// the invite recorded (POST /os/referral). The Spark account itself is created by the Mac app (the first
// account read starts the trial), so this page never calls the account route.
export function OsInvite({ ready }: { ready: boolean }) {
  const code = useSearchParams()?.get("ref") ?? "";
  const download = (
    <div className="mt-12">{ready ? <SparkDownload /> : <p className="text-lg text-[var(--lb-ink)]">{SOON}</p>}</div>
  );
  if (!CODE.test(code)) return download;
  return <Invite code={code} ready={ready} download={download} />;
}

function Invite({ code, ready, download }: { code: string; ready: boolean; download: React.ReactNode }) {
  const { isLoaded, isSignedIn } = useAuth();
  const api = useApi();
  const [recorded, setRecorded] = useState(false);
  const sent = useRef(false);
  const here = `/os?ref=${code}`;

  useEffect(() => {
    if (!isLoaded || sent.current) return;
    sent.current = true;
    if (!isSignedIn) {
      capture("spark_invite_signup_started");
      return;
    }
    // The answer does not change what the page shows; a failure still shows the download.
    api
      .post("/os/referral", { code })
      .then(() => capture("spark_invite_signup_completed"))
      .catch(() => undefined)
      .finally(() => setRecorded(true));
  }, [isLoaded, isSignedIn, api, code]);

  if (!isLoaded) return null;
  if (!isSignedIn) {
    return (
      <div className="mt-12">
        <p className="mb-6 text-lg leading-8 text-[var(--lb-ink)]">
          You&apos;ve been invited to Spark. Create your account, then download.
        </p>
        <SignUp routing="hash" forceRedirectUrl={here} signInForceRedirectUrl={here} signInUrl={`${here}#/sign-in`} />
        {ready ? (
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
