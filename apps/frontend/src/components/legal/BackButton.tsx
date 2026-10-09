"use client";

import { useRouter } from "next/navigation";

/** Back to the page the reader came from; home when this page was opened directly. */
export function BackButton() {
  const router = useRouter();
  const cameFromHere = () => {
    try {
      // document.referrer is frozen at the first load, so it cannot see in-app navigation;
      // Next's router marks every history entry it handles with __NA.
      const inApp = !!window.history.state?.__NA || (!!document.referrer && new URL(document.referrer).origin === window.location.origin);
      return window.history.length > 1 && inApp;
    } catch {
      return false;
    }
  };
  return (
    <button type="button" className="legal-btn-back" onClick={() => (cameFromHere() ? router.back() : router.push("/"))}>
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <path d="M10 12L6 8L10 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      Back
    </button>
  );
}
