"use client";

import { memo, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components, Options, UrlTransform } from "react-markdown";
import { remarkSoftBreaks } from "@/components/teams/shared/lib/remark-soft-breaks";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { handleShellLinkClick, shellUrlFromPaperclipPath } from "@/lib/shellNav";
import { normalizeCtaLinks } from "@/lib/ctaLinks";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import {
  attachmentIdFromHref,
  issueAttachmentContentPath,
  useAttachmentIssueId,
  useAttachmentObjectUrl,
} from "@/components/teams/shared/lib/attachments";
import { notifyApiError } from "@/lib/notify";

function textContent(node: React.ReactNode): string {
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(textContent).join("");
  return "";
}

// Agent comments carry attachment links in the upstream shape
// (`/api/attachments/{id}/content`, see shared/lib/attachments). With an owning
// issue in context the bytes come through the issue-scoped Teams download (a
// bare <a href> can't carry the Clerk bearer) and reach the browser as a named
// file. Without one there is no fenced route to ask, so the link text renders
// as plain text — never a fetch, never a dead anchor.
function AttachmentLink({ attachmentId, name, children }: {
  attachmentId: string;
  name: string;
  children?: React.ReactNode;
}) {
  const issueId = useAttachmentIssueId();
  const { download } = useTeamsApi();
  const [busy, setBusy] = useState(false);
  if (!issueId) return <span data-attachment-inert>{children}</span>;
  const path = issueAttachmentContentPath(issueId, attachmentId);

  async function handleClick() {
    setBusy(true);
    try {
      const blob = await download(path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      notifyApiError(err, "Couldn't download this attachment");
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      disabled={busy}
      className="cursor-pointer underline disabled:cursor-wait"
      style={{ color: "var(--midnight)" }}
    >
      {children}
    </button>
  );
}

// An agent-pasted attachment image: fetched as a blob through the issue-scoped
// route; without an owning issue (or until/unless it loads) its alt text shows
// as plain text instead of a broken image.
function AttachmentImage({ src, alt }: { src: string; alt: string }) {
  const url = useAttachmentObjectUrl(src);
  // eslint-disable-next-line @next/next/no-img-element -- blob: URL
  return url ? <img src={url} alt={alt} className="max-w-full rounded-sm" /> : <span data-attachment-inert>{alt}</span>;
}

// "[Reserve a spot]([REGISTRATION URL])" is not a link to the parser (a destination can't hold spaces), so it reached
// the page as literal brackets. Any such pair left in a text node is a broken link: keep its words. Code is not text,
// so code spans and blocks keep theirs.
type MdNode = { type?: string; value?: string; children?: MdNode[] };
const BROKEN_LINK = /\[([^\]\n]+)\]\(([^)\n]*)\)/g;
// A line that is only "[Sign up for story-time]" is a button a draft asks for (an email's call to action, W16-5): it
// shows as a button-styled label, no brackets. Copy gives the words alone (docText `plainCta`). A placeholder link
// ("[**BOOK YOUR SPOT**]([BOOKING LINK])", W17-3) reaches here already in that shape (`normalizeCtaLinks`).
const CTA_LINE = /^\s*\[([^\]\n]{2,60})\]\s*$/;
const ctaNode = (words: string): MdNode & { data: object } => ({
  type: "emphasis", children: [{ type: "text", value: words }],
  data: { hName: "span", hProperties: { className: "md-cta inline-block rounded-md border border-border bg-muted px-3 py-0.5 font-medium not-italic text-foreground" } },
});
function remarkBrokenLinks() {
  const walk = (node: MdNode) => {
    if (node.type === "text" && node.value) node.value = node.value.replace(BROKEN_LINK, "$1");
    node.children?.forEach(walk);
    if (node.children) node.children = node.children.map((child) => {
      const cta = child.type === "text" && child.value ? CTA_LINE.exec(child.value) : null;
      return cta ? ctaNode(cta[1].trim()) : child;
    });
  };
  return walk;
}

