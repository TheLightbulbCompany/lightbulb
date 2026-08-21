"use client";

import { useAuth } from "@clerk/nextjs";
import { useCallback, useEffect, useRef, useState } from "react";
import { Chrome, RotateCcw } from "lucide-react";

import { BACKEND_URL } from "@/lib/api";
import { useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { normalizeBrowserViewport, type BrowserViewport } from "./browserViewport";
import {
  browserViewIframeUrl,
  browserViewScreenUrl,
  browserViewSessionUrl,
  browserViewWhoamiUrl,
} from "./browserViewUrl";

type ViewState = "connecting" | "connected" | "disconnected";

const READINESS_POLL_MS = 1000;
const READINESS_TIMEOUT_MS = 30_000;
const AUTH_REFRESH_MS = 45_000;
const RESIZE_DEBOUNCE_MS = 300;

export function WebRtcBrowserDockPanel(): React.ReactElement {
  const { getToken } = useAuth();
  const callRpc = useGatewayRpcMutation();
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const attemptRef = useRef(0);
  const iframeLoadRef = useRef(0);
  const abortsRef = useRef(new Set<AbortController>());
  const readinessPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const readinessTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const resizeRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastViewportRef = useRef<BrowserViewport | null>(null);
  const [state, setState] = useState<ViewState>("connecting");
  const [attempt, setAttempt] = useState(0);
  const [iframeUrl, setIframeUrl] = useState<string | null>(null);

  const clearAttemptWork = useCallback(() => {
    abortsRef.current.forEach((controller) => controller.abort());
    abortsRef.current.clear();
    if (readinessPollRef.current) clearInterval(readinessPollRef.current);
    if (readinessTimeoutRef.current) clearTimeout(readinessTimeoutRef.current);
    if (refreshRef.current) clearInterval(refreshRef.current);
    if (resizeRef.current) clearTimeout(resizeRef.current);
    readinessPollRef.current = null;
    readinessTimeoutRef.current = null;
    refreshRef.current = null;
    resizeRef.current = null;
  }, []);

  const fetchJson = useCallback(async (url: string, init: RequestInit = {}) => {
    const controller = new AbortController();
    abortsRef.current.add(controller);
    try {
      const response = await fetch(url, { ...init, credentials: "include", signal: controller.signal });
      if (!response.ok) throw new Error(`Browser view request failed: ${response.status}`);
      return response.json();
    } finally {
      abortsRef.current.delete(controller);
    }
  }, []);

  useEffect(() => {
    void callRpc("browser.request", { method: "POST", path: "/start" }).catch(() => {});
  }, [attempt, callRpc]);

  useEffect(() => {
    const currentAttempt = ++attemptRef.current;
    let active = true;
    clearAttemptWork();
    lastViewportRef.current = null;
    setState("connecting");
    setIframeUrl(null);

    const bootstrap = async (initial: boolean) => {
      try {
        const token = await getToken();
        if (!token) throw new Error("Missing Clerk token");
        const session = await fetchJson(browserViewSessionUrl(BACKEND_URL), {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!initial || !active || attemptRef.current !== currentAttempt) return;
        if (typeof session.iframe_url !== "string") throw new Error("Missing browser-view iframe URL");
        setIframeUrl(browserViewIframeUrl(BACKEND_URL, session.iframe_url));
        refreshRef.current = setInterval(() => void bootstrap(false), AUTH_REFRESH_MS);
      } catch {
        if (initial && active && attemptRef.current === currentAttempt) setState("disconnected");
      }
    };

    void bootstrap(true);
    return () => {
      active = false;
      clearAttemptWork();
    };
  }, [attempt, clearAttemptWork, fetchJson, getToken]);

  const sendResize = useCallback(() => {
    const rect = wrapperRef.current?.getBoundingClientRect();
    if (!rect) return;
    const viewport = normalizeBrowserViewport(rect.width, rect.height);
    if (
      lastViewportRef.current?.width === viewport.width &&
      lastViewportRef.current.height === viewport.height
    ) return;
    lastViewportRef.current = viewport;
    void fetchJson(browserViewScreenUrl(BACKEND_URL), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(viewport),
    }).catch(() => {
      if (
        lastViewportRef.current?.width === viewport.width &&
        lastViewportRef.current.height === viewport.height
      ) lastViewportRef.current = null;
    });
  }, [fetchJson]);

  useEffect(() => {
    if (state !== "connected" || !wrapperRef.current || typeof ResizeObserver === "undefined") return;
    sendResize();
    const observer = new ResizeObserver(() => {
      if (resizeRef.current) clearTimeout(resizeRef.current);
      resizeRef.current = setTimeout(sendResize, RESIZE_DEBOUNCE_MS);
    });
    observer.observe(wrapperRef.current);
    return () => {
      if (resizeRef.current) clearTimeout(resizeRef.current);
      resizeRef.current = null;
      observer.disconnect();
    };
  }, [sendResize, state]);

  const onIframeLoad = useCallback(() => {
    if (readinessPollRef.current) clearInterval(readinessPollRef.current);
    if (readinessTimeoutRef.current) clearTimeout(readinessTimeoutRef.current);
    readinessPollRef.current = null;
    readinessTimeoutRef.current = null;
    const currentAttempt = attemptRef.current;
    const currentLoad = ++iframeLoadRef.current;
    let done = false;
    const finish = (next: ViewState) => {
      if (done || attemptRef.current !== currentAttempt || iframeLoadRef.current !== currentLoad) return;
      done = true;
      if (readinessPollRef.current) clearInterval(readinessPollRef.current);
      if (readinessTimeoutRef.current) clearTimeout(readinessTimeoutRef.current);
      readinessPollRef.current = null;
      readinessTimeoutRef.current = null;
      setState(next);
    };
    const checkReady = async () => {
      try {
        const whoami = await fetchJson(browserViewWhoamiUrl(BACKEND_URL));
        if (whoami?.state?.is_watching === true) finish("connected");
      } catch {}
    };
    void checkReady();
    readinessPollRef.current = setInterval(() => void checkReady(), READINESS_POLL_MS);
    readinessTimeoutRef.current = setTimeout(() => finish("disconnected"), READINESS_TIMEOUT_MS);
  }, [fetchJson]);

  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  return (
    <div className="relative h-full w-full overflow-hidden bg-[var(--panel)]">
      <div ref={wrapperRef} className="h-full w-full">
        {iframeUrl && (
          <iframe
            key={attempt}
            src={iframeUrl}
            title="Your shared Chrome browser"
            allow="autoplay; clipboard-read; clipboard-write"
            allowFullScreen
            className="h-full w-full border-0"
            onLoad={onIframeLoad}
            onError={() => setState("disconnected")}
          />
        )}
      </div>
      {state !== "connected" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-[#faf7f2] px-6 text-center text-[#1a1a1a]">
          <Chrome size={28} className="text-[#06402b]" />
          {state === "connecting" ? (
            <p className="motion-safe:animate-pulse text-sm text-[#767061]">Connecting to your browser…</p>
          ) : (
            <>
              <p className="max-w-xs text-sm text-[#767061]">Browser view isn’t reachable yet. Retry once it’s ready.</p>
              <button
                onClick={retry}
                className="flex items-center gap-1.5 rounded-md border border-[#e0dbd0] bg-[#f3efe6] px-3 py-1.5 text-sm text-[#1a1a1a] transition-colors hover:bg-[#faf7f2] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#06402b]"
              >
                <RotateCcw size={14} />
                Retry
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}
