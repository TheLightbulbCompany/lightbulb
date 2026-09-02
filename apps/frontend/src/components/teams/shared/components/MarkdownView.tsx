"use client";

import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import { remarkSoftBreaks } from "@/components/teams/shared/lib/remark-soft-breaks";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { handleShellLinkClick, teamsDetailUrl, teamsPanelUrl } from "@/lib/shellNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";

/**
 * Agent comments carry attachment links in the upstream Paperclip shape
 * (`/api/attachments/{id}/content`, the `contentPath` the upload helper prints)
 * — a path that only resolves on the Paperclip BFF and needs a Clerk bearer,
 * so a bare anchor 404s against the frontend origin. Returns the BFF-relative
 * download path (`/attachments/{id}/content`) for such hrefs, else null.
 */
export function attachmentPathFromHref(href?: string | null): string | null {
  if (!href) return null;
  const m = /^\/api(\/attachments\/[^/?#]+\/content)(?:[?#].*)?$/.exec(href);
  return m ? m[1] : null;
}

// The bundled paperclip skill ("Company-prefixed URLs (required)") instructs
// agents to write every internal link as a native Paperclip path —
// `/ISOA/issues/ISOA-171`, `/ISOA/approvals/<id>`, `/ISOA/agents/<key>/runs/<id>`.
// Those routes do not exist in this app (the /teams/* tree was retired without a
// redirect), so a bare anchor left the shell and 404'd. Maps such a path onto its
// in-shell equivalent, or null when the href isn't one (external URLs, /api/…,
// already-in-app /chat?… links — the uppercase prefix keeps those out).
//
// Issue identifiers need no lookup: upstream Paperclip resolves `ISOA-171` for
// all /issues/:id routes and the Teams BFF passes issue_id straight through.
const PAPERCLIP_LINK =
  /^\/[A-Z][A-Z0-9]{0,9}\/(issues|approvals|projects|agents)\/([^/?#]+)(?:\/runs\/([^/?#]+))?\/?(?:\?[^#]*)?(#[^?]*)?$/;

export function shellUrlFromPaperclipPath(href?: string | null): string | null {
  const m = href ? PAPERCLIP_LINK.exec(href) : null;
  if (!m) return null;
  const [, section, id, runId, hash = ""] = m;
  if (runId) return teamsDetailUrl("run", runId);
  if (section === "issues") return teamsDetailUrl("issue", id) + hash;
  // No detail panel takes these ids — agent url-keys and project url-keys are
  // not the uuids our panels resolve, and approvals only ever render inside the
  // Inbox. Land on the list panel that owns them; a list beats a dead link.
  return teamsPanelUrl(section === "approvals" ? "inbox" : section);
}

function textContent(node: React.ReactNode): string {
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(textContent).join("");
  return "";
}

// Fetches the attachment bytes through the authenticated Teams download (a bare
// <a href> can't carry the Clerk bearer) and hands them to the browser as a
// named file — same flow as the artifact gallery's download affordance.
function AttachmentLink({ path, name, children }: {
  path: string;
  name: string;
  children?: React.ReactNode;
}) {
  const { download } = useTeamsApi();
  const [busy, setBusy] = useState(false);

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
    } catch {
      // Best-effort — a download hiccup should not wedge the comment thread.
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

// react-markdown does NOT render raw HTML (no rehype-raw), so the body is
// inert apart from link hrefs, which we guard. Themed with the teams CSS vars.
export const markdownComponents: Components = {
  a: ({ href, children, ...props }) => {
    const attachmentPath = attachmentPathFromHref(href);
    if (attachmentPath) {
      return (
        <AttachmentLink path={attachmentPath} name={textContent(children) || "attachment"}>
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
  h1: ({ children }) => <h1 className="text-base font-semibold mt-3 mb-1">{children}</h1>,
  h2: ({ children }) => <h2 className="text-sm font-semibold mt-3 mb-1">{children}</h2>,
  h3: ({ children }) => <h3 className="text-sm font-semibold mt-2 mb-1">{children}</h3>,
  p: ({ children }) => <p className="text-sm my-1 leading-relaxed">{children}</p>,
  ul: ({ children }) => <ul className="list-disc pl-5 text-sm my-1">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-5 text-sm my-1">{children}</ol>,
  li: ({ children }) => <li className="my-0.5">{children}</li>,
  code: ({ className, children }) => {
    const isBlock = typeof className === "string" && className.includes("language-");
    return isBlock ? (
      <code className="font-mono">{children}</code>
    ) : (
      <code className="rounded-sm px-1 py-0.5 text-xs font-mono [overflow-wrap:anywhere]"
            style={{ background: "var(--edge)" }}>{children}</code>
    );
  },
  pre: ({ children }) => (
    <pre className="rounded-sm p-2 text-xs font-mono overflow-x-auto my-2"
         style={{ background: "var(--edge)" }}>{children}</pre>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 pl-3 my-2 text-sm"
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
}

/** Shared read-only markdown renderer (gfm, no raw HTML, link-guarded, CSS-var themed).
 *
 * `min-w-0` + `[overflow-wrap:anywhere]` are load-bearing: agent output routinely
 * contains long unbreakable tokens (URLs, error strings like `getaddrinfoENOTFOUND`,
 * hashes) with no whitespace to break on. Without these, such a token can't wrap and
 * overflows its column — in the Teams task view it bled off the right edge into the
 * metadata panel. `min-w-0` lets this shrink below its content's intrinsic width inside
 * a flex/grid parent; `overflow-wrap:anywhere` forces a break inside the long token. */
export function MarkdownView({ children, components, skipHtml }: MarkdownViewProps) {
  const merged = useMemo(
    () => (components ? { ...markdownComponents, ...components } : markdownComponents),
    [components],
  );
  return (
    <div className="min-w-0 [overflow-wrap:anywhere]">
      <ReactMarkdown remarkPlugins={[remarkGfm, remarkSoftBreaks]} components={merged} skipHtml={skipHtml}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
