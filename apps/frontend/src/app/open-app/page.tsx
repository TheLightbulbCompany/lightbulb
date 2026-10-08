"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { DEEP_LINK_SCHEME } from "@/lib/domain";
import { beginNativeAppReturn } from "@/lib/nativeAppReturn";

const WEB_FALLBACK = "/chat";

/** Post-onboarding handoff for accounts created in a mobile browser. */
export default function OpenAppPage() {
  const router = useRouter();

  useEffect(() => {
    const timeout = beginNativeAppReturn({
      userAgent: navigator.userAgent,
      platform: navigator.platform,
      maxTouchPoints: navigator.maxTouchPoints,
      openApp: (url) => window.location.assign(url),
      continueWeb: () => router.replace(WEB_FALLBACK),
      scheduleFallback: (callback, delayMs) => window.setTimeout(callback, delayMs),
    });
    return () => {
      if (timeout !== null) window.clearTimeout(timeout);
    };
  }, [router]);

  return (
    <div className="legal-page">
      <main className="legal-content">
        <h1>Opening Lightbulb…</h1>
        <p>If the app does not open, you can try again or continue in your browser.</p>
        <p>
          <a href={`${DEEP_LINK_SCHEME}://`}>Open the Lightbulb app</a>
          {" · "}
          <Link href={WEB_FALLBACK}>Continue on the web</Link>
        </p>
      </main>
    </div>
  );
}
