"use client";

import { useRef, useState } from "react";
import { Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { capture } from "@/lib/analytics";
import type { RoomMessage, RoomParticipant } from "./types";

interface RoomMessageSearchProps {
  roomId: string;
  messages: readonly RoomMessage[];
  participants: readonly RoomParticipant[];
  loading?: boolean;
  complete?: boolean;
  error?: boolean;
  errorMessage?: string | null;
  onLoad?: () => void;
  onSelect: (message: RoomMessage) => void;
}

export function RoomMessageSearch({ roomId, messages, participants, loading = false, complete = false, error = false, errorMessage = null, onLoad, onSelect }: RoomMessageSearchProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const selected = useRef<RoomMessage | null>(null);
  const term = query.trim().toLowerCase();
  const roomMessages = messages.filter((message) => message.roomId === roomId);
  const loadedMessageIds = new Set(roomMessages.map((message) => message.messageId));
  const matches = term ? roomMessages.filter((message) => message.text.toLowerCase().includes(term)).reverse() : [];

  // Fires once per completed search — closing the dialog (however: Escape,
  // overlay click, or picking a result) after the user actually typed
  // something. An empty term means the dialog was opened and dismissed
  // without a search ever happening.
  const reportSearchCompletion = () => {
    if (!term) return;
    const resultCountBucket = matches.length === 0 ? "0" : matches.length <= 5 ? "1-5" : "6+";
    capture("message_search_used", { result_count_bucket: resultCountBucket });
  };

  return <Dialog open={open} onOpenChange={(next) => {
    if (!next) reportSearchCompletion();
    setOpen(next);
    if (next) { setQuery(""); selected.current = null; onLoad?.(); }
  }}>
    <DialogTrigger asChild><Button type="button" variant="ghost" aria-label="Search this conversation"><Search size={18} aria-hidden="true" /><span className="hidden sm:inline">Search</span></Button></DialogTrigger>
    <DialogContent showCloseButton className="w-[calc(100%_-_2rem)] max-w-xl shadow-none" onEscapeKeyDown={(event) => event.stopPropagation()} onCloseAutoFocus={(event) => {
      if (!selected.current) return;
      event.preventDefault();
      onSelect(selected.current);
      selected.current = null;
    }}>
      <DialogTitle className="pr-8">Search this conversation</DialogTitle>
      <DialogDescription>Find messages and replies in this conversation.</DialogDescription>
      <Input type="search" aria-label="Search messages" placeholder="Search messages…" value={query} onChange={(event) => setQuery(event.target.value)} />
      <div role="status" className="text-sm text-muted-foreground">
        {loading ? "Loading conversation history. Results may be incomplete."
          : error ? (errorMessage ? `Could not load all messages: ${errorMessage} Results may be incomplete.` : "Could not load all messages. Results may be incomplete.")
            : !complete ? "Conversation history is not fully loaded. Results may be incomplete."
              : term ? `${matches.length} ${matches.length === 1 ? "message" : "messages"} found` : "Search the message text, including thread replies."}
      </div>
      {!loading && !complete && onLoad && <Button type="button" variant="outline" onClick={onLoad}>Load conversation history</Button>}
      {term && matches.length === 0 && <p className="text-sm">No matching messages{complete ? ". Try a different word or phrase." : " in the history loaded so far."}</p>}
      {matches.length > 0 && <ul aria-label="Search results" className="max-h-[50dvh] overflow-y-auto -mx-2">
        {matches.map((message) => {
          const name = participants.find((member) => member.kind === message.author.kind && member.id === message.author.id)?.displayName || message.author.displayName || "Unknown author";
          const validDate = !Number.isNaN(Date.parse(message.createdAt));
          const threadLoaded = !message.threadRootMessageId || loadedMessageIds.has(message.threadRootMessageId);
          const start = Math.max(0, message.text.toLowerCase().indexOf(term) - 60);
          return <li key={message.messageId}>
            <button type="button" disabled={!threadLoaded} className="w-full rounded-sm p-3 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
              onClick={() => { reportSearchCompletion(); selected.current = message; setOpen(false); }}>
              <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm"><span className="font-semibold">{name}</span>
                {validDate && <time dateTime={message.createdAt} className="text-muted-foreground">{new Date(message.createdAt).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" })}</time>}
                {message.threadRootMessageId && <span className="text-muted-foreground">Thread reply</span>}
                {!threadLoaded && <span className="text-muted-foreground">Thread not loaded</span>}
              </span>
              <span className="mt-1 block whitespace-pre-wrap break-words text-sm">{start > 0 ? "…" : ""}{message.text.slice(start, start + 240)}{message.text.length > start + 240 ? "…" : ""}</span>
            </button>
          </li>;
        })}
      </ul>}
    </DialogContent>
  </Dialog>;
}
