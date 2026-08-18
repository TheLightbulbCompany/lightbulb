import * as React from "react";
import { cn } from "@/lib/utils";
import { linkifyFilePaths, isWorkspaceFileLink, extractFilePath } from "@/lib/filePathDetection";
import { stripExternalContentMarkers } from "@/lib/externalContentMarkers";
import { Copy, Share2, ChevronDown, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useScrollToBottom } from "@/hooks/useScrollToBottom";
import { ChatErrorCard, isTrialPaywallCode } from "@/components/chat/ChatErrorCard";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import SyntaxHighlighter from "react-syntax-highlighter/dist/esm/prism-light";
import oneDark from "react-syntax-highlighter/dist/esm/styles/prism/one-dark";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import diff from "react-syntax-highlighter/dist/esm/languages/prism/diff";
import docker from "react-syntax-highlighter/dist/esm/languages/prism/docker";
import ini from "react-syntax-highlighter/dist/esm/languages/prism/ini";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import jsx from "react-syntax-highlighter/dist/esm/languages/prism/jsx";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import markup from "react-syntax-highlighter/dist/esm/languages/prism/markup";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import rust from "react-syntax-highlighter/dist/esm/languages/prism/rust";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import toml from "react-syntax-highlighter/dist/esm/languages/prism/toml";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import { HireCard, parseHireSuggestion } from "@/components/chat/HireCard";
import { ToolActivityTrace, type ToolCall } from "@/components/chat/ToolActivityTrace";
import {
  pairToolBlocks,
  isToolUseBlock,
  isToolResultBlock,
  isThinkingBlock,
  isTextBlock,
  resolveToolUseId,
  resolveToolArgs,
  resolveMessageBlocks,
} from "@/hooks/internal/toolBlockExtract";
import type {
  ChatRenderItem,
  ContentBlock,
  RenderGroup,
  RenderSide,
  HistoryMessage,
} from "@/hooks/internal/types";
// Approval rendering moved to ExecApprovalDialog (mounted at chat-shell level
// via useExecApprovals). Tool activity is rendered by ToolActivityTrace, which
// represents execution state only — running / done / error. See
// ExecApprovalDialog.tsx for the modal that surfaces pending exec approvals.

export interface MessageListProps {
  renderGroups: RenderGroup[];
  agentName?: string;
  isStreaming?: boolean;
  isTyping?: boolean;
  onOpenFile?: (path: string) => void;
  hasOlder?: boolean;
  isLoadingOlder?: boolean;
  onLoadOlder?: () => void;
}

/**
 * Grammars registered on the PrismLight build. The `react-syntax-highlighter`
 * barrel export bundles all ~300 refractor grammars — nearly half of /chat's
 * first-load JS — so we register only the fences agents realistically emit
 * here: the languages this platform is written in and the config / markup
 * formats that surround them. Registering a grammar also pulls in whatever it
 * extends (jsx → markup + javascript, typescript → javascript) and installs
 * its own aliases (py, sh, shell, ts, yml, md, dockerfile, html, …).
 */
const PRISM_LANGUAGES = [
  bash,
  css,
  diff,
  docker,
  ini,
  javascript,
  json,
  jsx,
  markdown,
  markup,
  python,
  rust,
  sql,
  toml,
  tsx,
  typescript,
  yaml,
];

/** Language passed through for anything unregistered — renders unhighlighted. */
const PLAIN_LANGUAGE = "text";

const REGISTERED_LANGUAGES = new Set<string>();
for (const grammar of PRISM_LANGUAGES) {
  SyntaxHighlighter.registerLanguage(grammar.displayName, grammar);
  REGISTERED_LANGUAGES.add(grammar.displayName);
  for (const alias of (grammar.aliases ?? []) as string[]) {
    REGISTERED_LANGUAGES.add(alias);
  }
}

/**
 * Resolve a fence's language to one PrismLight can highlight. Unknown
 * languages fall back to plain text rather than reaching refractor, which
 * throws on an unregistered grammar. Exported for unit tests.
 */
export function resolveHighlightLanguage(language: string): string {
  const normalized = language.toLowerCase();
  return REGISTERED_LANGUAGES.has(normalized) ? normalized : PLAIN_LANGUAGE;
}

function CodeBlock({ className, children, ...props }: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) {
  const match = /language-(\w+)/.exec(className || "");
  const codeString = String(children).replace(/\n$/, "");

  if (!match) {
    // Inline code
    return (
      <code className="bg-[#e8e3d9] rounded px-1.5 py-0.5 text-sm text-[var(--ink)]" {...props}>
        {children}
      </code>
    );
  }

  // Fenced code block
  return (
    <div className="relative group/code my-4 rounded-lg overflow-hidden border border-[var(--edge)]">
      <div className="flex items-center justify-between px-4 py-2 bg-[var(--cream-deeper)] border-b border-[var(--edge)]">
        <span className="text-xs text-[var(--mute)]">{match[1]}</span>
        <button
          onClick={() => { navigator.clipboard.writeText(codeString).catch(() => {}); }}
          className="text-xs text-[var(--mute)] hover:text-[var(--ink)] transition-colors flex items-center gap-1"
        >
          <Copy className="h-3 w-3" />
          Copy
        </button>
      </div>
      <SyntaxHighlighter
        style={oneDark}
        language={resolveHighlightLanguage(match[1])}
        PreTag="div"
        customStyle={{ margin: 0, borderRadius: 0, background: "#f8f5f0" }}
      >
        {codeString}
      </SyntaxHighlighter>
    </div>
  );
}

