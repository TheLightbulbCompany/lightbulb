import type { Components } from "react-markdown";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { handleShellLinkClick, shellUrlFromPaperclipPath } from "@/lib/shellNav";

/** Public conversation text: no remote images, raw HTML, or privileged attachment routing. */
export const roomMarkdown: Partial<Components> = {
  a: ({ href, children }) => {
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
