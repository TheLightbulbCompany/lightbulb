"use client";

import { useEffect } from "react";

/**
 * Renders a Termly-hosted legal document. The Termly SDK scans the page for
 * elements with `name="termly-embed"` and injects the published policy by
 * `data-id` — once, when its script runs. Client-side navigation to a legal page
 * never re-ran it (and `Termly.initialize()` does not re-scan), so the embed was
 * blank. Appending a fresh script element per mount is the initial-load path
 * every time; the browser re-executes it from cache.
 */
export function TermlyEmbed({ dataId }: { dataId: string }) {
  useEffect(() => {
    const script = document.createElement("script");
    script.src = "https://app.termly.io/embed-policy.min.js";
    script.async = true;
    document.body.appendChild(script);
    return () => script.remove();
  }, [dataId]);

  // The Termly SDK locates embeds by the non-standard `name="termly-embed"`
  // attribute, which React's DOM types don't include on <div>. Cast so the
  // attribute is still rendered in the server markup for the SDK to find.
  const embedProps = {
    name: "termly-embed",
    "data-id": dataId,
  } as React.HTMLAttributes<HTMLDivElement>;

  return <div key={dataId} {...embedProps} />;
}