const MarkdownContent = React.memo(function MarkdownContent({
  content,
  onOpenFile,
}: {
  content: string;
  onOpenFile?: (path: string) => void;
}) {
  // Sentinel strip BEFORE linkify: the injection-defense markers are
  // internal plumbing wherever they surface (echoed into assistant prose,
  // folded history text) — see externalContentMarkers.ts.
  const processedContent = React.useMemo(
    () => linkifyFilePaths(stripExternalContentMarkers(content)),
    [content],
  );
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        code: CodeBlock,
        h1: ({ children }) => <h1 className="text-lg font-semibold mt-4 mb-2">{children}</h1>,
        h2: ({ children }) => <h2 className="text-base font-semibold mt-3 mb-2">{children}</h2>,
        h3: ({ children }) => <h3 className="text-sm font-semibold mt-3 mb-1">{children}</h3>,
        h4: ({ children }) => <h4 className="text-sm font-medium mt-2 mb-1">{children}</h4>,
        p: ({ children }) => <p className="mb-3 last:mb-0">{children}</p>,
        a: ({ href, children }) => {
          if (href && isWorkspaceFileLink(href) && onOpenFile) {
            const filePath = extractFilePath(href);
            return (
              <button
                onClick={() => onOpenFile(filePath)}
                className="text-[var(--forest)] hover:underline cursor-pointer bg-transparent border-none p-0 font-inherit text-inherit inline"
              >
                {children}
              </button>
            );
          }
          const isSafe = !href?.match(/^(javascript|data|vbscript):/i);
          return (
            <a href={isSafe ? href : '#'} target="_blank" rel="noopener noreferrer" className="text-[var(--forest)] hover:underline">
              {children}
            </a>
          );
        },
        ul: ({ children }) => <ul className="list-disc list-inside mb-3 space-y-1">{children}</ul>,
        ol: ({ children }) => <ol className="list-decimal list-inside mb-3 space-y-1">{children}</ol>,
        li: ({ children }) => <li className="ml-2">{children}</li>,
        blockquote: ({ children }) => (
          <blockquote className="border-l-2 border-[var(--edge)] pl-4 text-[var(--mute)] my-3">{children}</blockquote>
        ),
        table: ({ children }) => (
          <div className="overflow-x-auto my-4">
            <table className="w-full border-collapse border border-[var(--edge)] text-sm">{children}</table>
          </div>
        ),
        thead: ({ children }) => <thead className="bg-[var(--cream-deeper)]">{children}</thead>,
        th: ({ children }) => <th className="border border-[var(--edge)] px-3 py-2 text-left font-medium">{children}</th>,
        td: ({ children }) => <td className="border border-[var(--edge)] px-3 py-2">{children}</td>,
        tr: ({ children }) => <tr className="even:bg-[var(--cream-deeper)]">{children}</tr>,
        hr: () => <hr className="border-[var(--edge)] my-4" />,
        pre: ({ children }) => <>{children}</>,
      }}
    >
      {processedContent}
    </ReactMarkdown>
  );
});

/**
 * Split markdown into stable top-level blocks: double-newline boundaries
 * outside code fences (fences stay intact as single blocks, including an
 * unterminated fence still streaming in). Used by StreamingMarkdownContent
 * so a growing live message only re-parses its tail block per paint.
 * Exported for unit tests.
 */
export function splitMarkdownBlocks(content: string): string[] {
  // Reference-style definitions ([label]: url, [^1]: footnote) resolve at
  // DOCUMENT level — a usage in one block with its definition in another
  // would render as literal text. Rare in chat output (models emit inline
  // links), so when any definition-shaped line exists, skip splitting
  // entirely rather than track cross-block reference graphs. May false-
  // positive on such a line inside a code fence — that only costs the
  // optimization, never correctness.
  if (/^ {0,3}\[[^\]\n]+\]:/m.test(content)) {
    return [content];
  }
  const blocks: string[] = [];
  let current: string[] = [];
  let inFence = false;
  let fenceMarker = "";
  for (const line of content.split("\n")) {
    const trimmed = line.trimStart();
    if (inFence) {
      current.push(line);
      if (trimmed.startsWith(fenceMarker)) inFence = false;
      continue;
    }
    if (trimmed.startsWith("```") || trimmed.startsWith("~~~")) {
      inFence = true;
      fenceMarker = trimmed.startsWith("```") ? "```" : "~~~";
      current.push(line);
      continue;
    }
    if (line.trim() === "") {
      // Block boundary. The blank line itself is dropped — separation is
      // re-established by rendering each block as its own component.
      if (current.length > 0) {
        blocks.push(current.join("\n"));
        current = [];
      }
      continue;
    }
    current.push(line);
  }
  if (current.length > 0) blocks.push(current.join("\n"));
  return blocks.length > 0 ? blocks : [content];
}

/**
 * Markdown for the LIVE streaming item. Splits the accumulated text into
 * fence-aware blocks and renders each through the memoized MarkdownContent:
 * every block except the growing tail is a frozen string, so per paint only
 * the tail re-runs ReactMarkdown/Prism instead of the whole message — the
 * cost that used to grow with message length. History messages keep the
 * plain whole-message MarkdownContent (their memo already never re-parses,
 * and one piece avoids block-split artifacts in the durable render).
 *
 * Each block mounts inside a `.chat-block-in` wrapper — a one-shot reveal
 * animation (globals.css). This is safe BECAUSE of the memoization above:
 * settled blocks memo-hit and never re-render, and the growing tail block is
 * reconciled in place (same key, same position) rather than remounted, so
 * the animation fires exactly once per block, ever. It cannot re-trigger or
 * flicker on subsequent chunks.
 *
 * `streaming` is true only for the live growing tail (`kind:"stream"`, see
 * ItemRenderer) — it renders the blinking caret after the last block. Past
 * `stream-segment`s and the thinking panel never set it.
 */
const StreamingMarkdownContent = React.memo(function StreamingMarkdownContent({
  content,
  onOpenFile,
  streaming,
}: {
  content: string;
  onOpenFile?: (path: string) => void;
  streaming?: boolean;
}) {
  const blocks = React.useMemo(() => splitMarkdownBlocks(content), [content]);
  const lastIndex = blocks.length - 1;
  return (
    <>
      {blocks.map((block, i) => {
        const isTail = streaming && i === lastIndex;
        return (
          <div key={i} className={cn("chat-block-in", isTail && "chat-tail")}>
            <MarkdownContent content={block} onOpenFile={onOpenFile} />
            {isTail && <span className="chat-caret" aria-hidden="true" />}
          </div>
        );
      })}
    </>
  );
});

