"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { capture } from "@/lib/analytics";
import { SendHorizontal, Paperclip, X, FileIcon, Loader2, Square, CornerDownRight, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { parseSlashInput } from "@/lib/slashCommands";
import { useSlashCommands } from "@/hooks/useSlashCommands";
import { SlashCommandMenu, flattenVisible } from "@/components/chat/SlashCommandMenu";

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB

interface PendingFile {
  file: File;
  id: string;
}

interface ChatInputProps {
  onSend: (message: string, files?: File[]) => void;
  onStop?: () => void;
  /**
   * The active agent. Scopes the slash-command menu's `commands.list` RPC so
   * the menu offers the agent's own commands (skills/plugins vary per agent).
   */
  agentId?: string | null;
  disabled?: boolean;
  centered?: boolean;
  isUploading?: boolean;
  isStreaming?: boolean;
  /**
   * Gateway socket state. Sends while disconnected still queue (by
   * design, mirroring upstream) — this only drives the "Reconnecting…"
   * hint so the user knows WHY a send is parked.
   */
  isConnected?: boolean;
  suggestedMessage?: string;
  queuedMessages?: {
    id: string;
    text: string;
    sendState?: "sending" | "waiting-reconnect" | "failed";
    sendError?: string;
  }[];
  onRemoveQueued?: (id: string) => void;
  onClearQueue?: () => void;
  onSteerQueued?: (id: string) => void;
  onRetryQueued?: (id: string) => void;
  /**
   * Composer controls (the model · effort pill) rendered in a row directly
   * above the input bar. Kept as a slot so ChatInput stays free of Clerk /
   * billing / gateway coupling — AgentChatWindow supplies the wired pill.
   */
  controls?: React.ReactNode;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

export function ChatInput({
  onSend,
  onStop,
  agentId,
  disabled,
  centered,
  isUploading,
  isStreaming,
  isConnected = true,
  suggestedMessage,
  queuedMessages = [],
  onRemoveQueued,
  onClearQueue,
  onSteerQueued,
  onRetryQueued,
  controls,
}: ChatInputProps) {
  const [input, setInput] = React.useState("");
  const [activeIndex, setActiveIndex] = React.useState(0);
  const [pendingFiles, setPendingFiles] = React.useState<PendingFile[]>([]);
  const [sizeError, setSizeError] = React.useState<{ id: number; message: string } | null>(null);
  const sizeErrorTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const sizeErrorIdRef = React.useRef(0);
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const textareaRef = React.useRef<HTMLTextAreaElement>(null);

  // Slash-command menu. The menu is "open" only when the whole input is a
  // bare command token (`/foo`) with no argument text yet — once the user
  // types an argument (`/foo bar`) the menu yields to normal typing. Keyboard
  // routing (↑↓/Tab/Enter/Esc) is handled FIRST in handleKeyDown but ONLY
  // while menuOpen, so the existing suggested-message / Enter-to-send logic
  // is untouched when the menu is closed.
  const slash = parseSlashInput(input);
  const { commands } = useSlashCommands(agentId ?? null);
  const visible = slash.isCommand ? flattenVisible(commands, slash.query) : [];
  const menuOpen = slash.isCommand && visible.length > 0 && !slash.argText;

  // Keep the highlighted row in range as the query (and therefore the visible
  // set) changes — resetting to the top is the least-surprising behavior.
  React.useEffect(() => {
    setActiveIndex(0);
  }, [slash.query]);

  // Clear timer on unmount
  React.useEffect(() => {
    return () => {
      if (sizeErrorTimerRef.current) {
        clearTimeout(sizeErrorTimerRef.current);
      }
    };
  }, []);

  // "Get started" guide hand-off: when the chat step fires its action it
  // selects the agent (an async router navigation that switches the view
  // from a control panel to chat) then asks the composer to focus so the
  // user lands ready to type. When the guide is opened from a control
  // panel, the chat surface — and this textarea — lives inside a
  // `display:none` container until that navigation commits, so a focus()
  // fired the same tick as the event no-ops (a hidden element can't take
  // focus). Focus, then verify the element actually took it; if it didn't
  // (still hidden), retry on the next animation frame a bounded number of
  // times until the chat surface is visible. Checking the outcome
  // (activeElement) rather than a layout precondition like
  // `offsetParent === null` keeps this correct under jsdom, which has no
  // layout and reports offsetParent === null even for visible nodes — so
  // the visible-render focus test still passes synchronously. Window-scoped
  // CustomEvent keeps ChatLayout decoupled from this component's ref.
  React.useEffect(() => {
    // ~1s at 60fps — bounded so a never-visible composer can't spin forever.
    const MAX_ATTEMPTS = 60;
    let raf = 0;
    const tryFocus = (attempt: number) => {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      // Focus landed (the chat surface is visible) — done.
      if (document.activeElement === el) return;
      if (attempt < MAX_ATTEMPTS) {
        raf = requestAnimationFrame(() => tryFocus(attempt + 1));
      }
    };
    const onFocusRequest = () => {
      if (raf) cancelAnimationFrame(raf);
      tryFocus(0);
    };
    window.addEventListener("getstarted:focus-composer", onFocusRequest);
    return () => {
      window.removeEventListener("getstarted:focus-composer", onFocusRequest);
      if (raf) cancelAnimationFrame(raf);
    };
  }, []);

  const showSizeError = React.useCallback((message: string) => {
    if (sizeErrorTimerRef.current) {
      clearTimeout(sizeErrorTimerRef.current);
      sizeErrorTimerRef.current = null;
    }
    sizeErrorIdRef.current += 1;
    setSizeError({ id: sizeErrorIdRef.current, message });
    sizeErrorTimerRef.current = setTimeout(() => {
      setSizeError(null);
      sizeErrorTimerRef.current = null;
    }, 5000);
  }, []);

  const dismissSizeError = React.useCallback(() => {
    if (sizeErrorTimerRef.current) {
      clearTimeout(sizeErrorTimerRef.current);
      sizeErrorTimerRef.current = null;
    }
    setSizeError(null);
  }, []);

  const filterOversizedFiles = React.useCallback((files: File[]): File[] => {
    const valid: File[] = [];
    const rejected: string[] = [];
    for (const file of files) {
      if (file.size > MAX_FILE_SIZE) {
        rejected.push(file.name);
      } else {
        valid.push(file);
      }
    }
    if (rejected.length > 0) {
      showSizeError(
        `${rejected.join(", ")} exceed${rejected.length === 1 ? "s" : ""} the 10MB limit`,
      );
    }
    return valid;
  }, [showSizeError]);

  const handleSend = () => {
    if (input.trim() || pendingFiles.length > 0) {
      const files = pendingFiles.map((pf) => pf.file);
      // No chat_message_sent capture here: useAgentChat.dispatchNow is the
      // SINGLE capture point, firing only when a message actually goes on
      // the wire (direct and queue-drained sends alike). A click is not a
      // send — parked/queued messages must not count until they dispatch.
      onSend(input, files.length > 0 ? files : undefined);
      setInput("");
      setPendingFiles([]);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Slash menu owns the arrow / Tab / Enter / Esc keys while it's open, then
    // returns so the existing logic below never sees them. When the menu is
    // closed this block is inert and the original handling runs unchanged.
    if (menuOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIndex((i) => (i + 1) % visible.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIndex((i) => (i - 1 + visible.length) % visible.length);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setInput("");
        return;
      }
      if (e.key === "Tab" || e.key === "Enter") {
        e.preventDefault();
        const cmd = visible[activeIndex];
        if (!cmd) return;
        if (e.key === "Tab") {
          // Tab fills the input. For an arg command, append a trailing space so
          // the user can immediately type the argument (the space also closes
          // the menu so normal Enter-to-send takes over once an arg is typed).
          setInput(`/${cmd.name}${cmd.acceptsArgs ? " " : ""}`);
          setActiveIndex(0);
        } else {
          // Enter commits the highlighted command immediately — even for an
          // acceptsArgs command. The server handles optional/missing args (and
          // returns an error message when one was required); Tab remains the
          // path to fill-then-type-an-arg. Without this, an acceptsArgs command
          // (e.g. /reset, /compact, /model) could never be submitted via Enter
          // because the trailing-space fill keeps the menu open (infinite refill).
          onSend(`/${cmd.name}`);
          setInput("");
        }
        return;
      }
    }
    if (e.key === "Tab" && suggestedMessage && !input) {
      e.preventDefault();
      setInput(suggestedMessage);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = e.target.files;
    if (!selected) return;

    const valid = filterOversizedFiles(Array.from(selected));
    const newFiles: PendingFile[] = valid.map((file) => ({
      file,
      id: crypto.randomUUID(),
    }));
    setPendingFiles((prev) => [...prev, ...newFiles].slice(0, 10));
    capture("chat_file_uploaded", { file_count: selected.length });

    // Reset input so the same file can be selected again
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const removeFile = (id: string) => {
    setPendingFiles((prev) => prev.filter((pf) => pf.id !== id));
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    const dropped = e.dataTransfer.files;
    if (!dropped.length) return;

    const valid = filterOversizedFiles(Array.from(dropped));
    const newFiles: PendingFile[] = valid.map((file) => ({
      file,
      id: crypto.randomUUID(),
    }));
    setPendingFiles((prev) => [...prev, ...newFiles].slice(0, 10));
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
  };

  const isDisabled = disabled || isUploading;

  return (
    <div className={cn("p-4", !centered && "bg-[#f3efe6] border-t border-[#e0dbd0]")}>
      <div
        className="relative flex flex-col max-w-3xl mx-auto"
        onDrop={handleDrop}
        onDragOver={handleDragOver}
      >
        {pendingFiles.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-2 px-1">
            {pendingFiles.map((pf) => (
              <div
                key={pf.id}
                className="flex items-center gap-1.5 bg-[#e8e3d9] rounded-lg px-2.5 py-1.5 text-xs text-[#5a5549]"
              >
                <FileIcon className="h-3.5 w-3.5 shrink-0 text-[#8a8578]" />
                <span className="truncate max-w-37.5">{pf.file.name}</span>
                <span className="text-[#b5ae9e]">{formatFileSize(pf.file.size)}</span>
                <button
                  type="button"
                  onClick={() => removeFile(pf.id)}
                  className="ml-0.5 text-[#b5ae9e] hover:text-[#5a5549] transition-colors"
                  aria-label={`Remove ${pf.file.name}`}
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        )}

        {sizeError && (
          <div className="flex items-center gap-1.5 mb-2 px-2 py-1.5 bg-red-50 border border-red-200 rounded-lg text-xs text-red-600">
            <span>{sizeError.message}</span>
            <button
              type="button"
              onClick={dismissSizeError}
              className="ml-auto text-red-400 hover:text-red-600"
              aria-label="Dismiss size error"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
        )}

        {!isConnected && (
          <div className="flex items-center gap-2 mb-2 px-2 py-1 rounded-md bg-[#fdf6e3] border border-[#ede3c4] text-xs text-[#8a7340]">
            <span className="h-1.5 w-1.5 rounded-full bg-[#c9a227] animate-pulse" />
            <span>Reconnecting to your agent — messages you send will be delivered once back online.</span>
          </div>
        )}

        {queuedMessages.length > 0 && (
          <div className="flex flex-col gap-1 mb-2 px-2 py-1 rounded-md bg-[#f3efe6] border border-[#e0dbd0]">
            <div className="flex items-center justify-between text-xs text-[#8a8578]">
              <span>{queuedMessages.length} queued</span>
              {onClearQueue && (
                <button
                  type="button"
                  onClick={onClearQueue}
                  className="hover:underline"
                >
                  Clear queue
                </button>
              )}
            </div>
            {queuedMessages.map((q) => {
              const stateLabel =
                q.sendState === "waiting-reconnect"
                  ? "Waiting for reconnect"
                  : q.sendState === "failed"
                    ? "Failed"
                    : q.sendState === "sending"
                      ? "Sending"
                      : null;
              // Steer is only meaningful while a run is live and the item is
              // a plain queued send (not failed / waiting). Mirrors upstream
              // chat-queue.ts:69-86 (canAbort && !sendState).
              const canSteer =
                !!isStreaming && !!onSteerQueued && !q.sendState;
              const canRetry = q.sendState === "failed" && !!onRetryQueued;
              return (
                <div
                  key={q.id}
                  className="flex flex-col gap-0.5 text-sm text-[#1a1a1a]"
                >
                  <div className="flex items-center gap-2">
                    {stateLabel && (
                      <span className="shrink-0 text-[0.65rem] uppercase tracking-wide text-[#8a8578]">
                        {stateLabel}
                      </span>
                    )}
                    <span className="flex-1 truncate">{q.text}</span>
                    {canRetry && (
                      <button
                        type="button"
                        aria-label="Retry queued message"
                        title="Retry"
                        onClick={() => onRetryQueued!(q.id)}
                        className="flex items-center gap-1 text-[#8a8578] hover:text-[#1a1a1a]"
                      >
                        <RefreshCw className="h-3 w-3" />
                        <span className="text-xs">Retry</span>
                      </button>
                    )}
                    {canSteer && (
                      <button
                        type="button"
                        aria-label="Steer queued message"
                        title="Steer now"
                        onClick={() => onSteerQueued!(q.id)}
                        className="flex items-center gap-1 text-[#8a8578] hover:text-[#1a1a1a]"
                      >
                        <CornerDownRight className="h-3 w-3" />
                        <span className="text-xs">Steer</span>
                      </button>
                    )}
                    {onRemoveQueued && (
                      <button
                        type="button"
                        aria-label="Remove queued message"
                        onClick={() => onRemoveQueued(q.id)}
                        className="text-[#b5ae9e] hover:text-[#5a5549]"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    )}
                  </div>
                  {q.sendError && (
                    <span className="text-xs text-red-600 truncate">
                      {q.sendError}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {controls && (
          <div className="flex items-center gap-2 mb-2 px-1">{controls}</div>
        )}

        <div className="relative flex items-end gap-2 border border-[#e0dbd0] rounded-full bg-white px-3 py-2 focus-within:ring-1 focus-within:ring-[#06402B]/20 focus-within:border-[#06402B]/30 transition-all">
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={handleFileSelect}
            aria-label="Attach files"
          />
          <Button
            size="icon"
            variant="ghost"
            className="shrink-0 h-8 w-8 rounded-full text-[#8a8578] hover:text-[#1a1a1a] hover:bg-[#f3efe6]"
            onClick={() => fileInputRef.current?.click()}
            disabled={isDisabled}
            aria-label="Attach file"
          >
            <Paperclip className="h-4 w-4" />
          </Button>

          <div className="relative flex-1">
            {menuOpen && (
              <SlashCommandMenu
                commands={commands}
                query={slash.query}
                activeIndex={activeIndex}
                onSelect={(c) => {
                  if (c.acceptsArgs) {
                    setInput(`/${c.name} `);
                  } else {
                    onSend(`/${c.name}`);
                    setInput("");
                  }
                }}
                onActiveIndexChange={setActiveIndex}
              />
            )}
            {suggestedMessage && !input && (
              <div className="absolute inset-0 pointer-events-none text-[#b5ae9e] text-sm leading-6 py-1 truncate">
                {suggestedMessage}
                <span className="ml-2 text-[#cdc7ba] text-xs">[Tab]</span>
              </div>
            )}
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={suggestedMessage ? "" : "Ask anything"}
              rows={1}
              className="w-full min-h-6 max-h-50 resize-none bg-transparent text-[#1a1a1a] placeholder:text-[#b5ae9e] focus:outline-none text-sm leading-6 py-1"
              disabled={isDisabled}
              style={{ fieldSizing: "content" } as React.CSSProperties}
            />
          </div>

          <div className="flex items-center gap-2">
            {isStreaming && onStop && (
              <Button
                type="button"
                size="icon"
                variant="destructive"
                className="shrink-0 h-8 w-8 rounded-full"
                onClick={() => { capture("chat_stopped"); onStop?.(); }}
                data-testid="stop-button"
                aria-label="Stop agent"
              >
                <Square className="h-3.5 w-3.5 fill-current" />
              </Button>
            )}
            <Button
              type="button"
              size="icon"
              className="shrink-0 h-8 w-8 rounded-full bg-[#06402B] hover:bg-[#0a5c3e] text-white disabled:bg-[#e0dbd0] disabled:text-[#b5ae9e]"
              onClick={handleSend}
              disabled={(!input.trim() && pendingFiles.length === 0) || isDisabled}
              data-testid="send-button"
              // While a run is in flight a send doesn't fire immediately —
              // useAgentChat enqueues it and drains on the server run-ended
              // signal. Surface that as a "Queue" affordance so the button's
              // accessible label / tooltip reflects the actual action.
              // Mirrors upstream OpenClaw web UI (chat/run-controls.ts:83-91).
              aria-label={isStreaming ? "Queue message" : "Send message"}
              title={isStreaming ? "Queue" : "Send"}
            >
              {isUploading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <SendHorizontal className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
