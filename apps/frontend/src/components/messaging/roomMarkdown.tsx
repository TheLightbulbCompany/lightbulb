import type { Components } from "react-markdown";
import { safeHref } from "@/components/teams/shared/lib/safeHref";

/** Public conversation text: no remote images, raw HTML, or privileged attachment routing. */
export const roomMarkdown: Partial<Components> = {
  a: ({ href, children }) => {
    const safe = safeHref(href);
    return safe ? <a href={safe} target="_blank" rel="noopener noreferrer" className="text-info underline underline-offset-4">{children}</a> : <span>{children}</span>;
  },
  img: ({ alt }) => <span className="text-muted-foreground">{alt || "Image omitted"}</span>,
  blockquote: ({ children }) => <blockquote className="border-l border-border pl-3 my-2 text-muted-foreground">{children}</blockquote>,
};