/**
 * Reasoning-summary parts arrive concatenated with no separator (OpenClaw's
 * OpenAI transport handles `reasoning_summary_text.delta` but not the
 * `reasoning_summary_part.added` boundary), so consecutive `**Title**` headers
 * collide into `**A****B**`. Markdown then pairs the delimiters ACROSS the
 * boundary — alternating bold, with leftover `****` rendered literally. Split
 * the collision back into one header per paragraph.
 */
export function splitCollidedHeaders(content: string): string {
  return content.replace(/\*\*(?=\*\*)/g, "**\n\n");
}

/** Whole seconds only — a reasoning duration reads as "Thought for 23s", not
 *  "23.0s"; sub-second precision is noise here. */
function formatThinkingDuration(ms: number): string {
  return `${Math.round(ms / 1000)}s`;
}

export function ThinkingBlock({
  content,
  streaming,
  durationMs,
}: {
  content: string;
  streaming?: boolean;
  /** Wall-clock ms the reasoning phase took. NOT carried on the wire today —
   *  OpenClaw sends no start/end timestamp for thinking the way it does for
   *  tool calls (ContentBlock.startedAt/endedAt). Callers pass this only when
   *  a real value is derivable; omit it rather than approximate — the label
   *  falls back to the tenseless "Thought" instead of a wrong duration. */
  durationMs?: number;
}) {
  const text = splitCollidedHeaders(content);
  // Live (streaming) thinking mounts EXPANDED so reasoning is visible as it
  // flows — a collapsed "Thinking…" pill reads as dead air during long
  // thinking phases. The folded history block mounts collapsed as before.
  const [isExpanded, setIsExpanded] = React.useState(!!streaming);
  const userToggledRef = React.useRef(false);
  // Auto-collapse when the live stream ends, unless the user chose a state
  // themselves. (Usually the live item simply unmounts at `done` and the
  // history block mounts collapsed; this covers a block whose `streaming`
  // prop flips false while it stays mounted.)
  React.useEffect(() => {
    if (!streaming && !userToggledRef.current) setIsExpanded(false);
  }, [streaming]);

  // Present tense only while actually live; a settled thought reads in the
  // past, same as a finished tool call ("Read", not "Reading").
  const label = streaming
    ? "Thinking…"
    : durationMs
      ? `Thought for ${formatThinkingDuration(durationMs)}`
      : "Thought";

  return (
    <div className="mb-4 chat-block-in">
      <button
        onClick={() => {
          userToggledRef.current = true;
          setIsExpanded(!isExpanded);
        }}
        className="flex items-center gap-1.5 text-left text-[var(--mute)] hover:text-[var(--ink-soft)] transition-colors"
      >
        {isExpanded ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0" />
        )}
        <span className="text-sm italic">{label}</span>
      </button>
      {isExpanded && (
        <div className="mt-1.5 pl-5 border-l border-[var(--edge-soft)] text-sm text-[var(--mute)]">
          {streaming ? (
            <StreamingMarkdownContent content={text} />
          ) : (
            <MarkdownContent content={text} />
          )}
        </div>
      )}
    </div>
  );
}


export const AGENT_GLYPH_PATH =
    "M11.2 6 C10.4 4.2 8.8 2.5 7 2.5 C5.2 2.5 4 4 4 6 C4 8 5.2 9.5 7 9.5 C8.8 9.5 10.4 7.8 11.2 6 C12 4.2 13.6 2.5 15.4 2.5 C17.2 2.5 18.4 4 18.4 6 C18.4 8 17.2 9.5 15.4 9.5 C13.6 9.5 12 7.8 11.2 6Z";

export function AgentGlyph({
    state = "idle",
    size = 18,
    className,
}: {
    state?: "idle" | "thinking";
    size?: number;
    className?: string;
}) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 24 12"
            fill="none"
            className={cn("agent-glyph", state === "thinking" && "agent-glyph--thinking", className)}
        >
            <path
                className="agent-glyph-base"
                d={AGENT_GLYPH_PATH}
                strokeWidth="1.6"
                fill="none"
                strokeLinecap="round"
                strokeLinejoin="round"
            />
            <path
                className="agent-glyph-tracer"
                d={AGENT_GLYPH_PATH}
                strokeWidth="1.6"
                fill="none"
                strokeLinecap="round"
                strokeLinejoin="round"
            />
        </svg>
    );
}

/**
 * Copy/Share appear once the turn SETTLES, not on hover. The old
 * `opacity-0 group-hover:opacity-100` reveal had two defects, not one: the
 * buttons carried no `onClick` (dead), and hover never fires on touch, so on
 * every phone they were also permanently invisible. Settle-gating fixes both
 * — reachable on every input modality — and Regenerate was dropped rather
 * than shipped as a third dead button (see MessageList.tsx module notes / PR
 * description for why: it has no resend-this-turn path without new
 * cross-component plumbing and backend session-mutation semantics that
 * don't exist yet).
 */
