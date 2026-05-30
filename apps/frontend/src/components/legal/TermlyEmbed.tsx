"use client";

import { useEffect } from "react";

const TERMLY_SRC = "https://app.termly.io/embed-policy.min.js";
const TERMLY_SCRIPT_ID = "termly-jssdk";

/**
 * Renders a Termly-hosted legal document. The Termly SDK scans the page for
 * elements with `name="termly-embed"` and injects the published policy by
 * `data-id`.
 *
 * The SDK only scans when its script first executes; it does not expose a
 * public re-init method. On a Next.js client-side navigation the script is
 * already loaded and React reuses the embed node, so a fresh route would
 * otherwise render blank. To handle this we (1) key the embed `<div>` by
 * `dataId` so each route gets a brand-new, unprocessed node, and (2) remove
 * and re-inject the SDK on mount so it re-scans and renders the current doc.
 */
export function TermlyEmbed({ dataId }: { dataId: string }) {
  useEffect(() => {
    document.getElementById(TERMLY_SCRIPT_ID)?.remove();
    const script = document.createElement("script");
    script.id = TERMLY_SCRIPT_ID;
    script.src = TERMLY_SRC;
    document.body.appendChild(script);
  }, [dataId]);

  // The SDK locates embeds by the non-standard `name="termly-embed"`
  // attribute, which React's DOM types don't include on <div>. Cast so the
  // attribute is still rendered for the SDK to find.
  const embedProps = {
    name: "termly-embed",
    "data-id": dataId,
  } as React.HTMLAttributes<HTMLDivElement>;

  return <div key={dataId} {...embedProps} />;
}
