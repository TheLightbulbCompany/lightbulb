"use client";

import { useRef, useState } from "react";
import { Search } from "lucide-react";
import { sessionAgentName, useSessionSearch, type SessionSearchHit } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { openSessionPanel } from "@/components/workspace/workspaceController";
import { useMessagingWorkspaceOptional } from "./MessagingWorkspaceContext";
import { SearchSnippet } from "./searchSnippet";

/** Mounted only while the dialog is open, so nothing searches while it is closed. */
function SessionResults({ query, onPick }: { query: string; onPick: (hit: SessionSearchHit) => void }) {
  const { messaging } = useLiveEvents();
  const workspace = useMessagingWorkspaceOptional();
  const { hits, state } = useSessionSearch(messaging, query, { enabled: true, limit: 50 });
  const author = (hit: SessionSearchHit) => hit.authorKind === "agent"
    ? hit.authorName || sessionAgentName(hit.provider)
    : workspace?.resolveSessionOwner(hit.authorId).ownerName || "A teammate";
  return <>
    <div role="status" className="text-ui text-muted-foreground">
      {state === "loading" ? "Searching…"
        : state === "warming" ? "Search is starting up. This can take a few seconds."
          : state === "error" ? "Search failed. Try again."
            : state === "done" ? `${hits.length} ${hits.length === 1 ? "turn" : "turns"} found`
              : "Search what people asked and what the agent answered in shared sessions."}
    </div>
    {state === "done" && hits.length === 0 && <p className="text-ui">Nothing matches. Try a different word or phrase.</p>}
    {hits.length > 0 && <ul aria-label="Session search results" className="max-h-[50dvh] overflow-y-auto -mx-2">
      {hits.map((hit) => {
        const validDate = !Number.isNaN(Date.parse(hit.createdAt));
        return <li key={`${hit.sessionId}:${hit.seq}`}>
          <button type="button" className="w-full rounded-sm p-3 text-left hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={() => onPick(hit)}>
            <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1 text-ui">
              <span className="font-semibold">{hit.sessionTitle || "Session"}</span>
              <span className="text-muted-foreground">{author(hit)}</span>
              {validDate && <time dateTime={hit.createdAt} className="text-muted-foreground">{new Date(hit.createdAt).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</time>}
            </span>
            <span className="mt-1 block whitespace-pre-wrap break-words text-ui"><SearchSnippet snippet={hit.snippet} /></span>
          </button>
        </li>;
      })}
    </ul>}
  </>;
}

/** Search what was said in coding sessions; a hit opens that session's conversation at its turn. */
export function SessionSearch() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const picked = useRef<SessionSearchHit | null>(null);
  return <Dialog open={open} onOpenChange={(next) => { setOpen(next); if (next) { setQuery(""); picked.current = null; } }}>
    <DialogTrigger asChild><Button type="button" variant="ghost" size="icon-sm" className="ml-auto" aria-label="Search sessions"><Search aria-hidden="true" /></Button></DialogTrigger>
    <DialogContent showCloseButton className="w-[calc(100%_-_2rem)] max-w-xl shadow-none" onEscapeKeyDown={(event) => event.stopPropagation()} onCloseAutoFocus={(event) => {
      const hit = picked.current;
      if (!hit) return;
      event.preventDefault();
      picked.current = null;
      openSessionPanel(hit.sessionId, hit.seq);
    }}>
      <DialogTitle className="pr-8">Search sessions</DialogTitle>
      <DialogDescription>Find a prompt or an answer in any session shared with you, or your own.</DialogDescription>
      <Input type="search" aria-label="Search session text" placeholder="Search sessions…" value={query} onChange={(event) => setQuery(event.target.value)} />
      <SessionResults query={query} onPick={(hit) => { picked.current = hit; setOpen(false); }} />
    </DialogContent>
  </Dialog>;
}