export function AgentHead({
    name,
    state = "idle",
    streaming = false,
    messageText = "",
}: {
    name: string;
    state?: "idle" | "thinking";
    /** True only while this is the actively-streaming tail turn. */
    streaming?: boolean;
    /** Copyable plain text for this turn, e.g. from groupCopyText(group). */
    messageText?: string;
}) {
    const settled = !streaming;
    return (
        <div className="flex items-center gap-2 mb-2">
            <span className="text-xs font-semibold text-[#302d28] tracking-tight">
                {name}
            </span>
            <span className="inline-flex items-center justify-center w-5 h-5">
                <AgentGlyph state={state} size={18} />
            </span>
            {settled && (
                <div className="flex items-center gap-0.5 ml-1 chat-block-in">
                    <Button
                        variant="ghost"
                        size="icon"
                        aria-label="Copy"
                        onClick={() => {
                            navigator.clipboard?.writeText(messageText).catch(() => {});
                        }}
                        className="chat-action-btn h-6 w-6 text-[var(--mute)] hover:text-[var(--ink)] hover:bg-[var(--cream-deeper)]"
                    >
                        <Copy className="h-3 w-3" />
                    </Button>
                    <Button
                        variant="ghost"
                        size="icon"
                        aria-label="Share"
                        onClick={() => {
                            if (typeof navigator.share === "function") {
                                navigator.share({ text: messageText }).catch(() => {});
                            } else {
                                navigator.clipboard?.writeText(messageText).catch(() => {});
                            }
                        }}
                        className="chat-action-btn h-6 w-6 text-[var(--mute)] hover:text-[var(--ink)] hover:bg-[var(--cream-deeper)]"
                    >
                        <Share2 className="h-3 w-3" />
                    </Button>
                </div>
            )}
        </div>
    );
}

/**
 * Walks a HistoryMessage's content blocks and renders them in order:
 * text → MarkdownContent, thinking → ThinkingBlock, tool_use →
 * ToolUseIndicator paired with its matching tool_result. tool_result
 * blocks are skipped — they're rendered implicitly as part of the
 * matching tool_use pair.
 */
function MessageBlocks({
  message,
  onOpenFile,
  interactive = true,
}: {
  message: HistoryMessage;
  onOpenFile?: (path: string) => void;
  /** Forwarded to ChatErrorCard — only the newest bubble may render the live
   *  conversion paywall, so repeated capped turns do not stack purchase forms
   *  each holding their own credit amount. */
  interactive?: boolean;
}) {
  // `content` may be a block array, a bare string, or absent with the body on
  // the top-level `text` field (older chat.history rows, cron run transcripts).
  // resolveMessageBlocks collapses all three — without it each non-array shape
  // renders as an empty bubble.
  const blocks: ContentBlock[] = resolveMessageBlocks(message);
  const pairs = pairToolBlocks(blocks);
  const renderedToolIds = new Set(pairs.map((p) => p.toolUseId));
  const rendered: React.ReactNode[] = [];

  // Consecutive tool calls accumulate here and flush as ONE trace. Any non-tool
  // block (text, thinking, a hire card) is a step boundary and flushes first, so
  // "tool, tool, text, tool" renders as two traces around the prose rather than
  // three scattered indicators — which is what the old one-pill-per-call did.
  let pendingCalls: ToolCall[] = [];
  const flushTrace = () => {
    if (pendingCalls.length === 0) return;
    const calls = pendingCalls;
    pendingCalls = [];
    rendered.push(
      <ToolActivityTrace
        key={`trace:${calls[0]!.id}`}
        calls={calls}
        // A trace is settled once every call in it has a terminal status. The
        // live turn's tail group keeps its rows; older groups fold.
        settled={calls.every((c) => c.status !== "running")}
      />,
    );
  };

  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i];
    if (isToolResultBlock(b)) {
      // Rendered as part of the matching tool_use's pair.
      continue;
    }
    if (isToolUseBlock(b)) {
      const id = resolveToolUseId(b);
      if (id && renderedToolIds.has(id)) {
        const pair = pairs.find((p) => p.toolUseId === id)!;
        const resultBlock = pair.toolResultBlock as
          | { content?: unknown; result?: unknown; isError?: boolean }
          | undefined;
        const status: "running" | "done" | "error" = resultBlock
          ? resultBlock.isError
            ? "error"
            : "done"
          : "running";
        // A completed recommend_agents call renders as a hire card instead of
        // a tool pill. parseHireSuggestion returns null for every other tool
        // result, so anything else falls through untouched.
        const hireSuggestion =
          status === "done"
            ? parseHireSuggestion(resultBlock?.content ?? resultBlock?.result)
            : null;
        if (hireSuggestion) {
          // A hire card is prose-weight content, not a step — it ends the run.
          flushTrace();
          rendered.push(<HireCard key={`hire:${id}`} suggestion={hireSuggestion} />);
          renderedToolIds.delete(id);
          continue;
        }
        pendingCalls.push({
          id,
          name: b.name ?? "tool",
          args: resolveToolArgs(b),
          result: resultBlock?.content ?? resultBlock?.result,
          status,
          progressText: (b as ContentBlock).progressText,
          startedAt: (b as ContentBlock).startedAt,
          endedAt: (b as ContentBlock).endedAt,
        });
        renderedToolIds.delete(id);
      }
      continue;
    }
    if (isThinkingBlock(b)) {
      const text = (b.thinking ?? b.text ?? "") as string;
      if (text.length > 0) {
        flushTrace();
        rendered.push(<ThinkingBlock key={`think:${i}`} content={text} />);
      }
      continue;
    }
    if (isTextBlock(b)) {
      const text = (b.text ?? "") as string;
      if (text.length > 0) {
        flushTrace();
        // Surface terminal agent errors via the typed card when the message
        // carries an errorCode, otherwise just render plain markdown.
        if (message.errorCode && text.startsWith("Error: ")) {
          rendered.push(
            <ChatErrorCard
              key={`err:${i}`}
              message={text.slice("Error: ".length)}
              code={message.errorCode}
              interactive={interactive}
            />
          );
        } else {
          rendered.push(
            <MarkdownContent
              key={`text:${i}`}
              content={text}
              onOpenFile={onOpenFile}
            />
          );
        }
      }
      continue;
    }
    // Unknown block — skip.
  }
  // A turn that ends on tool calls (still running, or finished with no closing
  // prose) still has to render them.
  flushTrace();
  return <>{rendered}</>;
}