// react-markdown does NOT render raw HTML (no rehype-raw), so the body is
// inert apart from link hrefs, which we guard. Themed with the teams CSS vars.
export const markdownComponents: Components = {
  a: ({ href, children, ...props }) => {
    const attachmentId = attachmentIdFromHref(href);
    if (attachmentId) {
      return (
        <AttachmentLink attachmentId={attachmentId} name={textContent(children) || "attachment"}>
          {children}
        </AttachmentLink>
      );
    }
    const shellUrl = shellUrlFromPaperclipPath(href);
    const safe = shellUrl ?? safeHref(href);
    if (!safe) return <span {...props}>{children}</span>;
    // In-shell links keep their real href (native middle-click / cmd-click) but
    // commit through pushState — and only when the shell is already on that
    // route: handleShellLinkClick swaps query params in place, so from another
    // page (e.g. the unauthenticated landing demo) the anchor must navigate.
    const shellAttrs = shellUrl
      ? {
          onClick: (e: React.MouseEvent<HTMLAnchorElement>) => {
            if (shellUrl.split("?")[0] === window.location.pathname)
              handleShellLinkClick(e, shellUrl);
          },
        }
      : { target: "_blank", rel: "noopener noreferrer" };
    return (
      <a href={safe} style={{ color: "var(--midnight)", textDecoration: "underline" }}
         {...props} {...shellAttrs}>
        {children}
      </a>
    );
  },
  img: ({ src, alt, title }) => {
    if (typeof src === "string" && attachmentIdFromHref(src)) {
      return <AttachmentImage src={src} alt={alt || "attachment"} />;
    }
    // eslint-disable-next-line @next/next/no-img-element -- markdown image, not a page asset
    return <img src={src} alt={alt} title={title} />;
  },
  h1: ({ children }) => <h1 className="text-base font-semibold mt-3 mb-1">{children}</h1>,
  h2: ({ children }) => <h2 className="text-ui font-semibold mt-3 mb-1">{children}</h2>,
  h3: ({ children }) => <h3 className="text-ui font-semibold mt-2 mb-1">{children}</h3>,
  p: ({ children }) => <p className="text-ui my-1 leading-relaxed">{children}</p>,
  ul: ({ children }) => <ul className="list-disc pl-5 text-ui my-1">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-5 text-ui my-1">{children}</ol>,
  li: ({ children }) => <li className="my-0.5">{children}</li>,
  code: ({ className, children }) => {
    const isBlock = typeof className === "string" && className.includes("language-");
    return isBlock ? (
      <code className="font-mono">{children}</code>
    ) : (
      <code className="rounded-sm px-1 py-0.5 text-caption font-mono [overflow-wrap:anywhere]"
            style={{ background: "var(--edge)" }}>{children}</code>
    );
  },
  pre: ({ children }) => (
    <pre className="rounded-sm p-2 text-caption font-mono overflow-x-auto my-2"
         style={{ background: "var(--edge)" }}>{children}</pre>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 pl-3 my-2 text-ui"
                style={{ borderColor: "var(--edge)", color: "var(--midnight-muted)" }}>{children}</blockquote>
  ),
};

export interface MarkdownViewProps {
  children: string;
  /** Per-surface overrides merged over the shared set — the Brain swaps `a`
   *  for in-vault wikilink navigation and enlarges headings for a reading pane. */
  components?: Partial<Components>;
  /** Drop raw HTML. react-markdown 10 turns an html node into a TEXT node
   *  unless told to skip (lib/index.js:360-366), so memory-wiki's managed-block
   *  markers (`<!-- openclaw:wiki:index:start -->`) would print verbatim. */
  skipHtml?: boolean;
  /** Override react-markdown's default href/src sanitizer (which blanks any
   *  non-http(s)/mailto/relative scheme). A caller that encodes structured data
   *  in a link href — e.g. messaging's `mention:` scheme — needs this, or the
   *  href never reaches its `a` override at all. Omit to keep the default. */
  urlTransform?: UrlTransform;
  /** Extra remark plugins run after the shared gfm + soft-break set. Pass a
   *  stable array (module constant or memoised) — a fresh one re-parses. Room
   *  messages pass none, so they never get task-only rewrites such as
   *  TaskMarkdown's issue-identifier links. */
  remarkPlugins?: NonNullable<Options["remarkPlugins"]>;
}

/** Shared read-only markdown renderer (gfm, no raw HTML, link-guarded, CSS-var themed).
 *
 * `min-w-0` + `[overflow-wrap:anywhere]` are load-bearing: agent output routinely
 * contains long unbreakable tokens (URLs, error strings like `getaddrinfoENOTFOUND`,
 * hashes) with no whitespace to break on. Without these, such a token can't wrap and
 * overflows its column — in the Teams task view it bled off the right edge into the
 * metadata panel. `min-w-0` lets this shrink below its content's intrinsic width inside
 * a flex/grid parent; `overflow-wrap:anywhere` forces a break inside the long token. */
// Module scope, so react-markdown sees the same array every render. As an
// inline literal it was a fresh array each time, which defeats memoisation
// inside the renderer as well as outside it.
const REMARK_PLUGINS = [remarkGfm, remarkSoftBreaks, remarkBrokenLinks];

/**
 * Memoised on purpose.
 *
 * This mounts a full remark/rehype pipeline, and the room timeline renders one
 * per message — through a render-prop closure that React Compiler cannot see
 * across, so every re-render of the room panel re-parsed every message body.
 * The panel re-renders on each composer keystroke (the draft lives in the same
 * provider), each typing signal and each streamed chunk. Props are all stable
 * by value or by reference (`markdownComponents` and the room's overrides are
 * module constants), so a shallow compare is enough.
 */
export const MarkdownView = memo(function MarkdownView({ children, components, skipHtml, urlTransform, remarkPlugins }: MarkdownViewProps) {
  const plugins = useMemo(
    () => (remarkPlugins?.length ? [...REMARK_PLUGINS, ...remarkPlugins] : REMARK_PLUGINS),
    [remarkPlugins],
  );
  const merged = useMemo(
    () => (components ? { ...markdownComponents, ...components } : markdownComponents),
    [components],
  );
  return (
    <div className="min-w-0 [overflow-wrap:anywhere]">
      <ReactMarkdown remarkPlugins={plugins} components={merged} skipHtml={skipHtml} urlTransform={urlTransform}>
        {normalizeCtaLinks(children)}
      </ReactMarkdown>
    </div>
  );
});
