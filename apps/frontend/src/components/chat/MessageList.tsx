import * as React from "react";
import { cn } from "@/lib/utils";
import { linkifyFilePaths, isWorkspaceFileLink, extractFilePath } from "@/lib/filePathDetection";
import { Copy, RefreshCw, Share2, ChevronDown, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useScrollToBottom } from "@/hooks/useScrollToBottom";
import { ChatErrorCard } from "@/components/chat/ChatErrorCard";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import {
  pairToolBlocks,
  isToolUseBlock,
  isToolResultBlock,
  isThinkingBlock,
  isTextBlock,
  resolveToolUseId,
  resolveToolArgs,
} from "@/hooks/internal/toolBlockExtract";
import type {
  ChatRenderItem,
  RenderGroup,
  HistoryMessage,
} from "@/hooks/internal/types";
// Approval rendering moved to ExecApprovalDialog (mounted at chat-shell level
// via useExecApprovals). Tool pills now only represent execution state —
// running / done / error. See ExecApprovalDialog.tsx for the modal that
// surfaces pending exec approvals from the global queue.

export interface ToolResultBlock {
  type: string;
  text?: string;
  bytes?: number;
  omitted?: boolean;
}

export interface ToolUse {
  tool: string;
  toolCallId?: string;
  status: "running" | "done" | "error";
  args?: Record<string, unknown>;
  result?: ToolResultBlock[];
  meta?: string;
}

/**
 * Legacy flat-message shape. As of Task 8 (plan 2026-05-13), no `src/`
 * consumer references it any more — RunTranscript and StreamingStatusBar
 * have both been migrated. The only remaining importer is
 * `tests/unit/components/chat/StreamingStatusBar.test.tsx`, which tests
 * the pre-migration `messages` prop shape. Once that test is rewritten
 * or removed (see Task 9 / 10 of the flat-chat-timeline plan), these
 * transitional types (Message + ToolUse + ToolResultBlock) can be
 * deleted outright.
 * TODO(flat-chat-timeline): remove after StreamingStatusBar test is updated.
 */
export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  thinking?: string;
  model?: string;
  toolUses?: ToolUse[];
  /**
   * Set when the bubble represents a terminal agent error. Carries
   * OpenClaw's `errorKind` (forwarded by the backend as `code`) so we
   * can render a typed ChatErrorCard with tailored copy / actions.
   * `content` will still begin with the legacy `Error: ` prefix for
   * back-compat with consumers that match on string.
   */
  errorCode?: string;
}

export interface MessageListProps {
  renderGroups: RenderGroup[];
  agentName?: string;
  isStreaming?: boolean;
  isTyping?: boolean;
  onOpenFile?: (path: string) => void;
}

function CodeBlock({ className, children, ...props }: React.HTMLAttributes<HTMLElement> & { children?: React.ReactNode }) {
  const match = /language-(\w+)/.exec(className || "");
  const codeString = String(children).replace(/\n$/, "");

  if (!match) {
    // Inline code
    return (
      <code className="bg-[#e8e3d9] rounded px-1.5 py-0.5 text-sm text-[#1a1a1a]" {...props}>
        {children}
      </code>
    );
  }

  // Fenced code block
  return (
    <div className="relative group/code my-4 rounded-lg overflow-hidden border border-[#e0dbd0]">
      <div className="flex items-center justify-between px-4 py-2 bg-[#f3efe6] border-b border-[#e0dbd0]">
        <span className="text-xs text-[#8a8578]">{match[1]}</span>
        <button
          onClick={() => { navigator.clipboard.writeText(codeString).catch(() => {}); }}
          className="text-xs text-[#8a8578] hover:text-[#1a1a1a] transition-colors flex items-center gap-1"
        >
          <Copy className="h-3 w-3" />
          Copy
        </button>
      </div>
      <SyntaxHighlighter
        style={oneDark}
        language={match[1]}
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
  const processedContent = React.useMemo(() => linkifyFilePaths(content), [content]);
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
                className="text-[#06402B] hover:underline cursor-pointer bg-transparent border-none p-0 font-inherit text-inherit inline"
              >
                {children}
              </button>
            );
          }
          const isSafe = !href?.match(/^(javascript|data|vbscript):/i);
          return (
            <a href={isSafe ? href : '#'} target="_blank" rel="noopener noreferrer" className="text-[#06402B] hover:underline">
              {children}
            </a>
          );
        },
        ul: ({ children }) => <ul className="list-disc list-inside mb-3 space-y-1">{children}</ul>,
        ol: ({ children }) => <ol className="list-decimal list-inside mb-3 space-y-1">{children}</ol>,
        li: ({ children }) => <li className="ml-2">{children}</li>,
        blockquote: ({ children }) => (
          <blockquote className="border-l-2 border-[#e0dbd0] pl-4 text-[#8a8578] my-3">{children}</blockquote>
        ),
        table: ({ children }) => (
          <div className="overflow-x-auto my-4">
            <table className="w-full border-collapse border border-[#e0dbd0] text-sm">{children}</table>
          </div>
        ),
        thead: ({ children }) => <thead className="bg-[#f3efe6]">{children}</thead>,
        th: ({ children }) => <th className="border border-[#e0dbd0] px-3 py-2 text-left font-medium">{children}</th>,
        td: ({ children }) => <td className="border border-[#e0dbd0] px-3 py-2">{children}</td>,
        tr: ({ children }) => <tr className="even:bg-[#f3efe6]">{children}</tr>,
        hr: () => <hr className="border-[#e0dbd0] my-4" />,
        pre: ({ children }) => <>{children}</>,
      }}
    >
      {processedContent}
    </ReactMarkdown>
  );
});

