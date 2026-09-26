"use client";

import { useEffect, useState } from "react";

const CHECK_MS = 5 * 60_000;
const BLOCKED_RECHECK_MS = 30_000;
const CHUNK_RELOAD_KEY = "lightbulb-chunk-reload-at";

/**
 * True while reloading would cost the user something: text typed but not sent
 * (drafts persist, but a reload mid-sentence still loses the cursor and the
 * moment), an open dialog, a live run on screen, or the shared browser under
 * this user's control. The last two mark themselves with `data-reload-blocker`.
 */
export function reloadBlocked(doc: Document = document): boolean {
  if (doc.querySelector("[data-reload-blocker], [role=dialog], [role=alertdialog], [aria-modal=true]")) return true;
  for (const field of doc.querySelectorAll("textarea")) if (field.value.trim()) return true;
  for (const field of doc.querySelectorAll<HTMLElement>("[contenteditable=true]")) if (field.textContent?.trim()) return true;
  return false;
}

/** A lazy chunk from the previous deploy that no longer exists. */
export function isChunkLoadError(reason: unknown): boolean {
  if (!reason || typeof reason !== "object") return false;
  const { name, message } = reason as { name?: unknown; message?: unknown };
  return name === "ChunkLoadError"
    || (typeof message === "string" && /Loading (CSS )?chunk \S+ failed|Failed to fetch dynamically imported module|Importing a module script failed/.test(message));
}

/**
 * Reload once for a missing chunk. The timestamp stops a loop when the chunk is
 * missing for some other reason than a deploy.
 */
export function reloadForChunkError(): void {
  try {
    const last = Number(sessionStorage.getItem(CHUNK_RELOAD_KEY));
    if (last && Date.now() - last < 60_000) return;
    sessionStorage.setItem(CHUNK_RELOAD_KEY, String(Date.now()));
  } catch {
    // No storage: reload anyway; a broken page is worse than a rare loop.
  }
  window.location.reload();
}

/**
 * Notices a new deployment and reloads at a safe moment; while it is not safe,
 * shows a pill and keeps checking. The desktop app is the same bundle in a
 * WKWebView, so this covers it too.
 */
export function DeployRefresh() {
  const [stale, setStale] = useState(false);

  useEffect(() => {
    const loaded = process.env.NEXT_PUBLIC_BUILD_ID;
    if (!loaded || loaded === "local") return;
    let done = false;
    const check = async () => {
      if (done || document.visibilityState !== "visible") return;
      try {
        const res = await fetch("/api/version", { cache: "no-store" });
        const { buildId } = await res.json() as { buildId?: string };
        if (buildId && buildId !== "local" && buildId !== loaded) {
          done = true;
          setStale(true);
        }
      } catch {
        // Offline or mid-deploy: the next check tries again.
      }
    };
    const interval = window.setInterval(check, CHECK_MS);
    window.addEventListener("focus", check);
    document.addEventListener("visibilitychange", check);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", check);
      document.removeEventListener("visibilitychange", check);
    };
  }, []);

  useEffect(() => {
    const onError = (event: ErrorEvent) => { if (isChunkLoadError(event.error)) reloadForChunkError(); };
    const onRejection = (event: PromiseRejectionEvent) => { if (isChunkLoadError(event.reason)) reloadForChunkError(); };
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
    };
  }, []);

  const [blocked, setBlocked] = useState(false);
  useEffect(() => {
    if (!stale) return;
    const attempt = () => {
      if (reloadBlocked()) setBlocked(true);
      else window.location.reload();
    };
    attempt();
    const interval = window.setInterval(attempt, BLOCKED_RECHECK_MS);
    return () => window.clearInterval(interval);
  }, [stale]);

  if (!blocked) return null;
  return (
    <div role="status" className="fixed left-1/2 top-2 z-[100] flex -translate-x-1/2 items-center gap-2 rounded-full bg-midnight py-1 pl-3 pr-1 text-xs text-cream shadow-md">
      Lightbulb updated
      <button type="button" onClick={() => window.location.reload()} className="rounded-full bg-cream px-2.5 py-0.5 font-medium text-midnight">
        Reload
      </button>
    </div>
  );
}
