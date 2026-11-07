"use client";

// The face of an output: an app's own page (live, scaled into the tile), an image itself, a doc's title and first
// lines, else its glyph with the extension or host. Glyphs are lucide, one stroke.

import { useEffect, useRef, useState, type ReactElement } from "react";
import useSWR from "swr";
import { AppWindow, ArrowUpRight, FileText, File as FileIcon, Mail, Sparkles, type LucideIcon } from "lucide-react";
import type { OutputKind, TaskOutput } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { blobBytes, blobText } from "@/components/media/readBlob";
import { DOCX, DOCX_MAX_BYTES, docxFace } from "@/components/media/DocxPreview";
import { docPreview, readableName, textName } from "./docText";
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

/** A Word file a task made: its tile reads its words (W19). */
export function isDocx(o: Pick<TaskOutput, "kind" | "title" | "ref">): boolean {
  if (o.kind !== "document" && o.kind !== "file") return false;
  const type = (o.ref?.contentType ?? "").split(";")[0].trim().toLowerCase();
  return type === DOCX || (!type && extensionOf(o) === "DOCX");
}

/** A doc's heading and first lines: its first 4 KB (`?head=`, a ranged read) through the room-checked attachment route,
 *  once the tile is on screen; a .docx (a zip, its directory at the end) whole, up to the viewer's cap. A tile that goes
 *  away cancels its read. */
function useDocPreview(ref: Record<string, string>, on: boolean, docx: boolean) {
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
  const [preview, setPreview] = useState<{ key: string; value: ReturnType<typeof docPreview> } | null>(null);
  const { roomId, attachmentId } = ref;
  const key = `${roomId}:${attachmentId}`;
  useEffect(() => {
    if (!on || !seen || !roomId || !attachmentId) return;
    const abort = new AbortController();
    // ponytail: a .docx downloads whole before the size check (the ref has no size); an outputs `size` would skip it.
    const read = docx
      ? messaging.downloadAttachment(roomId, attachmentId, { signal: abort.signal })
        .then((blob) => { if (blob.size > DOCX_MAX_BYTES) throw new Error("too large"); return blobBytes(blob); }).then((b) => docxFace(b))
      : messaging.downloadAttachment(roomId, attachmentId, { head: 4096, signal: abort.signal }).then(blobText).then((t) => docPreview(t));
    read.then((value) => { if (!abort.signal.aborted) setPreview({ key: `${roomId}:${attachmentId}`, value }); }, () => undefined);
    return () => abort.abort();
  }, [on, seen, roomId, attachmentId, messaging, docx]);
  return { face, preview: preview?.key === key ? preview.value : undefined };
}

/** A short result's face: its name as the title, then its next lines (an email's subject line is the name). */
function textFace(text: string): { heading: string | null; lines: string[] } {
  const heading = textName(text) || null;
  const lines = docPreview(text, 6).lines.filter((l) => l !== heading && !/^subject\s*:/i.test(l)).slice(0, 3);
  return { heading, lines };
}

export function OutputThumb({ output, size = "tile", frame = true }: {
  output: Pick<TaskOutput, "kind" | "title" | "ref">;
  /** "tile": Creations' 16:10 tile, with the extension or host under the glyph. "mini": Made here's 88×56. */
  size?: "tile" | "mini";
  /** false: never preview the doc (the grid shows only its first few). */
  frame?: boolean;
}): ReactElement {
  const image = (output.kind === "file" || output.kind === "document") && output.ref?.contentType?.startsWith("image/");
  const src = useAttachmentImage(output.ref ?? {}, !!image);
  const docx = isDocx(output);
  const { face, preview: read } = useDocPreview(output.ref ?? {}, frame && (docx || isTextDoc(output)), docx);
  // A short text result carries its words (`ref.text`): its face is them, no read (W16-3).
  const preview = output.ref?.text ? textFace(output.ref.text) : read;
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
