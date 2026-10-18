"use client";

import { useEffect, useState } from "react";
import { blobText } from "./readBlob";

export const HTML_MAX_BYTES = 2 * 1024 * 1024;

/**
 * A page an agent wrote, shown in a frame with no origin of ours: scripts may run (owner
 * decision), nothing else is allowed — no same-origin, no forms, no pop-ups, no navigation.
 * The text comes from the authenticated fetch; the frame never loads an API URL.
 */
export function HtmlPreview({ blob, fileName }: { blob: Blob; fileName: string }) {
  const [html, setHtml] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    if (blob.size > HTML_MAX_BYTES) return;
    void blobText(blob).then((t) => { if (live) setHtml(t); }, () => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [blob]);
  if (blob.size > HTML_MAX_BYTES) return <p role="alert" className="p-4 text-ui text-muted-foreground">{fileName} is too large to preview. Download it instead.</p>;
  if (failed) return <p role="alert" className="p-4 text-ui text-muted-foreground">Could not preview {fileName}. Download it instead.</p>;
  if (html === null) return <p className="p-4 text-ui text-muted-foreground">Loading {fileName}…</p>;
  return <iframe title={fileName} sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={html} className="h-full w-full border-0" style={{ background: "var(--plane-pane)" }} />;
}