function UserMessageRow({ message }: { message: HistoryMessage }) {
  // Same three-shape normalization as MessageBlocks — a typed user message
  // arrives from chat.history with `content` as a bare string, which the old
  // `Array.isArray ? … : message.text ?? ""` ternary rendered as empty.
  const text = resolveMessageBlocks(message)
    .filter(isTextBlock)
    .map((b) => (b.text ?? "") as string)
    .join("");
  return (
    <div className="flex w-full justify-end" data-role="user">
      <div className="flex flex-col min-w-0 max-w-[85%]">
        <div className="relative text-sm leading-7 bg-[#f0ebe2] text-[var(--ink)] rounded-2xl rounded-br-md px-4 py-2.5">
          <div className="wrap-break-word whitespace-pre-wrap">{text}</div>
        </div>
      </div>
    </div>
  );
}

/**
 * Value-equality for render items. buildChatItems rebuilds the item ARRAY
 * (fresh wrapper objects) on every streaming paint, so referential memo on
 * the wrappers never hits — compare the cheap identity fields instead:
 * `key` + kind-specific payload (string identity for text, object identity
 * for messages — history message objects are stable across paints).
 */
function itemEquals(a: ChatRenderItem, b: ChatRenderItem): boolean {
  if (a === b) return true;
  if (a.kind !== b.kind || a.key !== b.key) return false;
  switch (a.kind) {
    case "message":
      return a.message === (b as typeof a).message;
    case "stream":
    case "stream-segment":
    case "live-thinking":
      return a.text === (b as typeof a).text;
    case "divider":
      return a.label === (b as typeof a).label;
    case "btw": {
      const bb = b as typeof a;
      return a.text === bb.text && a.question === bb.question && a.isError === bb.isError;
    }
    default:
      return false;
  }
}

/**
 * Renders one item from a RenderGroup. Stream / live-thinking / divider
 * items map directly to existing block primitives; {kind:"message"} items
 * defer to MessageBlocks which walks the message's content[] array.
 *
 * Memoized on item VALUE (itemEquals): during streaming only the growing
 * tail item re-renders — history messages and settled segments skip
 * reconciliation entirely instead of re-walking their block trees per paint.
 */
const ItemRenderer = React.memo(
  function ItemRenderer({
    item,
    onOpenFile,
    interactive = true,
  }: {
    item: ChatRenderItem;
    onOpenFile?: (path: string) => void;
    /** True only for the newest bubble of the newest assistant group — gates
     *  ChatErrorCard's live conversion paywall so it cannot stack. Included in
     *  the memo comparator below, or the flag would never propagate. */
    interactive?: boolean;
  }) {
    if (item.kind === "divider") {
      return (
        <div className="my-3 flex items-center gap-2 text-[11px] text-[var(--mute)]">
          <span className="flex-1 h-px bg-[var(--edge)]" />
          <span className="uppercase tracking-wide">{item.label}</span>
          <span className="flex-1 h-px bg-[var(--edge)]" />
        </div>
      );
    }
    if (item.kind === "stream" || item.kind === "stream-segment") {
      // Only "stream" is the live growing tail — "stream-segment"s are past
      // text a tool call already interrupted, so they never carry the caret.
      return (
        <StreamingMarkdownContent
          content={item.text}
          onOpenFile={onOpenFile}
          streaming={item.kind === "stream"}
        />
      );
    }
    if (item.kind === "live-thinking") {
      return <ThinkingBlock content={item.text} streaming />;
    }
    if (item.kind === "message") {
      return <MessageBlocks message={item.message} onOpenFile={onOpenFile} interactive={interactive} />;
    }
    return null;
  },
  (prev, next) =>
    prev.onOpenFile === next.onOpenFile &&
    prev.interactive === next.interactive &&
    itemEquals(prev.item, next.item),
);

/**
 * A `/btw` (detached side-channel) answer rendered as a distinct, non-thread
 * side card — labeled "BTW · not saved" (it is never written to the transcript
 * / history), the question in muted text, the answer as the body. Error styling
 * reuses the existing chat error tokens (#fce4ec / #a5311f / #f8bbd0). Mirrors
 * OpenClaw's "Not saved to chat history" side card.
 */
function BtwSideCard({
  question,
  text,
  isError,
  onOpenFile,
}: {
  question: string;
  text: string;
  isError?: boolean;
  onOpenFile?: (path: string) => void;
}) {
  return (
    <div
      data-testid="btw-card"
      className={cn(
        "rounded-lg border px-3 py-2.5 text-sm",
        isError ? "border-[#f8bbd0] bg-[#fce4ec]" : "border-[var(--edge)] bg-[var(--cream-deeper)]",
      )}
    >
      <div className="flex items-center gap-1.5 mb-1.5">
        <span
          className={cn(
            "text-[11px] font-semibold uppercase tracking-wide",
            isError ? "text-[#a5311f]" : "text-[var(--forest)]",
          )}
        >
          BTW
        </span>
        <span className="text-[11px] text-[#b5ae9e]">· not saved</span>
      </div>
      {question && <div className="text-xs text-[var(--mute)] mb-1.5">{question}</div>}
      <div className={cn("leading-7", isError ? "text-[#a5311f]" : "text-[#302d28]")}>
        <MarkdownContent content={text} onOpenFile={onOpenFile} />
      </div>
    </div>
  );
}

/** Value-equality for groups: same key/side and element-wise equal items. */
function groupEquals(a: RenderGroup, b: RenderGroup): boolean {
  if (a === b) return true;
  if (a.key !== b.key || a.side !== b.side || a.items.length !== b.items.length) return false;
  for (let i = 0; i < a.items.length; i++) {
    if (!itemEquals(a.items[i], b.items[i])) return false;
  }
  return true;
}

/**
 * One visual bubble group. User-side groups expand to one bubble per item
 * (each user message stands alone). Assistant-side groups render a single
 * rounded container with all items flowing inside it.
 *
 * Memoized on group VALUE (groupEquals) — groupBySide rebuilds the group
 * array per streaming paint, so referential memo would never hit. With the
 * comparator, every group except the one containing the growing tail bails
 * out per paint.
 */
