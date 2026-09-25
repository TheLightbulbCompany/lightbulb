import type { Components } from "react-markdown";
import { attachmentIdFromHref } from "@/components/teams/shared/lib/attachments";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { handleShellLinkClick, shellUrlFromPaperclipPath } from "@/lib/shellNav";

/** Public conversation text: no remote images, raw HTML, or privileged attachment routing. */
export const roomMarkdown: Partial<Components> = {
  a: ({ href, children }) => {
    // Paperclip attachment links (`/api/attachments/{id}/content`) only
    // resolve on the issue-scoped Teams BFF (shared/lib/attachments.ts) — a
    // room message has no owning issue to fetch through, so a bare anchor
    // pointed at this app's own host and 404'd. Render the link text inert
    // instead; Teams issue views still fetch these through AttachmentLink.
    if (attachmentIdFromHref(href)) return <span>{children}</span>;
    // The bundled paperclip skill writes internal links as native Paperclip
    // paths (e.g. `/ISOA/issues/ISOA-171`), which 404 against this app's own
    // routes — resolve them to the in-shell Teams panel instead (mirrors
    // MarkdownView.tsx's own `a` override).
    const shellUrl = shellUrlFromPaperclipPath(href);
    const safe = shellUrl ?? safeHref(href);
    if (!safe) return <span>{children}</span>;
    const shellAttrs = shellUrl
      ? {
          onClick: (e: React.MouseEvent<HTMLAnchorElement>) => {
            if (shellUrl.split("?")[0] === window.location.pathname) handleShellLinkClick(e, shellUrl);
          },
        }
      : { target: "_blank", rel: "noopener noreferrer" };
    return <a href={safe} className="text-info underline underline-offset-4" {...shellAttrs}>{children}</a>;
  },
  img: ({ alt }) => <span className="text-muted-foreground">{alt || "Image omitted"}</span>,
  blockquote: ({ children }) => <blockquote className="border-l border-border pl-3 my-2 text-muted-foreground">{children}</blockquote>,
};
