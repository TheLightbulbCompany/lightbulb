import type { ComponentType, ReactNode } from "react";
import type { Components, ExtraProps } from "react-markdown";
import { CARD_FENCE } from "@lightbulb/shared";
import { markdownComponents } from "@/components/teams/shared/components/MarkdownView";
import { OpenUICard } from "./openui/OpenUICard";
import { useCardContext } from "./openui/CardContext";
import { attachmentIdFromHref } from "@/components/teams/shared/lib/attachments";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { appLinkTarget, type AppLink } from "./appLinks";
import { useMessagingWorkspaceOptional } from "./MessagingWorkspaceContext";
import { openAppLink } from "./RoomAppLinks";

const CARD_CLASS = `language-${CARD_FENCE}`;
type CodeProps = React.ComponentProps<"code"> & ExtraProps;
type PreProps = React.ComponentProps<"pre"> & ExtraProps;
// They are plain function components in MarkdownView; cast once so the fallback is renderable.
const DefaultCode = markdownComponents.code as ComponentType<CodeProps>;
const DefaultPre = markdownComponents.pre as ComponentType<PreProps>;

/** An agent's card fence. Without a CardContext (a person's message, a transcript) it stays code. */
function CodeOrCard(props: CodeProps) {
  const ctx = useCardContext();
  const { className, children } = props as { className?: string; children?: ReactNode };
  if (ctx && typeof className === "string" && className.split(" ").includes(CARD_CLASS)) {
    return <OpenUICard program={String(children ?? "").replace(/\n$/, "")} />;
  }
  return <DefaultCode {...props} />;
}

/** react-markdown wraps block code in <pre>; a card must not inherit the code box. */
function PreOrCard(props: PreProps) {
  const ctx = useCardContext();
  const node = (props as { node?: { children?: { type: string; properties?: { className?: unknown } }[] } }).node;
  const first = node?.children?.find((c) => c.type === "element");
  const cls = Array.isArray(first?.properties?.className) ? (first!.properties!.className as unknown[]).join(" ") : "";
  if (ctx && cls.split(" ").includes(CARD_CLASS)) return <>{(props as { children?: ReactNode }).children}</>;
  return <DefaultPre {...props} />;
}

/** Public conversation text: no remote images, raw HTML, or privileged attachment routing. */
export const roomMarkdown: Partial<Components> = {
  a: ({ href, children }) => {
    // Paperclip attachment links (`/api/attachments/{id}/content`) only
    // resolve on the issue-scoped Teams BFF (shared/lib/attachments.ts) — a
    // room message has no owning issue to fetch through, so a bare anchor
    // pointed at this app's own host and 404'd. Render the link text inert
    // instead; Teams issue views still fetch these through AttachmentLink.
    if (attachmentIdFromHref(href)) return <span>{children}</span>;
    // Links into our own app — the bundled paperclip skill's native paths
    // (`/ISOA/issues/ISOA-171`, which 404 against this app's routes) and absolute
    // thelightbulb.company links (company., dev., permalinks) — open in the shell, in
    // place, instead of a new browser tab.
    const link = appLinkTarget(href);
    if (link && link.href.startsWith("/chat")) return <AppAnchor link={link}>{children}</AppAnchor>;
    const safe = safeHref(href);
    if (!safe) return <span>{children}</span>;
    return <a href={safe} className="text-info underline underline-offset-4" target="_blank" rel="noopener noreferrer">{children}</a>;
  },
  img: ({ alt }) => <span className="text-muted-foreground">{alt || "Image omitted"}</span>,
  blockquote: ({ children }) => <blockquote className="border-l border-border pl-3 my-2 text-muted-foreground">{children}</blockquote>,
  code: CodeOrCard,
  pre: PreOrCard,
};

function AppAnchor({ link, children }: { link: AppLink; children: ReactNode }) {
  const ws = useMessagingWorkspaceOptional();
  return <a href={link.href} className="text-info underline underline-offset-4" onClick={(e) => openAppLink(e, link, ws)}>{children}</a>;
}
