"use client";

import { useEffect, useState } from "react";
import { blobBytes } from "./readBlob";
import { zipInflatedSize } from "./zipInflatedSize";

export const DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
/** Past this the viewer offers Download instead of converting in the browser. */
export const DOCX_MAX_BYTES = 10 * 1024 * 1024;
/** A docx is a zip: its declared inflated size is checked before mammoth unpacks it (a zip bomb under 10 MB). */
const DOCX_MAX_INFLATED_BYTES = 64 * 1024 * 1024;

// mammoth's node build reads `buffer`, its browser build (the `browser` field) `arrayBuffer`; tests run the node one.
const input = (bytes: ArrayBuffer) => ({ arrayBuffer: bytes, buffer: bytes }) as unknown as { arrayBuffer: ArrayBuffer };

function checked(bytes: ArrayBuffer): ArrayBuffer {
  if (bytes.byteLength > DOCX_MAX_BYTES) throw new Error("too large");
  const inflated = zipInflatedSize(bytes);
  if (inflated === null || inflated > DOCX_MAX_INFLATED_BYTES) throw new Error("not a docx or too large inflated");
  return bytes;
}

/** A Word file as safe HTML: mammoth (lazy, its own chunk) then DOMPurify. No scripts, no styles, no handlers, and no
 *  image but one embedded in the file (mammoth writes those as data: URIs), so showing it fetches nothing. Links open
 *  in a new tab, never in place of the app. */
export async function docxHtml(bytes: ArrayBuffer): Promise<string> {
  const [{ default: mammoth }, { default: DOMPurify }] = await Promise.all([import("mammoth"), import("dompurify")]);
  const { value } = await mammoth.convertToHtml(input(checked(bytes)));
  const purify = DOMPurify();
  purify.addHook("afterSanitizeAttributes", (node) => {
    if (node.tagName === "IMG" && !/^data:image\//i.test(node.getAttribute("src") ?? "")) node.removeAttribute("src");
    if (node.tagName === "A") { node.setAttribute("target", "_blank"); node.setAttribute("rel", "noopener noreferrer"); }
  });
  return purify.sanitize(value, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select"],
    FORBID_ATTR: ["style", "srcset"],
  });
}

/** A Word file's words, for a tile: its first line as the heading, the next few under it. */
export async function docxFace(bytes: ArrayBuffer, max = 3): Promise<{ heading: string | null; lines: string[] }> {
  const { default: mammoth } = await import("mammoth");
  const { value } = await mammoth.extractRawText(input(checked(bytes)));
  const [heading = null, ...lines] = value.split("\n").map((l) => l.trim()).filter(Boolean).slice(0, max + 1);
  return { heading, lines };
}

type State = { status: "loading" } | { status: "ready"; html: string } | { status: "error" };

/** A .docx in the viewer, set in the viewer's document type (`.doc-view` in the pane). View only. */
export function DocxPreview({ blob, fileName }: { blob: Blob; fileName: string }) {
  const [state, setState] = useState<State>({ status: "loading" });
  useEffect(() => {
    let live = true;
    if (blob.size > DOCX_MAX_BYTES) return;
    blobBytes(blob).then(docxHtml).then((html) => { if (live) setState({ status: "ready", html }); }, () => { if (live) setState({ status: "error" }); });
    return () => { live = false; };
  }, [blob]);
  if (blob.size > DOCX_MAX_BYTES) return <p role="alert" className="p-4 text-ui text-muted-foreground">{fileName} is too large to preview. Download it instead.</p>;
  if (state.status === "error") return <p role="alert" className="p-4 text-ui text-muted-foreground">Could not preview {fileName}. Download it instead.</p>;
  if (state.status === "loading") return <p className="p-4 text-ui text-muted-foreground">Loading {fileName}…</p>;
  // Headings, paragraphs and lists take `.doc-view`'s type in the pane; lists and tables need their marks back here.
  return <div data-docx dangerouslySetInnerHTML={{ __html: state.html }} className={"p-4 text-ui [overflow-wrap:anywhere] [&_p]:my-1"
    + " [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_a]:underline [&_img]:max-w-full"
    + " [&_table]:my-4 [&_table]:w-full [&_table]:border-collapse [&_:is(td,th)]:border [&_:is(td,th)]:border-[var(--edge)]"
    + " [&_:is(td,th)]:px-2 [&_:is(td,th)]:py-1 [&_:is(td,th)]:align-top [&_:is(td,th)]:text-left [&_:is(td,th)_p]:m-0"} />;
}