const BubbleGroup = React.memo(BubbleGroupImpl, (prev, next) =>
  prev.agentName === next.agentName &&
  prev.isStreaming === next.isStreaming &&
  prev.isLastAssistantGroup === next.isLastAssistantGroup &&
  prev.paywallItemKey === next.paywallItemKey &&
  prev.onOpenFile === next.onOpenFile &&
  groupEquals(prev.group, next.group),
);

/** One rendered slot: either a normal item, or a run of coalesced tool calls. */
type Renderable =
  | { kind: "item"; item: ChatRenderItem }
  | { kind: "tool-run"; key: string; calls: ToolCall[] };

/**
 * Extracts a message's tool calls, or null if it is not a pure tool message.
 *
 * "Pure" matters: a message mixing prose with tool calls must keep its own
 * renderer, because MessageBlocks already groups tool blocks WITHIN a message
 * and knows where the prose sits between them. Only whole tool-only messages
 * are eligible to merge with their neighbours.
 */
function toolCallsOfPureToolMessage(message: HistoryMessage): ToolCall[] | null {
  const blocks = resolveMessageBlocks(message);
  if (blocks.length === 0) return null;
  if (message.errorCode) return null;
  const pairs = pairToolBlocks(blocks);
  if (pairs.length === 0) return null;

  const calls: ToolCall[] = [];
  for (const b of blocks) {
    if (isToolResultBlock(b)) continue;
    if (!isToolUseBlock(b)) return null; // prose/thinking present — not pure
    const id = resolveToolUseId(b);
    if (!id) return null;
    const pair = pairs.find((p) => p.toolUseId === id);
    if (!pair) return null;
    const resultBlock = pair.toolResultBlock as
      | { content?: unknown; result?: unknown; isError?: boolean }
      | undefined;
    const status: ToolCall["status"] = resultBlock
      ? resultBlock.isError
        ? "error"
        : "done"
      : "running";
    // A completed recommend_agents call renders as a hire card, which is prose-
    // weight content — leave the whole message to the normal renderer.
    if (status === "done" && parseHireSuggestion(resultBlock?.content ?? resultBlock?.result)) {
      return null;
    }
    calls.push({
      id,
      name: b.name ?? "tool",
      args: resolveToolArgs(b),
      result: resultBlock?.content ?? resultBlock?.result,
      status,
      progressText: (b as ContentBlock).progressText,
      startedAt: (b as ContentBlock).startedAt,
      endedAt: (b as ContentBlock).endedAt,
    });
  }
  return calls.length > 0 ? calls : null;
}

/**
 * Merges consecutive tool-only messages into one trace, so a multi-step turn
 * reads as a single run instead of one indicator per call.
 *
 * This happens at RENDER time, deliberately. The live path emits one message
 * per tool call and that 1:1 shape is load-bearing upstream — buildChatItems
 * interleaves narration segments with tool messages by index, and per-message
 * timestamps are what keep React keys stable across progress ticks. Collapsing
 * the data instead of the view broke both (stranded narration, and every
 * expanded row snapping shut whenever any sibling call ticked).
 *
 * Anything that is not a pure tool message — prose, thinking, a stream segment,
 * an error card, a hire card — ends the run, which is also what makes the
 * grouping semantically right: the trace covers the steps taken between two
 * things the agent said.
 */
function coalesceToolRuns(items: ChatRenderItem[]): Renderable[] {
  const out: Renderable[] = [];
  let run: { key: string; calls: ToolCall[] } | null = null;
  const flush = () => {
    if (run) out.push({ kind: "tool-run", ...run });
    run = null;
  };

  for (const item of items) {
    const calls = item.kind === "message" ? toolCallsOfPureToolMessage(item.message) : null;
    if (!calls) {
      flush();
      out.push({ kind: "item", item });
      continue;
    }
    if (run) {
      run.calls.push(...calls);
    } else {
      // Keyed on the first call's id, not the message key: a toolCallId is
      // immutable, while a message key carries a timestamp that moves on every
      // progress tick and would remount the whole trace.
      run = { key: `tool-run:${calls[0]!.id}`, calls: [...calls] };
    }
  }
  flush();
  return out;
}

/**
 * Copyable plain text for one assistant bubble group, in transcript order.
 * Tool calls, thinking, and hire cards carry no text of their own and are
 * skipped — this only has to be a faithful plain-text copy of what the
 * agent said, not a pixel match of the rendered bubble.
 */
function groupCopyText(group: RenderGroup): string {
  const parts: string[] = [];
  for (const item of group.items) {
    if (item.kind === "message") {
      const text = resolveMessageBlocks(item.message)
        .filter(isTextBlock)
        .map((b) => (b.text ?? "") as string)
        .join("");
      if (text) parts.push(text);
    } else if (item.kind === "stream" || item.kind === "stream-segment") {
      if (item.text) parts.push(item.text);
    }
  }
  return parts.join("\n\n");
}

