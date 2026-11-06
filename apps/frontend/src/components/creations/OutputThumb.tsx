"use client";

// The face of an output: an app's own page (live, scaled into the tile), an image itself, else its glyph with the
// extension or host. Glyphs are lucide, one stroke.

import type { ReactElement } from "react";
import useSWR from "swr";
import { AppWindow, ArrowUpRight, FileText, File as FileIcon, Mail, Sparkles, type LucideIcon } from "lucide-react";
import type { OutputKind, TaskOutput } from "@lightbulb/shared";
import type { AppRow } from "@/hooks/useApps";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { extensionOf, hostOf } from "./openOutput";

export const KIND_ICON: Record<OutputKind, LucideIcon> = {
  app: AppWindow, document: FileText, file: FileIcon, mail: Mail, link: ArrowUpRight, pr: ArrowUpRight, capability: Sparkles,
};

/** A short-lived address for an image a task made (presigned; refetched by SWR when the tile comes back). */
function useAttachmentImage(ref: Record<string, string>, on: boolean): string | undefined {
  const { messaging } = useLiveEvents();
  const { data } = useSWR(on && ref.roomId && ref.attachmentId ? ["attachment-url", ref.roomId, ref.attachmentId] : null,
    ([, room, id]: [string, string, string]) => messaging.attachmentUrl(room, id), { shouldRetryOnError: false, revalidateOnFocus: false });
  return data?.url;
}

export function OutputThumb({ output, app, size = "tile" }: {
  output: Pick<TaskOutput, "kind" | "title" | "ref">;
  /** The app's row, for its page; without one an app shows its glyph. */
  app?: AppRow;
  /** "tile": Creations' 16:10 tile, with the extension or host under the glyph. "mini": Made here's 88×56. */
  size?: "tile" | "mini";
}): ReactElement {
  const image = (output.kind === "file" || output.kind === "document") && output.ref?.contentType?.startsWith("image/");
  const src = useAttachmentImage(output.ref ?? {}, !!image);
  const page = output.kind === "app" ? app?.live_url ?? app?.preview_url : null;
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
  const Icon = KIND_ICON[output.kind] ?? FileIcon;
  const note = output.kind === "link" || output.kind === "pr" ? hostOf(output) : output.kind === "document" || output.kind === "file" ? extensionOf(output) : "";
  return <span className="output-thumb output-thumb--glyph" data-size={size}>
    <Icon size={size === "mini" ? 18 : 24} strokeWidth={1.5} aria-hidden="true" />
    {size === "tile" && note && <span className="output-thumb__note">{note}</span>}
  </span>;
}
