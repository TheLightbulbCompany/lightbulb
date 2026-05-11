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
  messages: Message[];
  isTyping?: boolean;
  agentName?: string;
  onRetry?: (assistantMsgId: string) => void;
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

function renderToolResult(blocks: ToolResultBlock[] | undefined): string | null {
  if (!blocks?.length) return null;
  const parts: string[] = [];
  for (const b of blocks) {
    if (b.type === "text" && b.text) parts.push(b.text);
    else if (b.omitted) parts.push(`[${b.type} — ${b.bytes ?? "?"} bytes, omitted]`);
  }
  return parts.join("\n\n") || null;
}

function ToolPill({ t }: { t: ToolUse }) {
  const [open, setOpen] = React.useState(false);
  const s = TOOL_STYLES[t.status];
  const hasDetails = !!(t.args || t.result || t.meta);

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
        <span>{t.tool}</span>
        {t.status === "error" && <span>failed</span>}
        {hasDetails &&
          (open ? (
            <ChevronDown className="h-3 w-3 opacity-70" />
          ) : (
            <ChevronRight className="h-3 w-3 opacity-70" />
          ))}
      </button>
      {open && hasDetails && (
        <div className="mt-1.5 max-w-xl rounded-md border border-[#e0dbd0] bg-[#faf7f2] p-2 text-xs space-y-2">
          {t.meta && (
            <div className="text-[#8a8578]">
              <span className="font-medium text-[#302d28]">target:</span> {t.meta}
            </div>
          )}
          {t.args && Object.keys(t.args).length > 0 && (
            <div>
              <div className="font-medium text-[#302d28] mb-0.5">input</div>
              <pre className="whitespace-pre-wrap break-words text-[#302d28] bg-[#f3efe6] rounded px-2 py-1 max-h-48 overflow-auto">
                {JSON.stringify(t.args, null, 2)}
              </pre>
            </div>
          )}
          {renderToolResult(t.result) && (
            <div>
              <div className="font-medium text-[#302d28] mb-0.5">
                {t.status === "error" ? "error" : "output"}
              </div>
              <pre className="whitespace-pre-wrap break-words text-[#302d28] bg-[#f3efe6] rounded px-2 py-1 max-h-48 overflow-auto">
                {renderToolResult(t.result)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ToolUseIndicator({ toolUses }: { toolUses: ToolUse[] }) {
  if (toolUses.length === 0) return null;
  return (
    <div className="mb-3 flex flex-wrap gap-2 items-start">
      {toolUses.map((t, i) => (
        <ToolPill key={t.toolCallId ?? `${t.tool}-${i}`} t={t} />
      ))}
    </div>
  );
}

export const AGENT_GLYPH_PATH =
    "M11.2 6 C10.4 4.2 8.8 2.5 7 2.5 C5.2 2.5 4 4 4 6 C4 8 5.2 9.5 7 9.5 C8.8 9.5 10.4 7.8 11.2 6 C12 4.2 13.6 2.5 15.4 2.5 C17.2 2.5 18.4 4 18.4 6 C18.4 8 17.2 9.5 15.4 9.5 C13.6 9.5 12 7.8 11.2 6Z";

export function AgentGlyph({
    state,
    size = 18,
    className,
}: {
    state: "idle" | "thinking";
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

function AgentHead({ name, state }: { name: string; state: "idle" | "thinking" }) {
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

function ErrorToolbar({ messageId, onRetry }: { messageId: string; onRetry?: (id: string) => void }) {
    return (
        <div className="flex items-center gap-2 mb-2">
            <span className="text-xs font-medium text-[#dc2626]">Failed to generate</span>
            {onRetry && (
                <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 text-[#dc2626] hover:text-[#1a1a1a] hover:bg-[#f3efe6]"
                    onClick={() => onRetry(messageId)}
                >
                    <RefreshCw className="h-3 w-3" />
                </Button>
            )}
        </div>
    );
}

export interface MessageListHandle {
  scrollToBottom: () => void;
}

export const MessageList = React.forwardRef<MessageListHandle, MessageListProps>(
  function MessageList({ messages, isTyping, agentName, onRetry, onOpenFile }, ref) {
    const { containerRef, endRef, scrollToBottom } = useScrollToBottom();

    // Track whether the user is at (or near) the bottom of the scroll
    // container. While true, incoming streamed chunks pull the view down.
    // If the user scrolls up to re-read an earlier message, we stop
    // pulling — they'll scroll back down themselves. Threshold matches
    // Tailwind's `space-y-10` (~40px) plus a comfortable margin.
    const isNearBottomRef = React.useRef(true);
    const hasMountedRef = React.useRef(false);
    const prevMessagesLengthRef = React.useRef(0);
    const prevLastContentLengthRef = React.useRef(0);
    const prevLastRoleRef = React.useRef<"user" | "assistant" | undefined>(undefined);

    React.useImperativeHandle(ref, () => ({
      scrollToBottom,
    }));

    const handleScroll = React.useCallback(() => {
      const c = containerRef.current;
      if (!c) return;
      const distance = c.scrollHeight - c.scrollTop - c.clientHeight;
      isNearBottomRef.current = distance < 120;
    }, [containerRef]);

    // Auto-scroll rules:
    //   - First render (agent switch / history load) → snap to bottom.
    //   - Length increased → a new message arrived; snap if near bottom,
    //     or unconditionally if the new message is from the user (they
    //     just sent it, they want to see it).
    //   - Length same, last message's content grew → streaming chunk on
    //     the tail bubble; snap only if near bottom (so a user who
    //     scrolled up isn't yanked back while reading).
    React.useEffect(() => {
      const container = containerRef.current;
      const end = endRef.current;
      if (!container || !end) return;

      const lastMsg = messages[messages.length - 1];
      const lastContentLength = lastMsg?.content?.length ?? 0;
      const lastRole = lastMsg?.role;

      const lengthIncreased = messages.length > prevMessagesLengthRef.current;
      const contentGrew =
        messages.length === prevMessagesLengthRef.current &&
        lastContentLength > prevLastContentLengthRef.current;
      // Any new message whose tail is a user message is a fresh user send
      // (multi-bubble `sendMessage` appends only the user row, and a
      // cancel-before-first-event turn can leave the previous tail as
      // user too — so don't gate on role *changing*, just on "new tail
      // is user"). Always snap so the user's own message is visible.
      const newUserMessage = lengthIncreased && lastRole === "user";

      const isFirstPaintWithMessages = !hasMountedRef.current && messages.length > 0;

      // JSDOM test envs don't always polyfill scrollIntoView; production
      // browsers always have it. Optional-chain so tests that don't mock
      // it don't crash when they transitively render MessageList.
      if (isFirstPaintWithMessages || newUserMessage) {
        end.scrollIntoView?.({ behavior: "auto", block: "end" });
        isNearBottomRef.current = true;
        hasMountedRef.current = true;
      } else if ((lengthIncreased || contentGrew) && isNearBottomRef.current) {
        end.scrollIntoView?.({ behavior: "auto", block: "end" });
      }

      if (messages.length > 0) hasMountedRef.current = true;
      prevMessagesLengthRef.current = messages.length;
      prevLastContentLengthRef.current = lastContentLength;
      prevLastRoleRef.current = lastRole;
    }, [messages, containerRef, endRef]);

    // The "typing placeholder" shows the agent header (animated thinking
    // glyph) during the window between `sendMessage` and the first
    // streamed chunk. Since multi-bubble rendering creates assistant
    // bubbles lazily on first event (spec §4), nothing else is on screen
    // to signal "the agent received your message and is working." This
    // placeholder is rendered only when streaming is active AND the last
    // message is a user message (or there are no messages yet).
    const showTypingPlaceholder =
      !!isTyping &&
      (messages.length === 0 ||
        messages[messages.length - 1].role === "user");

    return (
      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 min-h-0 overflow-y-auto p-4 md:px-8"
        data-lenis-prevent
      >
        <div className="max-w-3xl mx-auto space-y-10 py-8">
          {messages.map((msg, idx) => {
            const isLastAssistant = msg.role === "assistant" && (
              idx === messages.length - 1 ||
              messages.slice(idx + 1).every((m) => m.role !== "assistant")
            );
            const isErrorBubble = msg.role === "assistant" && msg.content.startsWith("Error: ");
            const errorBody = isErrorBubble ? msg.content.slice(7) : "";
            // Typed cards take over the full bubble when we have a code —
            // the upstream `errorKind` enum (rate_limit, context_length,
            // refusal, timeout, unknown) lets us render tailored copy +
            // an action button. Without a code we fall back to the legacy
            // inline red text + ErrorToolbar so we don't regress untyped
            // failure paths (transport errors, sendMessage catch branch).
            const useTypedCard = isErrorBubble && (msg.errorCode || errorBody);

            return (
            <div
              key={msg.id}
              data-role={msg.role}
              className={cn(
                "flex w-full group relative",
                msg.role === "user" ? "justify-end" : "justify-start"
              )}
            >
              <div className="flex flex-col min-w-0 max-w-[85%]">
                {msg.role === "assistant" && (
                  isErrorBubble
                    ? <ErrorToolbar messageId={msg.id} onRetry={onRetry} />
                    : <AgentHead
                        name={agentName || "Assistant"}
                        state={isTyping && isLastAssistant ? "thinking" : "idle"}
                      />
                )}

                <div
                  className={cn(
                    "relative text-sm leading-7",
                    msg.role === "user"
                      ? "bg-[#f0ebe2] text-[#1a1a1a] rounded-2xl rounded-br-md px-4 py-2.5"
                      : "text-[#302d28] w-full"
                  )}
                >
                {msg.role === "assistant" && msg.thinking && (
                   <ThinkingBlock content={msg.thinking} />
                )}

                {msg.role === "assistant" && msg.toolUses && msg.toolUses.length > 0 && (
                  <ToolUseIndicator toolUses={msg.toolUses} />
                )}

                {useTypedCard ? (
                  <ChatErrorCard
                    message={errorBody}
                    code={msg.errorCode}
                    onRetry={onRetry ? () => onRetry(msg.id) : undefined}
                  />
                ) : (
                  <div className={cn(
                    "wrap-break-word",
                    msg.role === "user" && "whitespace-pre-wrap",
                    isErrorBubble && "text-[#dc2626]"
                  )}>
                    {isErrorBubble
                      ? errorBody
                      : msg.role === "assistant" && msg.content
                        ? <MarkdownContent content={msg.content} onOpenFile={onOpenFile} />
                        : msg.content || null}
                  </div>
                )}
                </div>
              </div>
            </div>
          );
          })}
          {showTypingPlaceholder && (
            <div
              data-testid="typing-placeholder"
              className="flex w-full justify-start"
            >
              <div className="flex flex-col min-w-0 max-w-[85%]">
                <AgentHead name={agentName || "Assistant"} state="thinking" />
              </div>
            </div>
          )}
          <div ref={endRef} />
        </div>
      </div>
    );
  }
);