function BubbleGroupImpl({
  group,
  agentName,
  isStreaming,
  isLastAssistantGroup,
  paywallItemKey,
  onOpenFile,
}: {
  group: RenderGroup;
  agentName?: string;
  isStreaming?: boolean;
  isLastAssistantGroup?: boolean;
  /** Key of the ONE transcript item allowed to render the live conversion
   *  paywall — the newest trial-coded error card. See the computation at the
   *  call site for why this is not simply the newest bubble. */
  paywallItemKey?: string | null;
  onOpenFile?: (path: string) => void;
}) {
  if (group.side === "btw") {
    return (
      <div className="flex w-full justify-start" data-role="btw">
        <div className="flex flex-col gap-2 min-w-0 max-w-[85%]">
          {group.items.map((item) =>
            item.kind === "btw" ? (
              <BtwSideCard
                key={item.key}
                question={item.question}
                text={item.text}
                isError={item.isError}
                onOpenFile={onOpenFile}
              />
            ) : null,
          )}
        </div>
      </div>
    );
  }
  if (group.side === "user") {
    return (
      <>
        {group.items.map((item) =>
          item.kind === "message" ? (
            <UserMessageRow key={item.key} message={item.message} />
          ) : null
        )}
      </>
    );
  }
  // Assistant side
  //
  // `w-full` when any item carries an errorCode: ChatErrorCard's conversion
  // paywall wraps ProviderPicker, whose root is `@container` —
  // `container-type: inline-size` zeroes the element's intrinsic width, and
  // this column is shrink-to-fit (sized from intrinsic content), so without an
  // explicit width the bubble collapses to the AgentHead's ~120px and the
  // purchase form renders as a needle-thin strip that no window resize can
  // widen. Error cards are full-width cards; plain bubbles keep hugging.
  const hasErrorCard = group.items.some(
    (item) => item.kind === "message" && Boolean(item.message.errorCode),
  );
  const renderables = coalesceToolRuns(group.items);
  return (
    <div className="flex w-full justify-start" data-role="assistant">
      <div
        className={cn(
          "flex flex-col min-w-0 max-w-[85%] relative",
          hasErrorCard && "w-full",
        )}
      >
        <AgentHead
          name={agentName || "Assistant"}
          state={isStreaming && isLastAssistantGroup ? "thinking" : "idle"}
          streaming={Boolean(isStreaming && isLastAssistantGroup)}
          messageText={groupCopyText(group)}
        />
        <div className="relative text-sm leading-7 text-[#302d28] w-full">
          {renderables.map((entry) =>
            entry.kind === "tool-run" ? (
              <ToolActivityTrace
                key={entry.key}
                calls={entry.calls}
                settled={entry.calls.every((c) => c.status !== "running")}
              />
            ) : (
            <ItemRenderer
              key={entry.item.key}
              item={entry.item}
              onOpenFile={onOpenFile}
              // Exactly ONE item in the whole transcript may render the live
              // paywall: the newest trial-coded error card (paywallItemKey,
              // computed once at the top level). Not "the newest bubble" —
              // a mid-run cap emits the reclassified trial error and then
              // OpenClaw's raw rate-limited frame ~20ms later, so the newest
              // bubble is reliably NOT the cap card and that predicate
              // demoted the paywall to a settings link in exactly the
              // scenario it exists for (observed live in prod). The
              // one-key rule still prevents stacked pickers with divergent
              // credit-amount state.
              interactive={entry.item.key === paywallItemKey}
            />
            ),
          )}
        </div>
      </div>
    </div>
  );
}

function TypingPlaceholder({ agentName }: { agentName?: string }) {
  return (
    <div
      data-testid="typing-placeholder"
      className="flex w-full justify-start"
    >
      <div className="flex flex-col min-w-0 max-w-[85%]">
        <AgentHead name={agentName || "Assistant"} state="thinking" streaming />
      </div>
    </div>
  );
}

export interface MessageListHandle {
  scrollToBottom: () => void;
}

