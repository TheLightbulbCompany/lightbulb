import * as React from "react";
import { cn } from "@/lib/utils";
import { stripExternalContentMarkers } from "@/lib/externalContentMarkers";
import { Copy, Check, Share2, ChevronDown, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useScrollToBottom } from "@/hooks/useScrollToBottom";
import { ChatErrorCard } from "@/components/chat/ChatErrorCard";
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
import { ToolActivityTrace, type ToolCall } from "@/components/chat/ToolActivityTrace";
import { TranscriptSelectionBar } from "@/components/chat/TranscriptSelectionBar";
import {
  pairToolBlocks,
  isToolUseBlock,
  isToolResultBlock,
  isThinkingBlock,
  isTextBlock,
  resolveToolUseId,
  resolveToolArgs,
  resolveMessageBlocks,
  type ToolBlockPair,
} from "@/hooks/internal/toolBlockExtract";
import type {
  ChatRenderItem,
  ContentBlock,
  RenderGroup,
  RenderSide,
  HistoryMessage,
} from "@/hooks/internal/types";
// Exec approvals no longer render in chat — that rail was deleted with the
// gateway pool. Tool activity is rendered by ToolActivityTrace, which
// represents execution state only — running / done / error.

interface MessageListProps {
  renderGroups: RenderGroup[];
  agentName?: string;
  isStreaming?: boolean;
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
function resolveHighlightLanguage(language: string): string {
  const normalized = language.toLowerCase();
  return REGISTERED_LANGUAGES.has(normalized) ? normalized : PLAIN_LANGUAGE;
}

/**
 * Exported (was module-private) so CodeBlock.test.tsx can exercise the copy
 * affordance directly, matching how AgentHead/ThinkingBlock are exported for
 * their own tests.
 */
export function CodeBlock({ className, children, ...props }: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) {
  const match = /language-(\w+)/.exec(className || "");
  // Exact source, trailing newline(s) included — this is what gets copied.
  // `codeString` below strips exactly one trailing newline for RENDER only
  // (Prism otherwise shows a trailing blank line); the two must stay
  // separate or copy silently loses a byte the fence actually had.
  const rawSource = String(children);
  const codeString = rawSource.replace(/\n$/, "");
  const [copied, setCopied] = React.useState(false);
  const copyTimerRef = React.useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  React.useEffect(() => () => clearTimeout(copyTimerRef.current), []);

  if (!match) {
    // Inline code
    return (
      <code className="bg-[var(--cream-deeper)] rounded-sm px-1.5 py-0.5 text-sm text-[var(--midnight)]" {...props}>
        {children}
      </code>
    );
  }

  const handleCopy = async () => {
    // navigator.clipboard is undefined on insecure origins (plain http, not
    // localhost) — a bare call throws instead of silently failing.
    if (!navigator.clipboard) return;
    try {
      // `await`, not `.writeText(...).then(...)` — a browser/extension that
      // patches `clipboard.writeText` to return something non-promise (or
      // undefined) makes `.then` throw synchronously inside this click
      // handler. `await` has no such requirement: it resolves a plain value
      // as itself instead of demanding a `.then` method, so a nonconformant
      // implementation degrades to "treated as succeeded" instead of an
      // uncaught TypeError. A genuine rejection (real clipboard failure)
      // still lands in the catch below and sets no success state.
      await navigator.clipboard.writeText(rawSource);
      setCopied(true);
      clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard write failed — no success state.
    }
  };

  // Fenced code block
  return (
    <div className="relative group/code my-4 rounded-lg overflow-hidden border border-[var(--edge)]">
      <div className="flex items-center justify-between px-4 py-2 bg-[var(--cream-deep)] border-b border-[var(--edge)]">
        <span className="text-xs text-[var(--midnight-muted)]">{match[1]}</span>
        <button
          onClick={handleCopy}
          aria-label={copied ? "Copied" : "Copy code"}
          className={cn(
            "chat-code-copy-btn text-xs transition-colors flex items-center gap-1",
            copied ? "text-[var(--midnight)]" : "text-[var(--midnight-muted)] hover:text-[var(--midnight)]",
          )}
        >
          {/* Icon swaps Copy -> Check; the "Copy" label never changes text,
              so the button's width never shifts (the brief's "reserve the
              space" and "don't swap text of differing width" are the same
              fix here: don't swap the text at all). aria-label still flips
              to "Copied" so the confirmation reaches screen readers too. */}
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          Copy
        </button>
      </div>
      <SyntaxHighlighter
        style={oneDark}
        language={resolveHighlightLanguage(match[1])}
        PreTag="div"
        customStyle={{ margin: 0, borderRadius: 0, background: "var(--cream)" }}
      >
        {codeString}
      </SyntaxHighlighter>
    </div>
  );
}

export const MarkdownContent = React.memo(function MarkdownContent({
  content,
}: {
  content: string;
}) {
  // The injection-defense markers are internal plumbing wherever they surface
  // (echoed into assistant prose, folded history text) — see
  // externalContentMarkers.ts. File paths stay plain text: nothing here can
  // open one (the old isol8-file:// links rendered as dead anchors).
  const processedContent = React.useMemo(
    () => stripExternalContentMarkers(content),
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
          const isSafe = !href?.match(/^(javascript|data|vbscript):/i);
          return (
            <a href={isSafe ? href : '#'} target="_blank" rel="noopener noreferrer" className="text-[var(--midnight)] hover:underline">
              {children}
            </a>
          );
        },
        ul: ({ children }) => <ul className="list-disc list-inside mb-3 space-y-1">{children}</ul>,
        ol: ({ children }) => <ol className="list-decimal list-inside mb-3 space-y-1">{children}</ol>,
        li: ({ children }) => <li className="ml-2">{children}</li>,
        blockquote: ({ children }) => (
          <blockquote className="border-l-2 border-[var(--edge)] pl-4 text-[var(--midnight-muted)] my-3">{children}</blockquote>
        ),
        table: ({ children }) => (
          <div className="overflow-x-auto my-4">
            <table className="w-full border-collapse border border-[var(--edge)] text-sm">{children}</table>
          </div>
        ),
        thead: ({ children }) => <thead className="bg-[var(--cream-deep)]">{children}</thead>,
        th: ({ children }) => <th className="border border-[var(--edge)] px-3 py-2 text-left font-medium">{children}</th>,
        td: ({ children }) => <td className="border border-[var(--edge)] px-3 py-2">{children}</td>,
        tr: ({ children }) => <tr className="even:bg-[var(--cream-deep)]">{children}</tr>,
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
 *
 * Exported for unit tests ONLY (same convention as CodeBlock/AgentHead/
 * ThinkingBlock above). Kept exported deliberately: the prefix-stability
 * property test — every settled block stays byte-identical as the tail
 * grows, which is what makes the per-block memo hit while streaming — has
 * no equivalent through the rendered DOM, where a block is markdown, not
 * its source text.
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
  streaming,
}: {
  content: string;
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
            <MarkdownContent content={block} />
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
function splitCollidedHeaders(content: string): string {
  return content.replace(/\*\*(?=\*\*)/g, "**\n\n");
}

/**
 * Exported for unit tests ONLY. Kept exported deliberately: its collapse
 * state machine (mount expanded while streaming, auto-collapse on settle,
 * a user toggle that survives the settle) spans a streaming→settled
 * transition that MessageList expresses as an item-KIND change, so driving
 * it through the public component remounts the block and loses the state
 * the tests exist to pin.
 */
export function ThinkingBlock({
  content,
  streaming,
}: {
  content: string;
  streaming?: boolean;
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
  // past, same as a finished tool call ("Read", not "Reading"). No duration:
  // OpenClaw sends no start/end timestamp for thinking the way it does for
  // tool calls (ContentBlock.startedAt/endedAt), so there's nothing real to
  // show — a fabricated number would be worse than none.
  const label = streaming ? "Thinking…" : "Thought";

  return (
    <div className="mb-4 chat-block-in">
      <button
        onClick={() => {
          userToggledRef.current = true;
          setIsExpanded(!isExpanded);
        }}
        className="chat-thinking-trigger flex items-center gap-1.5 text-left text-[var(--midnight-muted)] hover:text-[var(--midnight-soft)] transition-colors"
      >
        {isExpanded ? (
          <ChevronDown className="h-3 w-3 shrink-0" />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0" />
        )}
        <span className="text-sm italic">{label}</span>
      </button>
      {isExpanded && (
        <div className="mt-1.5 pl-5 border-l border-[var(--edge-soft)] text-sm text-[var(--midnight-muted)]">
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


/*
 * The thinking indicator.
 *
 * This was a figure-eight that rotated to a horizontal infinity and chased a
 * tracer round the loop — the isol8 mark, animated. Under the Lightbulb
 * identity the mark is a lamp, so the indicator is the lamp's own filament
 * heating and cooling while the agent works.
 *
 * The cycle is deliberately ASYMMETRIC: fast attack, slow decay, the way an
 * incandescent filament actually behaves. A 50/50 on-off blink reads as a
 * broken bulb — an error signal — which is the opposite of what a running
 * agent should look like.
 *
 * The exported name, props and call sites are unchanged, so AgentHead and
 * StreamingStatusBar keep working without edits.
 */
const AGENT_GLYPH_PATH =
    "M 86,150 L 72,132 L 72,76 A 28,32 0 0 1 128,76 L 128,132 L 114,150 Z";

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
            viewBox="62 40 76 148"
            fill="none"
            aria-hidden="true"
            className={cn("agent-glyph", state === "thinking" && "agent-glyph--thinking", className)}
        >
            <path className="agent-glyph-glass" d={AGENT_GLYPH_PATH} strokeWidth="5" />
            <g className="agent-glyph-cage" strokeWidth="4" strokeLinejoin="round">
                <path d="M 87,74 L 95,74 L 101,146 L 99,146 Z" />
                <path d="M 96,74 L 104,74 L 101.4,146 L 98.6,146 Z" />
                <path d="M 105,74 L 113,74 L 101,146 L 99,146 Z" />
                <path d="M 74,96 H 126 M 82,120 H 118" fill="none" />
            </g>
            <g className="agent-glyph-collar">
                <rect x="86" y="150" width="28" height="7" rx="1" />
                <rect x="87" y="160" width="26" height="7" rx="1" />
                <rect x="89" y="170" width="22" height="6" rx="1" />
            </g>
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
            <span className="text-xs font-semibold text-[var(--midnight)]">
                {name}
            </span>
            <span className="inline-flex items-center justify-center w-5 h-5">
                <AgentGlyph state={state} size={16} />
            </span>
            {settled && (
                <div className="flex items-center gap-0.5 ml-1">
                    <Button
                        variant="ghost"
                        size="icon"
                        aria-label="Copy"
                        onClick={async () => {
                            // await, not `.writeText(...).catch(...)` — see CodeBlock's
                            // handleCopy for why a patched clipboard.writeText that
                            // returns something non-promise would otherwise throw
                            // synchronously inside this click handler.
                            if (!navigator.clipboard) return;
                            try {
                                await navigator.clipboard.writeText(messageText);
                            } catch {
                                // Clipboard write failed — no success state to reset.
                            }
                        }}
                        className="chat-action-btn h-6 w-6 text-[var(--midnight-muted)] hover:text-[var(--midnight)] hover:bg-[var(--cream-deep)]"
                    >
                        <Copy className="h-3 w-3" />
                    </Button>
                    <Button
                        variant="ghost"
                        size="icon"
                        aria-label="Share"
                        onClick={async () => {
                            if (typeof navigator.share === "function") {
                                try {
                                    await navigator.share({ text: messageText });
                                } catch {
                                    // Cancelled or unsupported — nothing to recover.
                                }
                                return;
                            }
                            // Same await-not-.then rationale as the Copy button above.
                            if (!navigator.clipboard) return;
                            try {
                                await navigator.clipboard.writeText(messageText);
                            } catch {
                                // Clipboard write failed — no success state to reset.
                            }
                        }}
                        className="chat-action-btn h-6 w-6 text-[var(--midnight-muted)] hover:text-[var(--midnight)] hover:bg-[var(--cream-deep)]"
                    >
                        <Share2 className="h-3 w-3" />
                    </Button>
                </div>
            )}
        </div>
    );
}

/**
 * Builds one ToolCall from a tool_use block + its paired tool_result (if
 * any). Extracted after Finding 3/12 of the 2026-08-17 craft-pass review: two
 * independent copies of this exact 9-field mapping (here and in
 * `toolCallsOfPureToolMessage`) had already drifted once — one copy carried
 * `startedAt`/`endedAt`, the other silently didn't. One helper, one place to
 * add a field.
 */
function toolCallFromPair(block: ContentBlock, pairs: ToolBlockPair[]): ToolCall | null {
  const id = resolveToolUseId(block);
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
  return {
    id,
    name: block.name ?? "tool",
    args: resolveToolArgs(block),
    result: resultBlock?.content ?? resultBlock?.result,
    status,
    // Non-tool activity pills (plan/patch/item) reuse the tool_use block
    // shape via a synthetic id — see ToolStreamEntry.kind. They carry their
    // own human label already and must not be re-labeled by ToolActivityTrace's
    // tool-name resolver.
    kind: block.kind === "activity" ? "activity" : "tool",
    progressText: block.progressText,
    startedAt: block.startedAt,
    endedAt: block.endedAt,
  };
}

/**
 * Walks a HistoryMessage's content blocks and renders them in order:
 * text → MarkdownContent, thinking → ThinkingBlock, tool_use →
 * ToolUseIndicator paired with its matching tool_result. tool_result
 * blocks are skipped — they're rendered implicitly as part of the
 * matching tool_use pair.
 */
function MessageBlocks({ message }: { message: HistoryMessage }) {
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
      <ToolActivityTrace key={`trace:${calls[0]!.id}`} calls={calls} />,
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
        const call = toolCallFromPair(b, pairs);
        if (call) {
          pendingCalls.push(call);
          renderedToolIds.delete(id);
        }
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
            />
          );
        } else {
          rendered.push(
            <MarkdownContent key={`text:${i}`} content={text} />
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
      <div className="flex flex-col items-end min-w-0 max-w-[85%]">
        <div className="relative text-sm leading-relaxed bg-[var(--cream-deep)] text-[var(--midnight)] rounded-lg rounded-br-sm px-4 py-2.5">
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
  function ItemRenderer({ item }: { item: ChatRenderItem }) {
    if (item.kind === "divider") {
      return (
        <div className="my-3 flex items-center gap-2 text-caption text-[var(--midnight-muted)]">
          <span className="flex-1 h-px bg-[var(--edge)]" />
          <span className="uppercase tracking-caps">{item.label}</span>
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
          streaming={item.kind === "stream"}
        />
      );
    }
    if (item.kind === "live-thinking") {
      return <ThinkingBlock content={item.text} streaming />;
    }
    if (item.kind === "message") {
      return <MessageBlocks message={item.message} />;
    }
    return null;
  },
  (prev, next) => itemEquals(prev.item, next.item),
);

/**
 * A `/btw` (detached side-channel) answer rendered as a distinct, non-thread
 * side card — labeled "BTW · not saved" (it is never written to the transcript
 * / history), the question in muted text, the answer as the body. Error styling
 * reuses the existing chat error tokens (var(--err) text/border, a low-alpha danger tint fill). Mirrors
 * OpenClaw's "Not saved to chat history" side card.
 */
function BtwSideCard({
  question,
  text,
  isError,
}: {
  question: string;
  text: string;
  isError?: boolean;
}) {
  return (
    <div
      data-testid="btw-card"
      className={cn(
        "rounded-lg border px-3 py-2.5 text-sm",
        isError ? "border-[var(--err)] bg-[color-mix(in_srgb,var(--err)_6%,var(--cream))]" : "border-[var(--edge)] bg-[var(--cream-deep)]",
      )}
    >
      <div className="flex items-center gap-1.5 mb-1.5">
        <span
          className={cn(
            "label-caps",
            isError ? "text-[var(--err)]" : "text-[var(--midnight)]",
          )}
        >
          BTW
        </span>
        <span className="text-caption text-[var(--midnight-muted)]">· not saved</span>
      </div>
      {question && <div className="text-xs text-[var(--midnight-muted)] mb-1.5">{question}</div>}
      <div className={cn("leading-relaxed", isError ? "text-[var(--err)]" : "text-[var(--midnight-soft)]")}>
        <MarkdownContent content={text} />
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
    const call = toolCallFromPair(b, pairs);
    if (!call) return null;
    calls.push(call);
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
}: {
  group: RenderGroup;
  agentName?: string;
  isStreaming?: boolean;
  isLastAssistantGroup?: boolean;
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
  // Error cards are full-width cards; plain bubbles keep hugging.
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
        <div className="relative text-sm leading-relaxed text-[var(--midnight-soft)] w-full">
          {renderables.map((entry) =>
            entry.kind === "tool-run" ? (
              <ToolActivityTrace key={entry.key} calls={entry.calls} />
            ) : (
            <ItemRenderer key={entry.item.key} item={entry.item} />
            ),
          )}
        </div>
      </div>
    </div>
  );
}
export interface MessageListHandle {
  scrollToBottom: () => void;
}

export const MessageList = React.forwardRef<MessageListHandle, MessageListProps>(
  function MessageList(
    { renderGroups, isStreaming, agentName },
    ref
  ) {
    const { containerRef, scrollToBottom, isAutoScrollingRef, markAutoScroll } = useScrollToBottom();

    // Track whether the user is at (or near) the bottom of the scroll
    // container. While true, incoming streamed chunks pull the view down.
    // If the user scrolls up to re-read an earlier message, we stop
    // pulling — they'll scroll back down themselves. Threshold matches
    // Tailwind's `space-y-10` (~40px) plus a comfortable margin.
    const isNearBottomRef = React.useRef(true);
    // "Jump to latest" visibility. Deliberately ONE boolean rather than two
    // derived conditions, because the two halves are set from different
    // places and neither is sufficient alone: it goes true only when content
    // arrives WHILE the reader is scrolled away (the streaming effect below),
    // and false the moment they are back near the bottom (handleScroll).
    //
    // Gating on "newer content exists" alone is the known failure mode —
    // upstream shipped that and the button flashed on every reply, because
    // from the first streaming tick of a turn there is always content at the
    // tail. Their fix was to AND it with live-edge geometry; this is the same
    // predicate, collapsed into the flag's own transitions.
    //
    // State, not a ref, because unlike isNearBottomRef this one has to paint.
    const [showJumpToLatest, setShowJumpToLatest] = React.useState(false);
    const hasMountedRef = React.useRef(false);
    const prevTotalItemsRef = React.useRef(0);
    const prevLastTailLengthRef = React.useRef(0);
    const prevLastSideRef = React.useRef<RenderSide | undefined>(undefined);

    React.useImperativeHandle(ref, () => ({
      scrollToBottom,
    }));

    // Re-pin autoscroll by hand rather than waiting for the scroll event the
    // write below queues: that event is asynchronous, so the next streaming
    // commit could land first, read a stale isNearBottomRef and re-arm the
    // button on the very chunk the user jumped to see.
    const handleJumpToLatest = React.useCallback(() => {
      isNearBottomRef.current = true;
      setShowJumpToLatest(false);
      scrollToBottom();
    }, [scrollToBottom]);

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
      // Back at the live edge — whatever arrived while they were away is now
      // on screen, so the affordance has nothing left to offer.
      if (isNearBottomRef.current) setShowJumpToLatest(false);
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
      // whole workspace shell out of the viewport). markAutoScroll flags the
      // write so TranscriptSelectionBar's scroll-dismissal (which shares
      // this same container) can tell it apart from a real user scroll.
      const snapToBottom = () => {
        markAutoScroll();
        container.scrollTop = container.scrollHeight;
      };

      if (isFirstPaintWithItems || newUserMessage) {
        snapToBottom();
        isNearBottomRef.current = true;
        setShowJumpToLatest(false);
        hasMountedRef.current = true;
      } else if (itemsIncreased || tailGrew) {
        if (isNearBottomRef.current) {
          snapToBottom();
        } else {
          // Content arrived below a reader who has scrolled away. This is the
          // ONLY place the affordance is armed, which is what keeps it off
          // screen for someone sitting at the live edge.
          setShowJumpToLatest(true);
        }
      }

      if (totalItems > 0) hasMountedRef.current = true;
      prevTotalItemsRef.current = totalItems;
      prevLastTailLengthRef.current = lastTailLength;
      prevLastSideRef.current = lastSide;
    }, [totalItems, lastTailLength, lastSide, containerRef, markAutoScroll]);

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

    return (
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 min-h-0 overflow-y-auto p-4 md:px-8"
        data-lenis-prevent
      >
        {/* Selecting text anywhere inside this container surfaces a floating
            Copy action — portaled to document.body, so it doesn't render as
            a DOM descendant here despite mounting from this ref. See
            TranscriptSelectionBar.tsx. */}
        <TranscriptSelectionBar containerRef={containerRef} isAutoScrollingRef={isAutoScrollingRef} />
        {/* `chat-transcript` occludes off-screen groups (content-visibility) —
            see globals.css. Keeps a long history off the layout hot path. */}
        <div className="max-w-3xl mx-auto space-y-10 py-8 chat-transcript">
          {renderGroups.map((group, idx) => (
            <BubbleGroup
              key={group.key}
              group={group}
              agentName={agentName}
              isStreaming={isStreaming}
              isLastAssistantGroup={idx === lastAssistantGroupIndex}
            />
          ))}
        </div>
        {/* Sticky, not absolute: `absolute` inside a scroll container anchors
            to the scrolled content, which would park this at the end of the
            transcript instead of over the viewport. Sticky pins it to the
            scrollport. It sits OUTSIDE .chat-transcript because that class
            gives every direct child `content-visibility: auto` and a 200px
            intrinsic-size placeholder. `h-0` keeps it out of layout so
            appearing and disappearing never reflows the transcript. */}
        {showJumpToLatest && (
          <div className="sticky bottom-4 z-10 flex h-0 justify-center">
            <button
              type="button"
              onClick={handleJumpToLatest}
              className="-translate-y-full text-caption text-[var(--midnight-soft)] hover:text-[var(--midnight)] px-3 py-1 rounded-full border border-[var(--edge)] bg-[var(--cream-deep)] shadow-sm"
            >
              Jump to latest
            </button>
          </div>
        )}
      </div>
    );
  }
);
