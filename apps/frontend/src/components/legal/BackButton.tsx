"use client";

import { useEffect, useRef } from "react";
import { usePathname, useRouter } from "next/navigation";

// True once the reader has navigated inside the app. A full page load resets module
// state, so a direct visit (a search result, a pasted link) reads false and Back goes
// to the fallback instead of leaving the site.
let navigatedInApp = false;

/** Mounted once in the root layout: flags any pathname change after the first render. */
export function InAppNavTracker(): null {
  const pathname = usePathname();
  const first = useRef(pathname);
  useEffect(() => {
    if (pathname !== first.current) navigatedInApp = true;
  }, [pathname]);
  return null;
}

/** Back to the previous in-app page, or `fallback` when this page was opened directly. */
export function BackButton({ fallback = "/" }: { fallback?: string }) {
  const router = useRouter();
  return (
    <button type="button" className="legal-btn-back" onClick={() => (navigatedInApp ? router.back() : router.push(fallback))}>
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <path d="M10 12L6 8L10 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      Back
    </button>
  );
}
