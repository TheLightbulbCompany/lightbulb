"use client";

import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { Search } from "lucide-react";
import type { MessageSearchHit } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { capture } from "@/lib/analytics";
import { SearchSnippet } from "./searchSnippet";
import { useMessageSearch } from "./useMessageSearch";

/** Mounted only while the dialog is open, so nothing reads live events (or searches) when closed. */
function SearchResults({ roomId, query, countRef, onPick }: { roomId: string; query: string; countRef: MutableRefObject<number>; onPick: (hit: MessageSearchHit) => void }) {
  const { hits, state } = useMessageSearch(query, { enabled: true, roomId, limit: 50 });
  useEffect(() => { countRef.current = hits.length; }, [countRef, hits.length]);
  return <>
    <div role="status" className="text-ui text-muted-foreground">
      {state === "loading" ? "Searching…"
        : state === "warming" ? "Search is starting up. This can take a few seconds."
          : state === "error" ? "Search failed. Try again."
            : state === "done" ? `${hits.length} ${hits.length === 1 ? "message" : "messages"} found`
              : "Search the message text, including thread replies. Use quotes for an exact phrase."}
    </div>
    {state === "done" && hits.length === 0 && <p className="text-ui">No matching messages. Try a different word or phrase.</p>}
    {hits.length > 0 && <ul aria-label="Search results" className="max-h-[50dvh] overflow-y-auto -mx-2">
      {hits.map((hit) => {
        const validDate = !Number.isNaN(Date.parse(hit.createdAt));
        return <li key={hit.messageId}>
          <button type="button" className="w-full rounded-sm p-3 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() => onPick(hit)}>
            <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-ui"><span className="font-semibold">{hit.authorName || "Unknown author"}</span>
              {validDate && <time dateTime={hit.createdAt} className="text-muted-foreground">{new Date(hit.createdAt).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit" })}</time>}
              {hit.threadRootMessageId && <span className="text-muted-foreground">Thread reply</span>}
            </span>
            <span className="mt-1 block whitespace-pre-wrap break-words text-ui"><SearchSnippet snippet={hit.snippet} /></span>
          </button>
        </li>;
      })}
    </ul>}
  </>;
}

export function RoomMessageSearch({ roomId, onSelect, openToken, hideTrigger = false }: {
  roomId: string; onSelect: (hit: MessageSearchHit) => void;
  /** A change opens the dialog (super-agent: ⋯ › Search in project, and ⌘F); 0 or absent opens nothing. */
  openToken?: number;
  /** No button of its own (super-agent: the header's ⋯ menu opens it). */
  hideTrigger?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const selected = useRef<MessageSearchHit | null>(null);
  const countRef = useRef(0);
  const term = query.trim();
  // Adjusted during render (not an effect): a new token opens the dialog fresh.
  const [seenToken, setSeenToken] = useState(openToken);
  if (seenToken !== openToken) {
    setSeenToken(openToken);
    if (openToken) { setOpen(true); setQuery(""); }
  }

  const reportSearchCompletion = () => {
    if (!term) return;
    const n = countRef.current;
    capture("message_search_used", { result_count_bucket: n === 0 ? "0" : n <= 5 ? "1-5" : "6+" });
  };

  return <Dialog open={open} onOpenChange={(next) => {
    if (!next) reportSearchCompletion();
    setOpen(next);
    if (next) { setQuery(""); selected.current = null; countRef.current = 0; }
  }}>
    {!hideTrigger && <DialogTrigger asChild><Button type="button" variant="ghost" aria-label="Search this chat"><Search size={18} aria-hidden="true" /><span className="hidden sm:inline">Search</span></Button></DialogTrigger>}
    <DialogContent showCloseButton className="w-[calc(100%_-_2rem)] max-w-xl shadow-none" onEscapeKeyDown={(event) => event.stopPropagation()} onCloseAutoFocus={(event) => {
      if (!selected.current) return;
      event.preventDefault();
      onSelect(selected.current);
      selected.current = null;
    }}>
      <DialogTitle className="pr-8">Search this chat</DialogTitle>
      <DialogDescription>Find messages and replies in this chat.</DialogDescription>
      <Input type="search" aria-label="Search messages" placeholder="Search messages…" value={query} onChange={(event) => setQuery(event.target.value)} />
      <SearchResults roomId={roomId} query={query} countRef={countRef} onPick={(hit) => { reportSearchCompletion(); selected.current = hit; setOpen(false); }} />
    </DialogContent>
  </Dialog>;
}
