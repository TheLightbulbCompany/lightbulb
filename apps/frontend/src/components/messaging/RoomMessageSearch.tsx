"use client";

import { useRef, useState } from "react";
import { Search } from "lucide-react";
import type { MessageSearchHit } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { capture } from "@/lib/analytics";
import { SearchSnippet } from "./searchSnippet";
import { useMessageSearch } from "./useMessageSearch";

export function RoomMessageSearch({ roomId, onSelect }: { roomId: string; onSelect: (hit: MessageSearchHit) => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const { hits, state } = useMessageSearch(query, { enabled: open, roomId, limit: 50 });
  const selected = useRef<MessageSearchHit | null>(null);
  const term = query.trim();

  const reportSearchCompletion = () => {
    if (!term) return;
    capture("message_search_used", { result_count_bucket: hits.length === 0 ? "0" : hits.length <= 5 ? "1-5" : "6+" });
  };

  return <Dialog open={open} onOpenChange={(next) => {
    if (!next) reportSearchCompletion();
    setOpen(next);
    if (next) { setQuery(""); selected.current = null; }
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
        {state === "loading" ? "Searching…"
          : state === "warming" ? "Search is starting up. This can take a few seconds."
            : state === "error" ? "Search failed. Try again."
              : state === "done" ? `${hits.length} ${hits.length === 1 ? "message" : "messages"} found`
                : "Search the message text, including thread replies. Use quotes for an exact phrase."}
      </div>
      {state === "done" && hits.length === 0 && <p className="text-sm">No matching messages. Try a different word or phrase.</p>}
      {hits.length > 0 && <ul aria-label="Search results" className="max-h-[50dvh] overflow-y-auto -mx-2">
        {hits.map((hit) => {
          const validDate = !Number.isNaN(Date.parse(hit.createdAt));
          return <li key={hit.messageId}>
            <button type="button" className="w-full rounded-sm p-3 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => { reportSearchCompletion(); selected.current = hit; setOpen(false); }}>
              <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm"><span className="font-semibold">{hit.authorName || "Unknown author"}</span>
                {validDate && <time dateTime={hit.createdAt} className="text-muted-foreground">{new Date(hit.createdAt).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" })}</time>}
                {hit.threadRootMessageId && <span className="text-muted-foreground">Thread reply</span>}
              </span>
              <span className="mt-1 block whitespace-pre-wrap break-words text-sm"><SearchSnippet snippet={hit.snippet} /></span>
            </button>
          </li>;
        })}
      </ul>}
    </DialogContent>
  </Dialog>;
}
