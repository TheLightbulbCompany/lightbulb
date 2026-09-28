"use client";

/* eslint-disable @typescript-eslint/no-explicit-any -- DCV's browser SDK ships without types. */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MoreHorizontal } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { BrowserFrameView } from "./BrowserFrameView";
import { reportViewerError } from "./dcvViewerError";
import { fitDcvDisplay, layoutRequester, type Size } from "./dcvSizing";
import { RECONNECT_GIVE_UP_MS, reconnectDelay, recordViewerConnect, type ConnectReason } from "./dcvReconnect";

let nextViewerId = 0;
const ATTACH_POLL_MS = 250;
/** A connect that has painted nothing by now is treated as dropped and retried. */
const CONNECT_TIMEOUT_MS = 15_000;
/** DCV's authentication answers when the session's one live-view slot is taken: AgentCore
 * streams to a single viewer and refuses the next one (SESSION_NOT_AVAILABLE, NO_SESSIONS,
 * AUTHENTICATION_REJECTED). */
const AUTH_REFUSED = new Set([3, 4, 9]);
/** DCV's Connection.CONNECTION_LIMIT_REACHED. */
const CONNECTION_LIMIT_REACHED = 12;
const errorCode = (reason: unknown) => (reason as { code?: unknown } | null)?.code;

export type ViewerStatus = "connecting" | "live" | "reconnecting" | "failed" | "elsewhere";

