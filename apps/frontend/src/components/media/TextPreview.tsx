"use client";

import { useEffect, useState } from "react";
import { blobText } from "./readBlob";
import { MarkdownView } from "@/components/voice/MarkdownView";

export const TEXT_MAX_BYTES = 1024 * 1024;

export function TextPreview({ blob, fileName, markdown }: { blob: Blob; fileName: string; markdown: boolean }) {
  const [text, setText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let live = true;
    if (blob.size > TEXT_MAX_BYTES) return;
    void blobText(blob).then((t) => { if (live) setText(t); }, () => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [blob]);
  if (blob.size > TEXT_MAX_BYTES) return <p role="alert" className="p-4 text-ui text-muted-foreground">{fileName} is too large to preview. Download it instead.</p>;
  if (failed) return <p role="alert" className="p-4 text-ui text-muted-foreground">Could not preview {fileName}. Download it instead.</p>;
  if (text === null) return <p className="p-4 text-ui text-muted-foreground">Loading {fileName}…</p>;
  if (markdown) return <div className="p-4"><MarkdownView skipHtml>{text}</MarkdownView></div>;
  return <pre className="h-full overflow-auto p-4 text-caption font-mono whitespace-pre-wrap" style={{ background: "var(--plane-pane)" }}>{text}</pre>;
}
