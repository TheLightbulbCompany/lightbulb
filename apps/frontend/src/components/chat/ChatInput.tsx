"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { capture } from "@/lib/analytics";
import { SendHorizontal, Paperclip, X, FileIcon, Loader2, Square } from "lucide-react";
import { cn } from "@/lib/utils";

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10 MB

interface PendingFile {
  file: File;
  id: string;
}

interface ChatInputProps {
  onSend: (message: string, files?: File[]) => void;
  onStop?: () => void;
  disabled?: boolean;
  centered?: boolean;
  isUploading?: boolean;
  isStreaming?: boolean;
  suggestedMessage?: string;
  queuedMessages?: { id: string; text: string }[];
  onRemoveQueued?: (id: string) => void;
  onClearQueue?: () => void;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

export function ChatInput({
  onSend,
  onStop,
  disabled,
  centered,
  isUploading,
  isStreaming,
  suggestedMessage,
  queuedMessages = [],
  onRemoveQueued,
  onClearQueue,
}: ChatInputProps) {
  const [input, setInput] = React.useState("");
  const [pendingFiles, setPendingFiles] = React.useState<PendingFile[]>([]);
  const [sizeError, setSizeError] = React.useState<{ id: number; message: string } | null>(null);
  const sizeErrorTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  const sizeErrorIdRef = React.useRef(0);
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  // Clear timer on unmount
  React.useEffect(() => {
    return () => {
      if (sizeErrorTimerRef.current) {
        clearTimeout(sizeErrorTimerRef.current);
      }
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
      capture("chat_message_sent", { has_files: files.length > 0 });
      onSend(input, files.length > 0 ? files : undefined);
      setInput("");
      setPendingFiles([]);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
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
            {queuedMessages.map((q) => (
              <div
                key={q.id}
                className="flex items-center gap-2 text-sm text-[#1a1a1a]"
              >
                <span className="flex-1 truncate">{q.text}</span>
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
            ))}
          </div>
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
            {suggestedMessage && !input && (
              <div className="absolute inset-0 pointer-events-none text-[#b5ae9e] text-sm leading-6 py-1 truncate">
                {suggestedMessage}
                <span className="ml-2 text-[#cdc7ba] text-xs">[Tab]</span>
              </div>
            )}
            <textarea
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
              aria-label="Send message"
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
