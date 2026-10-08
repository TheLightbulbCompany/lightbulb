"use client";

import { useAuth } from "@clerk/nextjs";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  beginNativeAppReturn,
  createNativeAppSignInUrl,
  isIosDevice,
} from "@/lib/nativeAppReturn";

const WEB_FALLBACK = "/chat";

/** Post-onboarding handoff for accounts created in a mobile browser. */
export default function OpenAppPage() {
  const router = useRouter();
  const { getToken, isLoaded } = useAuth();
  const [appUrl, setAppUrl] = useState<string | null>(null);
  const [handoffFailed, setHandoffFailed] = useState(false);

  useEffect(() => {
    if (!isLoaded) return;

    if (!isIosDevice(navigator.userAgent, navigator.platform, navigator.maxTouchPoints)) {
      router.replace(WEB_FALLBACK);
      return;
    }

    let cancelled = false;
    let timeout: number | null = null;
    void createNativeAppSignInUrl(WEB_FALLBACK, getToken)
      .then((url) => {
        if (cancelled) return;
        setAppUrl(url);
        timeout = beginNativeAppReturn({
          userAgent: navigator.userAgent,
          platform: navigator.platform,
          maxTouchPoints: navigator.maxTouchPoints,
          appUrl: url,
          openApp: (nativeUrl) => window.location.assign(nativeUrl),
          continueWeb: () => router.replace(WEB_FALLBACK),
          scheduleFallback: (callback, delayMs) => window.setTimeout(callback, delayMs),
        });
      })
      .catch(() => {
        if (!cancelled) setHandoffFailed(true);
      });

    return () => {
      cancelled = true;
      if (timeout !== null) window.clearTimeout(timeout);
    };
  }, [getToken, isLoaded, router]);

  return (
    <div className="legal-page">
      <main className="legal-content">
        <h1>Opening Lightbulb…</h1>
        <p>
          {handoffFailed
            ? "We couldn’t securely sign you in to the app. Continue in your browser."
            : "If the app does not open, you can try again or continue in your browser."}
        </p>
        <p>
          {appUrl ? <><a href={appUrl}>Open the Lightbulb app</a>{" · "}</> : null}
          <Link href={WEB_FALLBACK}>Continue on the web</Link>
        </p>
      </main>
    </div>
  );
}
