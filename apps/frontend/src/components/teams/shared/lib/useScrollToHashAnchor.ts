"use client";

import { useEffect } from "react";

// Deep-link scroll for always-rendered rows (comments, work products) whose
// `id` is set to the URL hash a link points at (`#comment-<id>`,
// `#work-product-<id>`) — mirrors DocumentRow.tsx's own `#document-<key>`
// handling, minus the expand step: a document's body is collapsed by default
// so that effect also opens it first, but a comment/work-product row is
// already in the DOM once its list has loaded, so this just scrolls.
function scrollHashIntoView(): void {
  if (typeof window === "undefined") return;
  const raw = window.location.hash.replace(/^#/, "");
  if (!raw) return;
  let id = raw;
  try {
    id = decodeURIComponent(raw);
  } catch {
    /* keep raw */
  }
  (document.getElementById(id) ?? document.getElementById(raw))?.scrollIntoView({
    block: "start",
    behavior: "smooth",
  });
}

/** Call once the anchor rows are actually rendered (e.g. once their data has
 *  loaded) — `ready` gates both the initial check and the hashchange listener
 *  so a pushState navigation into a still-loading list doesn't scroll to
 *  nothing. */
export function useScrollToHashAnchor(ready: boolean): void {
  useEffect(() => {
    if (!ready) return;
    scrollHashIntoView();
    window.addEventListener("hashchange", scrollHashIntoView);
    return () => window.removeEventListener("hashchange", scrollHashIntoView);
  }, [ready]);
}