function ThinkingBlock({ content }: { content: string }) {
  const [isExpanded, setIsExpanded] = React.useState(false);

  return (
    <div className="mb-4 border border-[#e0dbd0] rounded-lg overflow-hidden">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center gap-2 px-3 py-2 bg-[#f3efe6] hover:bg-[#ece7dc] transition-colors text-left"
      >
        {isExpanded ? (
          <ChevronDown className="h-4 w-4 text-[#8a8578]" />
        ) : (
          <ChevronRight className="h-4 w-4 text-[#8a8578]" />
        )}
        <span className="text-sm text-[#8a8578] italic">Thinking...</span>
      </button>
      {isExpanded && (
        <div className="px-3 py-2 text-sm text-[#8a8578] border-t border-[#e0dbd0]">
          <MarkdownContent content={content} />
        </div>
      )}
    </div>
  );
}

const TOOL_STYLES = {
  running: {
    pill: "bg-[#e8f5e9] text-[#2d8a4e] border-[#c8e6c9]",
    dot: "bg-[#2d8a4e] animate-pulse",
  },
  done: {
    pill: "bg-[#f3efe6] text-[#8a8578] border-[#e0dbd0]",
    dot: "bg-[#cdc7ba]",
  },
  error: {
    pill: "bg-[#fce4ec] text-[#a5311f] border-[#f8bbd0]",
    dot: "bg-[#c62828]",
  },
} as const;

/**
 * Render a tool_result block's content for display in the pill expansion.
 * `tool_result.content` may be a string OR an array of inner blocks
 * (Anthropic shape). We accept both. `result` is the legacy field name
 * pairToolBlocks falls back to.
 */
function renderToolOutput(content: unknown): string | null {
  if (content == null) return null;
  if (typeof content === "string") return content || null;
  if (!Array.isArray(content)) {
    try {
      return JSON.stringify(content, null, 2);
    } catch {
      return null;
    }
  }
  const parts: string[] = [];
  for (const b of content) {
    if (!b || typeof b !== "object") continue;
    const obj = b as { type?: string; text?: string; omitted?: boolean; bytes?: number };
    if (obj.type === "text" && typeof obj.text === "string") parts.push(obj.text);
    else if (obj.omitted) parts.push(`[${obj.type ?? "blob"} — ${obj.bytes ?? "?"} bytes, omitted]`);
  }
  return parts.join("\n\n") || null;
}

interface ToolPillState {
  id: string;
  name: string;
  args: unknown;
  result: unknown;
  status: "running" | "done" | "error";
}

function ToolPill({ tool }: { tool: ToolPillState }) {
  const [open, setOpen] = React.useState(false);
  const s = TOOL_STYLES[tool.status];
  const output = renderToolOutput(tool.result);
  const hasArgs = !!(
    tool.args &&
    typeof tool.args === "object" &&
    Object.keys(tool.args as Record<string, unknown>).length > 0
  );
  const hasDetails = hasArgs || !!output;

  return (
    <div className="inline-block">
      <button
        type="button"
        onClick={() => hasDetails && setOpen((v) => !v)}
        disabled={!hasDetails}
        className={cn(
          "inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium border transition-colors",
          s.pill,
          hasDetails ? "cursor-pointer hover:brightness-95" : "cursor-default",
        )}
        aria-expanded={open}
      >
        <span className={cn("w-1.5 h-1.5 rounded-full", s.dot)} />
        <span>{tool.name}</span>
        {tool.status === "error" && <span>failed</span>}
        {hasDetails &&
          (open ? (
            <ChevronDown className="h-3 w-3 opacity-70" />
          ) : (
            <ChevronRight className="h-3 w-3 opacity-70" />
          ))}
      </button>
      {open && hasDetails && (
        <div className="mt-1.5 max-w-xl rounded-md border border-[#e0dbd0] bg-[#faf7f2] p-2 text-xs space-y-2">
          {hasArgs && (
            <div>
              <div className="font-medium text-[#302d28] mb-0.5">input</div>
              <pre className="whitespace-pre-wrap break-words text-[#302d28] bg-[#f3efe6] rounded px-2 py-1 max-h-48 overflow-auto">
                {JSON.stringify(tool.args, null, 2)}
              </pre>
            </div>
          )}
          {output && (
            <div>
              <div className="font-medium text-[#302d28] mb-0.5">
                {tool.status === "error" ? "error" : "output"}
              </div>
              <pre className="whitespace-pre-wrap break-words text-[#302d28] bg-[#f3efe6] rounded px-2 py-1 max-h-48 overflow-auto">
                {output}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Single-tool indicator. Plan §6 changes the prop from `toolUses: ToolUse[]`
 * to a single tool object — pairing now happens upstream in MessageBlocks
 * (one indicator per tool_use block), so this component renders one pill
 * per call.
 */
function ToolUseIndicator({ tool }: { tool: ToolPillState }) {
  return (
    <div className="mb-3 flex flex-wrap gap-2 items-start">
      <ToolPill tool={tool} />
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

function AgentHead({ name, state = "idle" }: { name: string; state?: "idle" | "thinking" }) {
    return (
        <div className="flex items-center gap-2 mb-2">
            <span className="text-xs font-semibold text-[#302d28] tracking-tight">
                {name}
            </span>
            <span className="inline-flex items-center justify-center w-5 h-5">
                <AgentGlyph state={state} size={18} />
            </span>
            <Button variant="ghost" size="icon" className="h-6 w-6 text-[#8a8578] hover:text-[#1a1a1a] hover:bg-[#f3efe6] opacity-0 group-hover:opacity-100 transition-opacity ml-1">
                <Copy className="h-3 w-3" />
            </Button>
            <Button variant="ghost" size="icon" className="h-6 w-6 text-[#8a8578] hover:text-[#1a1a1a] hover:bg-[#f3efe6] opacity-0 group-hover:opacity-100 transition-opacity">
                <RefreshCw className="h-3 w-3" />
            </Button>
            <Button variant="ghost" size="icon" className="h-6 w-6 text-[#8a8578] hover:text-[#1a1a1a] hover:bg-[#f3efe6] opacity-0 group-hover:opacity-100 transition-opacity">
                <Share2 className="h-3 w-3" />
            </Button>
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
}: {
  message: HistoryMessage;
  onOpenFile?: (path: string) => void;
}) {
  const blocks = Array.isArray(message.content) ? message.content : [];
  const pairs = pairToolBlocks(blocks);
  const renderedToolIds = new Set(pairs.map((p) => p.toolUseId));
  const rendered: React.ReactNode[] = [];
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
        rendered.push(
          <ToolUseIndicator
            key={`tool:${id}`}
            tool={{
              id,
              name: b.name ?? "tool",
              args: resolveToolArgs(b),
              result: resultBlock?.content ?? resultBlock?.result,
              status,
            }}
          />
        );
        renderedToolIds.delete(id);
      }
      continue;
    }
    if (isThinkingBlock(b)) {
      const text = (b.thinking ?? b.text ?? "") as string;
      if (text.length > 0) {
        rendered.push(<ThinkingBlock key={`think:${i}`} content={text} />);
      }
      continue;
    }
    if (isTextBlock(b)) {
      const text = (b.text ?? "") as string;
      if (text.length > 0) {
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
  return <>{rendered}</>;
}

function UserMessageRow({ message }: { message: HistoryMessage }) {
  const text = Array.isArray(message.content)
    ? message.content
        .filter(isTextBlock)
        .map((b) => (b.text ?? "") as string)
        .join("")
    : (message.text ?? "");
  return (
    <div className="flex w-full justify-end" data-role="user">
      <div className="flex flex-col min-w-0 max-w-[85%]">
        <div className="relative text-sm leading-7 bg-[#f0ebe2] text-[#1a1a1a] rounded-2xl rounded-br-md px-4 py-2.5">
          <div className="wrap-break-word whitespace-pre-wrap">{text}</div>
        </div>
      </div>
    </div>
  );
}

/**
 * Renders one item from a RenderGroup. Stream / live-thinking / divider
 * items map directly to existing block primitives; {kind:"message"} items
 * defer to MessageBlocks which walks the message's content[] array.
 */
function ItemRenderer({
  item,
  onOpenFile,
}: {
  item: ChatRenderItem;
  onOpenFile?: (path: string) => void;
}) {
  if (item.kind === "divider") {
    return (
      <div className="my-3 flex items-center gap-2 text-[11px] text-[#8a8578]">
        <span className="flex-1 h-px bg-[#e0dbd0]" />
        <span className="uppercase tracking-wide">{item.label}</span>
        <span className="flex-1 h-px bg-[#e0dbd0]" />
      </div>
    );
  }
  if (item.kind === "stream" || item.kind === "stream-segment") {
    return <MarkdownContent content={item.text} onOpenFile={onOpenFile} />;
  }
  if (item.kind === "live-thinking") {
    return <ThinkingBlock content={item.text} />;
  }
  if (item.kind === "message") {
    return <MessageBlocks message={item.message} onOpenFile={onOpenFile} />;
  }
  return null;
}

/**
 * One visual bubble group. User-side groups expand to one bubble per item
 * (each user message stands alone). Assistant-side groups render a single
 * rounded container with all items flowing inside it.
 */
function BubbleGroup({
  group,
  agentName,
  isStreaming,
  isLastAssistantGroup,
  onOpenFile,
}: {
  group: RenderGroup;
  agentName?: string;
  isStreaming?: boolean;
  isLastAssistantGroup?: boolean;
  onOpenFile?: (path: string) => void;
}) {
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
  return (
    <div className="flex w-full justify-start" data-role="assistant">
      <div className="flex flex-col min-w-0 max-w-[85%] group relative">
        <AgentHead
          name={agentName || "Assistant"}
          state={isStreaming && isLastAssistantGroup ? "thinking" : "idle"}
        />
        <div className="relative text-sm leading-7 text-[#302d28] w-full">
          {group.items.map((item) => (
            <ItemRenderer key={item.key} item={item} onOpenFile={onOpenFile} />
          ))}
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
        <AgentHead name={agentName || "Assistant"} state="thinking" />
      </div>
    </div>
  );
}

export interface MessageListHandle {
  scrollToBottom: () => void;
}

export const MessageList = React.forwardRef<MessageListHandle, MessageListProps>(
  function MessageList({ renderGroups, isStreaming, isTyping, agentName, onOpenFile }, ref) {
    const { containerRef, endRef, scrollToBottom } = useScrollToBottom();

    // Track whether the user is at (or near) the bottom of the scroll
    // container. While true, incoming streamed chunks pull the view down.
    // If the user scrolls up to re-read an earlier message, we stop
    // pulling — they'll scroll back down themselves. Threshold matches
    // Tailwind's `space-y-10` (~40px) plus a comfortable margin.
    const isNearBottomRef = React.useRef(true);
    const hasMountedRef = React.useRef(false);
    const prevTotalItemsRef = React.useRef(0);
    const prevLastTailLengthRef = React.useRef(0);
    const prevLastSideRef = React.useRef<"user" | "assistant" | undefined>(undefined);

    React.useImperativeHandle(ref, () => ({
      scrollToBottom,
    }));

    const handleScroll = React.useCallback(() => {
      const c = containerRef.current;
      if (!c) return;
      const distance = c.scrollHeight - c.scrollTop - c.clientHeight;
      isNearBottomRef.current = distance < 120;
    }, [containerRef]);

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
      const end = endRef.current;
      if (!container || !end) return;

      const itemsIncreased = totalItems > prevTotalItemsRef.current;
      const tailGrew =
        totalItems === prevTotalItemsRef.current &&
        lastTailLength > prevLastTailLengthRef.current;
      const newUserMessage = itemsIncreased && lastSide === "user";
      const isFirstPaintWithItems = !hasMountedRef.current && totalItems > 0;

      // JSDOM test envs don't always polyfill scrollIntoView; production
      // browsers always have it. Optional-chain so tests that don't mock
      // it don't crash when they transitively render MessageList.
      if (isFirstPaintWithItems || newUserMessage) {
        end.scrollIntoView?.({ behavior: "auto", block: "end" });
        isNearBottomRef.current = true;
        hasMountedRef.current = true;
      } else if ((itemsIncreased || tailGrew) && isNearBottomRef.current) {
        end.scrollIntoView?.({ behavior: "auto", block: "end" });
      }

      if (totalItems > 0) hasMountedRef.current = true;
      prevTotalItemsRef.current = totalItems;
      prevLastTailLengthRef.current = lastTailLength;
      prevLastSideRef.current = lastSide;
    }, [totalItems, lastTailLength, lastSide, containerRef, endRef]);

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

    return (
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 min-h-0 overflow-y-auto p-4 md:px-8"
        data-lenis-prevent
      >
        <div className="max-w-3xl mx-auto space-y-10 py-8">
          {renderGroups.map((group, idx) => (
            <BubbleGroup
              key={group.key}
              group={group}
              agentName={agentName}
              isStreaming={isStreaming}
              isLastAssistantGroup={idx === lastAssistantGroupIndex}
              onOpenFile={onOpenFile}
            />
          ))}
          {showTypingPlaceholder && <TypingPlaceholder agentName={agentName} />}
          <div ref={endRef} />
        </div>
      </div>
    );
  }
);
