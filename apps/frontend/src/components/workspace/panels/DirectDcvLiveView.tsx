"use client";

/* eslint-disable @typescript-eslint/no-explicit-any -- DCV's browser SDK ships without types. */

import { useEffect, useRef, useState } from "react";
import { navigateShell } from "@/lib/shellNav";
import { createPortal } from "react-dom";
import { MoreHorizontal } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { reportViewerError } from "./dcvViewerError";
import { fitDcvDisplay } from "./dcvSizing";

let nextViewerId = 0;
const ATTACH_POLL_MS = 250;

export function DirectDcvLiveView({ signedUrl, remoteWidth = 1920, remoteHeight = 1080, menuHost }: {
  signedUrl: string;
  remoteWidth?: number;
  remoteHeight?: number;
  /** Where the viewer's `⋯` menu renders (the panel's bar); none renders no menu. */
  menuHost?: HTMLElement | null;
}): React.ReactElement {
  const displayId = useRef(`dcv-display-${++nextViewerId}`);
  const connection = useRef<import("dcv").DcvConnection | null>(null);
  const connecting = useRef(false);
  const viewport = useRef<HTMLDivElement>(null);
  const upload = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [quality, setQuality] = useState(80);
  const [files, setFiles] = useState<string[]>([]);
  const [transferStatus, setTransferStatus] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    let stage = "SDK import";
    setError(null);
    connecting.current = false;
    void import("dcv").then(async ({ default: dcv }) => {
      // DCV looks the display element up by id when the stream starts, and a dock tab that is
      // not on screen is detached from the document: connecting then fails with "Unable to find
      // the element" and the viewer stays blank for good (prod 2026-09-26). Start only once the
      // element is in the document.
      while (!disposed && !document.getElementById(displayId.current)?.isConnected) {
        await new Promise((resolve) => setTimeout(resolve, ATTACH_POLL_MS));
      }
      if (disposed) return;
      stage = "authentication setup";
      dcv.authenticate(signedUrl, {
        promptCredentials: () => {},
        error: (reason: unknown) => {
          // DCV can close a completed auth websocket after connect starts. That
          // callback must not unmount the host element under an active display.
          if (!disposed && !connecting.current && !connection.current) {
            reportViewerError("authentication", reason);
            setError("Could not authenticate the browser viewer.");
          }
        },
        httpExtraSearchParams: () => new URL(signedUrl).searchParams,
        success: async (_auth: unknown, result: Array<{ sessionId?: string; authToken?: string }>) => {
          if (disposed) return;
          const credentials = result[0] ?? {};
          if (!credentials.sessionId || !credentials.authToken) {
            if (!disposed) setError("Browser viewer credentials were unavailable.");
            return;
          }
          try {
            if (disposed) return;
            connecting.current = true;
            const connected = await dcv.connect({
            url: signedUrl,
            sessionId: credentials.sessionId,
            authToken: credentials.authToken,
            divId: displayId.current,
            baseUrl: "/nice-dcv-web-client-sdk/dcvjs-esm",
            enabledChannels: ["display", "input", "clipboard", "filestorage"],
              callbacks: {
              disconnect: () => !disposed && setError("Browser viewer disconnected."),
              httpExtraSearchParams: () => new URL(signedUrl).searchParams,
              },
            });
            connecting.current = false;
            if (disposed) connected.disconnect?.();
            else {
              connection.current = connected;
              connected.requestDisplayLayout?.([{ name: "Main Display", rect: { x: 0, y: 0, width: remoteWidth, height: remoteHeight }, primary: true }]);
              connected.captureClipboardEvents?.(true, window);
              connected.enableDisplayQualityUpdates?.(true);
              connected.setDisplayQuality?.(80, 80);
            }
          } catch (reason) {
            connecting.current = false;
            if (!disposed) {
              reportViewerError("connection", reason);
              setError("Could not connect the browser viewer.");
            }
          }
        },
      });
    }).catch((reason) => {
      if (!disposed) {
        reportViewerError(stage, reason);
        setError("Could not start the browser viewer.");
      }
    });
    return () => {
      disposed = true;
      connecting.current = false;
      connection.current?.captureClipboardEvents?.(false, window);
      connection.current?.disconnect?.();
      connection.current = null;
    };
  }, [remoteHeight, remoteWidth, signedUrl]);

  useEffect(() => {
    const host = document.getElementById(displayId.current);
    const container = viewport.current;
    if (!host || !container) return;

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
  }, [remoteHeight, remoteWidth]);

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
        {/* The passwords this browser fills for agents live in Connections. */}
        <button type="button" onClick={() => navigateShell("/chat?panel=connections#website-logins")} className={item}>Website logins</button>
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

  if (error) return <p className="p-3 text-sm text-err">{error}</p>;
  return <>
    {menu}
    {/* Absolute, not h-full: it takes the panel's own box in every engine, so the fit below
        always has a real width and height to scale the 1920x1080 remote into. */}
    <div ref={viewport} className="absolute inset-0 overflow-hidden" aria-label="Shared browser display">
      <div id={displayId.current} />
      <input ref={upload} type="file" className="hidden" onChange={(event) => void uploadFile(event)} />
    </div>
  </>;
}
