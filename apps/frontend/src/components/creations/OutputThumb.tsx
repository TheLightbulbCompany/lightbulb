"use client";

// The face of an output: an app's own page (live, scaled into the tile), an image itself, a doc's title and first
// lines, else its glyph with the extension or host. Glyphs are lucide, one stroke.

import { useEffect, useRef, useState, type ReactElement } from "react";
import useSWR from "swr";
import { AppWindow, ArrowUpRight, FileText, File as FileIcon, Mail, Sparkles, type LucideIcon } from "lucide-react";
import type { OutputKind, TaskOutput } from "@lightbulb/shared";
import type { AppRow } from "@/hooks/useApps";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { blobText } from "@/components/media/readBlob";
import { docPreview, readableName } from "./docText";
import { extensionOf, hostOf } from "./openOutput";

const KIND_ICON: Record<OutputKind, LucideIcon> = {
  app: AppWindow, document: FileText, file: FileIcon, mail: Mail, link: ArrowUpRight, pr: ArrowUpRight, capability: Sparkles,
};

const TEXT_FILES = new Set(["MD", "TXT", "PDF", "DOC", "DOCX", "RTF", "CSV"]);

/** An output's glyph: by kind, and a text file (a .md a task wrote as a file) as a document. */
export function iconOf(o: Pick<TaskOutput, "kind" | "title" | "ref">, size = 14, strokeWidth = 1.75): ReactElement {
  const Icon = o.kind === "file" && TEXT_FILES.has(extensionOf(o)) ? FileText : KIND_ICON[o.kind] ?? FileIcon;
  return <Icon size={size} strokeWidth={strokeWidth} aria-hidden="true" />;
}

/** A short-lived address for an image a task made (presigned; refetched by SWR when the tile comes back). */
function useAttachmentImage(ref: Record<string, string>, on: boolean): string | undefined {
  const { messaging } = useLiveEvents();
  const { data } = useSWR(on && ref.roomId && ref.attachmentId ? ["attachment-url", ref.roomId, ref.attachmentId] : null,
    ([, room, id]: [string, string, string]) => messaging.attachmentUrl(room, id), { shouldRetryOnError: false, revalidateOnFocus: false });
  return data?.url;
}

/** A doc a task wrote as text (markdown or plain), which its tile previews. */
export function isTextDoc(o: Pick<TaskOutput, "kind" | "title" | "ref">): boolean {
  if (o.kind !== "document" && o.kind !== "file") return false;
  const type = (o.ref?.contentType ?? "").split(";")[0].trim().toLowerCase();
  return type === "text/markdown" || type === "text/plain" || (!type && (extensionOf(o) === "MD" || extensionOf(o) === "TXT"));
}

/** A doc's heading and first lines: its first 4 KB through the room-checked attachment route, once it is on screen. */
function useDocPreview(ref: Record<string, string>, on: boolean) {
  const { messaging } = useLiveEvents();
  const face = useRef<HTMLSpanElement>(null);
  // No IntersectionObserver (an old browser, a test): read at once; the cap still holds.
  const [seen, setSeen] = useState(() => typeof IntersectionObserver === "undefined");
  useEffect(() => {
    const el = face.current;
    if (!on || seen || !el) return;
    const io = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) { setSeen(true); io.disconnect(); } }, { rootMargin: "200px" });
    io.observe(el);
    return () => io.disconnect();
  }, [on, seen]);
  const { data } = useSWR(on && seen && ref.roomId && ref.attachmentId ? ["attachment-preview", ref.roomId, ref.attachmentId] : null,
    async ([, room, id]: [string, string, string]) => docPreview(await blobText((await messaging.downloadAttachment(room, id)).slice(0, 4096))),
    { shouldRetryOnError: false, revalidateOnFocus: false });
  return { face, preview: data };
}

export function OutputThumb({ output, app, size = "tile", frame = true }: {
  output: Pick<TaskOutput, "kind" | "title" | "ref">;
  /** The app's row, for its page; without one an app shows its glyph. */
  app?: AppRow;
  /** "tile": Creations' 16:10 tile, with the extension or host under the glyph. "mini": Made here's 88×56. */
  size?: "tile" | "mini";
  /** false: never frame the app's page or preview the doc (the grid shows only its first few). */
  frame?: boolean;
}): ReactElement {
  const image = (output.kind === "file" || output.kind === "document") && output.ref?.contentType?.startsWith("image/");
  const src = useAttachmentImage(output.ref ?? {}, !!image);
  const { face, preview } = useDocPreview(output.ref ?? {}, frame && isTextDoc(output));
  // Only a public app frames as itself: any other shows its sign-in door, so it gets the glyph.
  const page = frame && output.kind === "app" && app?.open_to === "public" && app.can_open !== false ? app.live_url ?? app.preview_url : null;
  if (page && /^https?:\/\//.test(page)) {
    // A live preview, not a control: lazy (only tiles on screen load), sandboxed, and out of the tab order.
    return <span className="output-thumb output-thumb--page" data-size={size}>
      <iframe src={page} title="" aria-hidden="true" tabIndex={-1} loading="lazy" sandbox="allow-scripts allow-same-origin" referrerPolicy="no-referrer" />
    </span>;
  }
  if (src) {
    return <span className="output-thumb" data-size={size}>
      {/* eslint-disable-next-line @next/next/no-img-element -- a presigned, short-lived URL: next/image can't proxy it */}
      <img src={src} alt="" loading="lazy" />
    </span>;
  }
  if (preview && (preview.heading || preview.lines.length)) {
    // A small page: the doc's title and its opening lines, read-only, so the tile says what it is.
    return <span ref={face} className="output-thumb output-thumb--doc" data-size={size}>
      <span className="output-thumb__doc-title">{preview.heading ?? readableName(output.ref?.fileName || output.title)}</span>
      {preview.lines.map((line, i) => <span key={i} className="output-thumb__doc-line">{line}</span>)}
    </span>;
  }
  const note = output.kind === "link" || output.kind === "pr" ? hostOf(output) : output.kind === "document" || output.kind === "file" ? extensionOf(output) : "";
  return <span ref={face} className="output-thumb output-thumb--glyph" data-size={size}>
    {iconOf(output, size === "mini" ? 18 : 24, 1.5)}
    {size === "tile" && note && <span className="output-thumb__note">{note}</span>}
  </span>;
}
