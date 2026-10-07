"use client";

import Script from "next/script";
import { useEffect } from "react";

declare global {
  interface Window {
    Termly?: { initialize?: () => void };
  }
}

/**
 * Renders a Termly-hosted legal document. The Termly SDK scans the page for
 * elements with `name="termly-embed"` and injects the published policy by
 * `data-id`. The SDK only runs automatically on initial load, so we call
 * `Termly.initialize()` on mount to re-render when navigating between legal
 * pages client-side. The <Script> is deduped by `id`, so it loads once.
 */
export function TermlyEmbed({ dataId }: { dataId: string }) {
  useEffect(() => {
    window.Termly?.initialize?.();
  }, [dataId]);

  // The Termly SDK locates embeds by the non-standard `name="termly-embed"`
  // attribute, which React's DOM types don't include on <div>. Cast so the
  // attribute is still rendered in the server markup for the SDK to find.
  const embedProps = {
    name: "termly-embed",
    "data-id": dataId,
  } as React.HTMLAttributes<HTMLDivElement>;

  return (
    <>
      <div {...embedProps} />
      <Script
        id="termly-jssdk"
        src="https://app.termly.io/embed-policy.min.js"
        strategy="afterInteractive"
        onReady={() => window.Termly?.initialize?.()}
      />
    </>
  );
}