export function DirectDcvLiveView({ signedUrl, sessionId, resign, remoteWidth = 1920, remoteHeight = 1080, menuHost, busy = false, resizeRemote, onStatus }: {
  /** Used for the first connect; a newer one passed later is kept for reconnects, never forces one. */
  signedUrl: string;
  /** The browser session this viewer shows. The panel remounts the viewer when it changes. */
  sessionId?: string | null;
  /** A freshly signed URL for a reconnect (the first may have expired); null: use `signedUrl`. */
  resign?: () => Promise<string | null>;
  /** The session's start viewport: what the stream is scaled by until the server reports its layout. */
  remoteWidth?: number;
  remoteHeight?: number;
  /** Where the viewer's `⋯` menu renders (the panel's bar); none renders no menu. */
  menuHost?: HTMLElement | null;
  /** Hold remote resizes: an agent run is driving the browser, or it is changing control. */
  busy?: boolean;
  /** Resize the remote browser's window; true once it is that size. Absent: never resize. */
  resizeRemote?: (size: Size) => Promise<boolean>;
  onStatus?: (status: ViewerStatus) => void;
}): React.ReactElement {
  const displayId = useRef(`dcv-display-${++nextViewerId}`);
  const connection = useRef<import("dcv").DcvConnection | null>(null);
  const connecting = useRef(false);
  const viewport = useRef<HTMLDivElement>(null);
  const upload = useRef<HTMLInputElement>(null);
  const [status, setStatus] = useState<ViewerStatus>("connecting");
  const onStatusRef = useRef(onStatus);
  onStatusRef.current = onStatus;
  useEffect(() => onStatusRef.current?.(status), [status]);
  const [retryNonce, setRetryNonce] = useState(0);
  const signedUrlRef = useRef(signedUrl);
  signedUrlRef.current = signedUrl;
  const resignRef = useRef(resign);
  resignRef.current = resign;
  const [quality, setQuality] = useState(80);
  const [files, setFiles] = useState<string[]>([]);
  const [transferStatus, setTransferStatus] = useState<string | null>(null);
  // The remote display's real size, as the server last reported it.
  const [remote, setRemote] = useState<Size>({ width: remoteWidth, height: remoteHeight });
  const remoteRef = useRef(remote);
  remoteRef.current = remote;
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const resizeRemoteRef = useRef(resizeRemote);
  resizeRemoteRef.current = resizeRemote;
  const layout = useRef<ReturnType<typeof layoutRequester> | null>(null);

  // ONE connection per browser session: the panel keys this viewer by session id, and nothing
  // else reconnects it. A control change, an autosave or a re-signed URL used to tear the
  // stream down and rebuild it (the flicker, prod 2026-09-26). The newest signed URL is kept
  // for the next reconnect, and a drop reconnects quietly with backoff, over the last frame.
  useEffect(() => {
    let disposed = false;
    let attempt = 0;
    let droppedAt = 0;
    // Each connect attempt's id; a callback from an older attempt is ignored.
    let epoch = 0;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let watchdog: ReturnType<typeof setTimeout> | undefined;
    const sessionKey = sessionId ?? "unknown";
    setStatus("connecting");

    // `refused`: someone else (another person or device) holds the one slot. Not a drop:
    // retrying would only be refused again, so wait for the person to ask.
    const drop = (id: number, stage: string, reason?: unknown, refused = false) => {
      if (disposed || id !== epoch) return;
      epoch += 1;
      clearTimeout(watchdog);
      connecting.current = false;
      const dead = connection.current;
      connection.current = null;
      dead?.captureClipboardEvents?.(false, window);
      dead?.disconnect?.();
      clearTimeout(retryTimer);
      if (refused) return setStatus("elsewhere");
      if (reason !== undefined) reportViewerError(stage, reason);
      droppedAt ||= Date.now();
      if (Date.now() - droppedAt >= RECONNECT_GIVE_UP_MS) {
        setStatus("failed");
        return;
      }
      setStatus("reconnecting");
      retryTimer = setTimeout(() => void open("drop"), reconnectDelay(attempt++));
    };

    const open = async (why: ConnectReason) => {
      if (disposed) return;
      const id = ++epoch;
      recordViewerConnect(sessionKey, why);
      let stage = "SDK import";
      try {
        const { default: dcv } = await import("dcv");
        // DCV looks the display element up by id when the stream starts, and a dock tab that is
        // not on screen is detached from the document: connecting then fails with "Unable to
        // find the element" and the viewer stays blank for good (prod 2026-09-26). Start only
        // once the element is in the document.
        while (!disposed && !document.getElementById(displayId.current)?.isConnected) {
          await new Promise((resolve) => setTimeout(resolve, ATTACH_POLL_MS));
        }
        // An attempt that never paints a frame (a hung auth or socket) counts as a drop.
        watchdog = setTimeout(() => drop(id, "timeout"), CONNECT_TIMEOUT_MS);
        // A reconnect signs afresh: the URL it was first given may be past its 300 s.
        const url = why === "session" ? signedUrlRef.current : (await resignRef.current?.()) ?? signedUrlRef.current;
        if (disposed) return;
        stage = "authentication setup";
        dcv.authenticate(url, {
          promptCredentials: () => {},
          error: (_auth: unknown, reason: unknown) => {
            // DCV can close a completed auth websocket after connect starts. That
            // callback must not tear down an active display.
            if (!connecting.current && !connection.current) {
              drop(id, `authentication (code ${String(errorCode(reason))})`, reason, AUTH_REFUSED.has(errorCode(reason) as number));
            }
          },
          httpExtraSearchParams: () => new URL(url).searchParams,
          success: async (_auth: unknown, result: Array<{ sessionId?: string; authToken?: string }>) => {
            if (disposed || id !== epoch) return;
            const credentials = result[0] ?? {};
            if (!credentials.sessionId || !credentials.authToken) return drop(id, "credentials");
            try {
              connecting.current = true;
              const connected = await dcv.connect({
                url,
                sessionId: credentials.sessionId,
                authToken: credentials.authToken,
                divId: displayId.current,
                baseUrl: "/nice-dcv-web-client-sdk/dcvjs-esm",
                enabledChannels: ["display", "input", "clipboard", "filestorage"],
                callbacks: {
                  disconnect: () => drop(id, "disconnect"),
                  // The display channel is live only from the first frame; a layout request
                  // before it is rejected.
                  firstFrame: () => {
                    if (disposed || id !== epoch) return;
                    clearTimeout(watchdog);
                    attempt = 0;
                    droppedAt = 0;
                    setStatus("live");
                    const box = viewport.current?.getBoundingClientRect();
                    if (box) layout.current?.schedule(box.width, box.height);
                  },
                  // DCV calls every callback with the connection first.
                  displayLayout: (_conn: unknown, width: number, height: number) => {
                    if (!disposed && width > 0 && height > 0) setRemote({ width, height });
                  },
                  httpExtraSearchParams: () => new URL(url).searchParams,
                },
              });
              connecting.current = false;
              if (disposed || id !== epoch) return void connected.disconnect?.();
              connection.current = connected;
              connected.captureClipboardEvents?.(true, window);
              connected.enableDisplayQualityUpdates?.(true);
              connected.setDisplayQuality?.(80, 80);
            } catch (reason) {
              drop(id, `connection (code ${String(errorCode(reason))})`, reason, errorCode(reason) === CONNECTION_LIMIT_REACHED);
            }
          },
        });
      } catch (reason) {
        drop(id, stage, reason);
      }
    };

    void open(retryNonce ? "retry" : "session");
    return () => {
      disposed = true;
      clearTimeout(retryTimer);
      clearTimeout(watchdog);
      connecting.current = false;
      connection.current?.captureClipboardEvents?.(false, window);
      connection.current?.disconnect?.();
      connection.current = null;
    };
  }, [retryNonce, sessionId]);

  // Make the REMOTE browser the panel's size, so the page lays out like a normal browser
  // window of this size (text at its normal size, no letterbox) instead of a downscaled
  // 1920x1080. Two halves: the backend sizes Chrome's window (it never follows the display on
  // its own), then DCV resizes the display. Never while an agent run drives the browser.
  useEffect(() => {
    layout.current = layoutRequester({
      current: () => remoteRef.current,
      busy: () => busyRef.current,
      request: (size) => {
        const resize = resizeRemoteRef.current;
        if (!resize || !connection.current?.requestDisplayLayout) return;
        void resize(size).then((resized) => {
          const { width, height } = size;
          if (!resized || !connection.current) return;
          return connection.current.requestDisplayLayout([{ name: "Main Display", rect: { x: 0, y: 0, width, height }, primary: true }]);
        }).catch(() => undefined); // declined: the stream stays usable, scaled to fit
      },
    });
    const container = viewport.current;
    const observer = new ResizeObserver(([entry]) => layout.current?.schedule(entry.contentRect.width, entry.contentRect.height));
    if (container) observer.observe(container);
    return () => {
      observer.disconnect();
      layout.current?.cancel();
      layout.current = null;
    };
  }, []);

  // A request dropped while busy is re-made once the browser settles.
  useEffect(() => {
    const box = viewport.current?.getBoundingClientRect();
    if (!busy && box) layout.current?.schedule(box.width, box.height);
  }, [busy]);

  useEffect(() => {
    const host = document.getElementById(displayId.current);
    const container = viewport.current;
    if (!host || !container) return;
    const { width: remoteWidth, height: remoteHeight } = remote;

    const applyFit = () => {
      const { width, height } = container.getBoundingClientRect();
      const { scale, offsetX, offsetY } = fitDcvDisplay(width, height, remoteWidth, remoteHeight);
      if (!scale) return;
      const next = {
        height: `${remoteHeight}px`,
        left: `${offsetX}px`,
        position: "absolute",
        top: `${offsetY}px`,
        transform: `scale(${scale})`,
        "transform-origin": "top left",
        width: `${remoteWidth}px`,
      };
      for (const [property, value] of Object.entries(next)) {
        if (host.style.getPropertyValue(property) !== value) host.style.setProperty(property, value);
      }
    };

    const resizeObserver = new ResizeObserver(applyFit);
    const mutationObserver = new MutationObserver(applyFit);
    resizeObserver.observe(container);
    mutationObserver.observe(host, { attributes: true, attributeFilter: ["style"] });
    applyFit();
    return () => {
      resizeObserver.disconnect();
      mutationObserver.disconnect();
    };
  }, [remote]);

  const refreshFiles = async () => {
    try {
      const explorer = await connection.current?.getFileExplorer?.();
      const listed = await explorer?.list?.("/");
      const listing = listed as { files?: unknown[]; items?: unknown[] } | null | undefined;
      const entries = Array.isArray(listed) ? listed : listing?.files ?? listing?.items ?? [];
      const paths = entries
        .map((entry: unknown) => typeof entry === "string" ? entry : (entry as { path?: string; name?: string }).path ?? (entry as { name?: string }).name)
        .filter((path): path is string => Boolean(path));
      setFiles(paths);
    } catch {
      setTransferStatus("File storage is unavailable for this browser session.");
    }
  };

  const uploadFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const explorer = await connection.current?.getFileExplorer?.();
      await explorer?.storeFile?.(file, "/");
      setTransferStatus(`${file.name} uploaded.`);
      await refreshFiles();
    } catch {
      setTransferStatus("Could not upload that file.");
    } finally {
      event.target.value = "";
    }
  };

  const setViewerQuality = (next: number) => {
    setQuality(next);
    connection.current?.setDisplayQuality?.(next, next);
  };

  const downloadFile = async (path: string) => {
    try {
      const explorer = await connection.current?.getFileExplorer?.();
      await explorer?.retrieveFile?.(path);
    } catch {
      setTransferStatus("Could not download that file.");
    }
  };

  const item = "w-full rounded px-2 py-1 text-left hover:bg-cream-deep";
  const menu = menuHost && createPortal(
    <Popover onOpenChange={(open) => { if (open) void refreshFiles(); }}>
      <PopoverTrigger aria-label="Browser options" className="rounded-md px-1.5 py-0.5 hover:bg-cream-deep"><MoreHorizontal size={14} /></PopoverTrigger>
      <PopoverContent align="end" className="w-56 p-1 text-xs">
        <button type="button" onClick={() => void viewport.current?.requestFullscreen?.()} className={item}>Fullscreen</button>
        <button type="button" onClick={() => upload.current?.click()} className={item}>Upload file</button>
        <label className="flex items-center justify-between px-2 py-1">Quality
          <select value={quality} onChange={(event) => setViewerQuality(Number(event.target.value))} className="rounded border border-[var(--edge)] bg-transparent px-1 py-0.5">
            <option value={40}>Low</option><option value={80}>Balanced</option><option value={100}>High</option>
          </select>
        </label>
        <p className="border-t border-[var(--edge)] px-2 pb-0.5 pt-1.5 text-[var(--midnight-muted)]">Files</p>
        {files.length ? files.map((file) => <button key={file} type="button" onClick={() => void downloadFile(file)} className={`${item} truncate`}>{file}</button>)
          : <p className="px-2 py-1 text-[var(--midnight-muted)]">No files yet.</p>}
        {transferStatus && <p className="px-2 py-1 text-[var(--midnight-muted)]">{transferStatus}</p>}
      </PopoverContent>
    </Popover>,
    menuHost,
  );

  return <>
    {menu}
    {/* Absolute, not h-full: it takes the panel's own box in every engine, so the fit and the
        layout request always have a real width and height. The ground is the pane's own
        colour, never black, for the moment a resize is in flight. */}
    <div ref={viewport} className="absolute inset-0 overflow-hidden bg-[var(--cream)]" aria-label="Shared browser display">
      {/* The host stays mounted through a reconnect, so the last frame stays on screen. */}
      <div id={displayId.current} className={status === "reconnecting" || status === "failed" ? "opacity-50" : undefined} />
      {/* Another screen holds the one live view: an agent's tab as live frames, with input,
          and the live view back on "Take live control" once that screen lets go. */}
      {status === "elsewhere" && (
        <BrowserFrameView interactive onTakeControl={() => setRetryNonce((value) => value + 1)} empty={
          <div role="status" className="flex flex-col items-center gap-2 p-6 text-center text-sm text-[var(--midnight-muted)]">
            <p>Being watched elsewhere</p>
            <p className="max-w-sm text-xs">This browser streams to one screen at a time. An agent&apos;s page shows here live while it browses.</p>
          </div>
        } />
      )}
      {status === "reconnecting" && (
        <p role="status" className="absolute right-2 top-2 rounded-full bg-[var(--midnight)] px-2.5 py-1 text-xs text-[var(--cream)]">Reconnecting…</p>
      )}
      {status === "failed" && (
        <div role="alert" className="absolute inset-x-0 top-0 flex items-center justify-between gap-2 bg-[var(--cream)] p-3 text-sm text-err">
          Browser viewer disconnected.
          <button type="button" onClick={() => setRetryNonce((value) => value + 1)} className="rounded-md border border-[var(--edge)] px-2 py-0.5 text-xs text-[var(--midnight)]">Retry</button>
        </div>
      )}
      <input ref={upload} type="file" className="hidden" onChange={(event) => void uploadFile(event)} />
    </div>
  </>;
}
