"use client";

/* eslint-disable @typescript-eslint/no-explicit-any -- DCV's browser SDK ships without types. */

import { useEffect, useRef, useState } from "react";
import { reportViewerError } from "./dcvViewerError";
import { fitDcvDisplay } from "./dcvSizing";

let nextViewerId = 0;

export function DirectDcvLiveView({ signedUrl, remoteWidth = 1920, remoteHeight = 1080 }: {
  signedUrl: string;
  remoteWidth?: number;
  remoteHeight?: number;
}): React.ReactElement {
  const displayId = useRef(`dcv-display-${++nextViewerId}`);
  const connection = useRef<import("dcv").DcvConnection | null>(null);
  const connecting = useRef(false);
  const viewer = useRef<HTMLDivElement>(null);
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
    void import("dcv").then(({ default: dcv }) => {
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

  if (error) return <p className="p-3 text-sm text-err">{error}</p>;
  return <div ref={viewer} className="flex h-full min-h-0 flex-col">
    <div className="flex shrink-0 items-center gap-2 border-b border-[var(--edge)] px-3 py-2 text-xs">
      <button type="button" onClick={() => void viewer.current?.requestFullscreen?.()} className="rounded border border-[var(--edge)] px-2 py-1">Fullscreen</button>
      <button type="button" onClick={() => upload.current?.click()} className="rounded border border-[var(--edge)] px-2 py-1">Upload file</button>
      <button type="button" onClick={() => void refreshFiles()} className="rounded border border-[var(--edge)] px-2 py-1">Files</button>
      <label className="ml-auto flex items-center gap-1">Quality
        <select value={quality} onChange={(event) => setViewerQuality(Number(event.target.value))} className="rounded border border-[var(--edge)] bg-transparent px-1 py-1">
          <option value={40}>Low</option><option value={80}>Balanced</option><option value={100}>High</option>
        </select>
      </label>
      <input ref={upload} type="file" className="hidden" onChange={(event) => void uploadFile(event)} />
    </div>
    {transferStatus && <p className="shrink-0 px-3 py-1 text-xs text-[var(--midnight-muted)]">{transferStatus}</p>}
    {files.length > 0 && <div className="shrink-0 flex gap-2 overflow-auto px-3 py-1 text-xs">{files.map((file) => <button key={file} type="button" onClick={() => void downloadFile(file)} className="underline">{file}</button>)}</div>}
    <div ref={viewport} className="relative min-h-0 flex-1 overflow-hidden" aria-label="Shared browser display">
      <div id={displayId.current} />
    </div>
  </div>;
}
