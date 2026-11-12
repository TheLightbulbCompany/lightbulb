import { lazy, Suspense, type ComponentType, type ReactNode } from "react";
import type { Components, ExtraProps } from "react-markdown";
import { CARD_FENCE } from "@lightbulb/shared";
import { markdownComponents } from "./MarkdownView";
import { useCardContext } from "./openui/CardContext";
import { attachmentIdFromHref } from "@/lib/attachments";
import { safeHref } from "@/lib/safeHref";

const CARD_CLASS = `language-${CARD_FENCE}`;
const MERMAID_CLASS = "language-mermaid";
type CodeProps = React.ComponentProps<"code"> & ExtraProps;
type PreProps = React.ComponentProps<"pre"> & ExtraProps;
// They are plain function components in MarkdownView; cast once so the fallback is renderable.
const DefaultCode = markdownComponents.code as ComponentType<CodeProps>;
const DefaultPre = markdownComponents.pre as ComponentType<PreProps>;
// OpenUI, its parser and recharts load only when a card is first drawn.
const OpenUICard = lazy(() => import("./openui/OpenUICard").then((m) => ({ default: m.OpenUICard })));

// mermaid loads only when a diagram is first drawn.
const MermaidBlock = lazy(() => import("./MermaidBlock").then((m) => ({ default: m.MermaidBlock })));

/** An agent's card fence. Without a CardContext (a person's message, a transcript) it stays code. */
function CodeOrCard(props: CodeProps) {
  const ctx = useCardContext();
  const { className, children } = props as { className?: string; children?: ReactNode };
  if (ctx && typeof className === "string" && className.split(" ").includes(CARD_CLASS)) {
    return <Suspense fallback={<DefaultCode {...props} />}>
      <OpenUICard program={String(children ?? "").replace(/\n$/, "")} />
    </Suspense>;
  }
  // A diagram is inert, so unlike a card it renders for anyone.
  if (typeof className === "string" && className.split(" ").includes(MERMAID_CLASS)) {
    // PreOrCard unwrapped the <pre> for the diagram, so the loading fallback brings its own.
    return <Suspense fallback={<DefaultPre><DefaultCode {...props} /></DefaultPre>}>
      <MermaidBlock source={String(children ?? "").replace(/\n$/, "")} />
    </Suspense>;
  }
  return <DefaultCode {...props} />;
}

/** react-markdown wraps block code in <pre>; a card must not inherit the code box. */
function PreOrCard(props: PreProps) {
  const ctx = useCardContext();
  const node = (props as { node?: { children?: { type: string; properties?: { className?: unknown }; children?: { value?: string }[] }[] } }).node;
  const first = node?.children?.find((c) => c.type === "element");
  const cls = Array.isArray(first?.properties?.className) ? (first!.properties!.className as unknown[]).join(" ") : "";
  const classes = cls.split(" ");
  if ((ctx && classes.includes(CARD_CLASS)) || classes.includes(MERMAID_CLASS)) return <>{(props as { children?: ReactNode }).children}</>;
  // An empty fence (a model's stray ``` pair) draws nothing rather than an empty code box.
  const text = first?.children?.map((c) => (c as { value?: string }).value ?? "").join("") ?? "";
  if (first && text.trim() === "") return null;
  return <DefaultPre {...props} />;
}

/** Public conversation text: no remote images, raw HTML, or privileged attachment routing; agents' cards and
 *  diagrams drawn. Links open in a new tab (the old room's in-shell link opener is messaging's roomMarkdown). */
export const cardMarkdown: Partial<Components> = {
  a: ({ href, children }) => {
    // Paperclip attachment links only resolve on an issue-scoped route a room message has none of: inert text.
    if (attachmentIdFromHref(href)) return <span>{children}</span>;
    const safe = safeHref(href);
    if (!safe) return <span>{children}</span>;
    return <a href={safe} className="text-info underline underline-offset-4" target="_blank" rel="noopener noreferrer">{children}</a>;
  },
  img: ({ alt }) => <span className="text-muted-foreground">{alt || "Image omitted"}</span>,
  blockquote: ({ children }) => <blockquote className="border-l border-border pl-3 my-2 text-muted-foreground">{children}</blockquote>,
  code: CodeOrCard,
  pre: PreOrCard,
};