export const MessageList = React.forwardRef<MessageListHandle, MessageListProps>(
  function MessageList(
    { renderGroups, isStreaming, isTyping, agentName, onOpenFile, hasOlder, isLoadingOlder, onLoadOlder },
    ref
  ) {
    const { containerRef, scrollToBottom } = useScrollToBottom();

    // Loading an older page prepends messages at the top of a bottom-anchored
    // transcript, which would otherwise shove the viewport down by the height
    // of the newly-inserted content. Pin the distance from the bottom before
    // the fetch, then restore it once the DOM has the new messages painted —
    // mutate scrollTop on the container directly, never scrollIntoView (see
    // useScrollToBottom.ts for why).
    const handleLoadOlder = React.useCallback(() => {
      const c = containerRef.current;
      const before = c ? c.scrollHeight - c.scrollTop : null;
      Promise.resolve(onLoadOlder?.()).then(() => {
        requestAnimationFrame(() => {
          const el = containerRef.current;
          if (el && before !== null) el.scrollTop = el.scrollHeight - before;
        });
      });
    }, [onLoadOlder, containerRef]);

    // Track whether the user is at (or near) the bottom of the scroll
    // container. While true, incoming streamed chunks pull the view down.
    // If the user scrolls up to re-read an earlier message, we stop
    // pulling — they'll scroll back down themselves. Threshold matches
    // Tailwind's `space-y-10` (~40px) plus a comfortable margin.
    const isNearBottomRef = React.useRef(true);
    const hasMountedRef = React.useRef(false);
    const prevTotalItemsRef = React.useRef(0);
    const prevLastTailLengthRef = React.useRef(0);
    const prevLastSideRef = React.useRef<RenderSide | undefined>(undefined);

    React.useImperativeHandle(ref, () => ({
      scrollToBottom,
    }));

    // Scroll events can burst several times per frame (touch / momentum
    // scrolling), and every run measures the container. Coalesce them to one
    // measurement per animation frame: the frame's FIRST event measures, the
    // rest are dropped until the frame turns over.
    //
    // The measurement stays synchronous with that event rather than being
    // deferred into the rAF callback. A deferred read would land after the
    // next streaming commit, where `scrollHeight` has already grown by the
    // freshly appended chunk — the distance would read as "user scrolled up"
    // and silently kill autoscroll mid-stream. The pinned flag is only ever
    // allowed to change at scroll time.
    const scrollFrameRef = React.useRef<number | null>(null);

    const handleScroll = React.useCallback(() => {
      if (scrollFrameRef.current !== null) return;
      const c = containerRef.current;
      if (!c) return;
      const distance = c.scrollHeight - c.scrollTop - c.clientHeight;
      isNearBottomRef.current = distance < 120;
      scrollFrameRef.current = requestAnimationFrame(() => {
        scrollFrameRef.current = null;
      });
    }, [containerRef]);

    React.useEffect(
      () => () => {
        if (scrollFrameRef.current !== null) cancelAnimationFrame(scrollFrameRef.current);
      },
      [],
    );

    // Auto-scroll signals. Plan §6 keys on `totalItems` (sum of items across
    // groups) instead of `messages.length`, plus a "tail item's text-length
    // grew" heuristic for streaming.
    const totalItems = React.useMemo(
      () => renderGroups.reduce((acc, g) => acc + g.items.length, 0),
      [renderGroups],
    );

    // Length of the trailing item's text (only meaningful for stream /
    // stream-segment / live-thinking items where the same item grows).
    const lastTailLength = React.useMemo(() => {
      const lastGroup = renderGroups[renderGroups.length - 1];
      if (!lastGroup) return 0;
      const lastItem = lastGroup.items[lastGroup.items.length - 1];
      if (!lastItem) return 0;
      if (lastItem.kind === "stream" || lastItem.kind === "stream-segment" || lastItem.kind === "live-thinking") {
        return lastItem.text.length;
      }
      return 0;
    }, [renderGroups]);

    const lastSide = renderGroups[renderGroups.length - 1]?.side;

    // Auto-scroll rules:
    //   - First render (agent switch / history load) → snap to bottom.
    //   - totalItems increased → a new item arrived; snap if near bottom,
    //     or unconditionally if the new group's tail is a user message
    //     (they just sent it, they want to see it).
    //   - totalItems same, tail item's text grew → streaming chunk on
    //     the trailing assistant item; snap only if near bottom (so a
    //     user who scrolled up isn't yanked back while reading).
    React.useEffect(() => {
      const container = containerRef.current;
      if (!container) return;

      const itemsIncreased = totalItems > prevTotalItemsRef.current;
      const tailGrew =
        totalItems === prevTotalItemsRef.current &&
        lastTailLength > prevLastTailLengthRef.current;
      const newUserMessage = itemsIncreased && lastSide === "user";
      const isFirstPaintWithItems = !hasMountedRef.current && totalItems > 0;

      // Snap by setting scrollTop on the container — never scrollIntoView,
      // which also scrolls outer scrollable ancestors (that once yanked the
      // whole workspace shell out of the viewport).
      const snapToBottom = () => {
        container.scrollTop = container.scrollHeight;
      };

      if (isFirstPaintWithItems || newUserMessage) {
        snapToBottom();
        isNearBottomRef.current = true;
        hasMountedRef.current = true;
      } else if ((itemsIncreased || tailGrew) && isNearBottomRef.current) {
        snapToBottom();
      }

      if (totalItems > 0) hasMountedRef.current = true;
      prevTotalItemsRef.current = totalItems;
      prevLastTailLengthRef.current = lastTailLength;
      prevLastSideRef.current = lastSide;
    }, [totalItems, lastTailLength, lastSide, containerRef]);

    // The "typing placeholder" shows the agent header (animated thinking
    // glyph) during the window between `sendMessage` and the first
    // streamed chunk. Since the flat-timeline rendering creates assistant
    // items lazily on first event, nothing else is on screen to signal
    // "the agent received your message and is working." This placeholder
    // is rendered only when streaming is active AND the trailing group
    // side is user (or no groups yet).
    const showTypingPlaceholder =
      !!isTyping && (renderGroups.length === 0 || lastSide === "user");

    // Find the index of the last assistant-side group (for "thinking"
    // glyph wiring). We only animate the head of the bottommost assistant
    // bubble while streaming.
    let lastAssistantGroupIndex = -1;
    for (let i = renderGroups.length - 1; i >= 0; i--) {
      if (renderGroups[i].side === "assistant") {
        lastAssistantGroupIndex = i;
        break;
      }
    }

    // The ONE item allowed to render the live conversion paywall: the newest
    // trial-coded error card anywhere in the transcript. Searched backward so
    // repeated capped turns bless only the latest (no stacked pickers with
    // divergent credit-amount state), and searched by CODE rather than
    // position because the cap card is reliably NOT the last bubble — a
    // mid-run cap emits the reclassified trial error and then OpenClaw's raw
    // rate-limited frame ~20ms behind it (prod logs: 17:58:44.187 vs .208).
    let paywallItemKey: string | null = null;
    outer: for (let i = renderGroups.length - 1; i >= 0; i--) {
      if (renderGroups[i].side !== "assistant") continue;
      const items = renderGroups[i].items;
      for (let j = items.length - 1; j >= 0; j--) {
        const it = items[j];
        if (it.kind === "message" && isTrialPaywallCode(it.message.errorCode)) {
          paywallItemKey = it.key;
          break outer;
        }
      }
    }

    return (
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 min-h-0 overflow-y-auto p-4 md:px-8"
        data-lenis-prevent
      >
        {/* `chat-transcript` occludes off-screen groups (content-visibility) —
            see globals.css. Keeps a long history off the layout hot path. */}
        <div className="max-w-3xl mx-auto space-y-10 py-8 chat-transcript">
          {hasOlder && (
            <div className="flex justify-center pb-3">
              <button
                type="button"
                onClick={handleLoadOlder}
                disabled={isLoadingOlder}
                className="text-[11px] text-[var(--mute)] hover:text-[#302d28] disabled:opacity-60 px-3 py-1 rounded-full border border-[var(--edge)] bg-[var(--cream-deeper)]"
              >
                {isLoadingOlder ? "Loading earlier messages…" : "Load earlier messages"}
              </button>
            </div>
          )}
          {renderGroups.map((group, idx) => (
            <BubbleGroup
              key={group.key}
              group={group}
              agentName={agentName}
              isStreaming={isStreaming}
              isLastAssistantGroup={idx === lastAssistantGroupIndex}
              paywallItemKey={paywallItemKey}
              onOpenFile={onOpenFile}
            />
          ))}
          {showTypingPlaceholder && <TypingPlaceholder agentName={agentName} />}
        </div>
      </div>
    );
  }
);
