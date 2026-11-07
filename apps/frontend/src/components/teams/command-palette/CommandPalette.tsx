"use client";

// Ported from upstream Paperclip's CommandPalette
// (paperclip/ui/src/components/CommandPalette.tsx) (MIT, (c) 2025 Paperclip AI).
// Vanilla shadcn Dialog + Input + result list (no cmdk dep). v1: navigate +
// search across chats, tasks, files, messages and agents.

import { useState, useMemo, useRef } from "react";
import { useRouter } from "next/navigation";
import { Dialog as DialogPrimitive } from "radix-ui";
import { Dialog, DialogPortal, DialogTitle } from "@/components/ui/dialog";
import { Search } from "lucide-react";
import { filterNavActions, type CommandPaletteAction } from "./commandPaletteActions";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { navigateShell } from "@/lib/shellNav";
import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";
import { useFilteredCommandResults } from "./useFilteredCommandResults";
import { useRoomCommandResults } from "@/components/messaging/roomCommands";
import { useMessageSearch } from "@/components/messaging/useMessageSearch";
import { plainSnippet, SearchSnippet } from "@/components/messaging/searchSnippet";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { roomDisplayName } from "@lightbulb/shared";
import { openTaskPane } from "@/components/workspace/workspaceController";
import { KIND_WORD, openOutput, outputName } from "@/components/creations/openOutput";
import { MIN_TERM, useTaskSearch } from "./useTaskSearch";

type FlatItem = {
  kind: "conversation" | "task" | "file" | "message" | "nav" | "agent";
  id: string;
  label: string;
  path: string;
  run?: () => void;
};

export interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

// Inner component is mounted only while the dialog is open. Remounting on
// every open transition is what gives us "reset query + selectedIndex on
// reopen" without needing a setState-in-effect.
//
// Overlay + content are wired against the radix primitive directly (not via
// the shadcn DialogContent wrapper) because that wrapper bakes in its own
// DialogPortal + a heavily-darkened default DialogOverlay. Using the wrapper
// here would render two backdrops on top of each other and the dark one
// would cover the editorial midnight tint we want (matches the
// NewIssueDialog backdrop).
//
// No open/close animation, deliberately: ⌘K is keyboard-initiated and fires
// dozens of times a day, where motion only reads as lag (Raycast has none).
/** Hits whose text holds the query as a whole word come first; the rest keep the index's order. */
export function wholeWordFirst<T extends { snippet: string }>(hits: readonly T[], query: string): T[] {
  const term = query.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (!term) return [...hits];
  const word = new RegExp(`(^|[^\\p{L}\\p{N}])${term}($|[^\\p{L}\\p{N}])`, "iu");
  const exact = (h: T) => word.test(plainSnippet(h.snippet));
  return [...hits.filter(exact), ...hits.filter((h) => !exact(h))];
}

export function CommandPalette({ open, onOpenChange }: CommandPaletteProps) {
  // A result was opened: focus stays where it took the person (the message it scrolled to), not back on the page.
  const opened = useRef(false);
  const change = (next: boolean) => { if (next) opened.current = false; onOpenChange(next); };
  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogPortal>
        <DialogPrimitive.Overlay
          className="fixed inset-0 z-50"
          // The app's one --scrim (W14: a light grey sheet over the dark app).
          style={{ background: "var(--scrim)" }}
        />
        <DialogPrimitive.Content
          onCloseAutoFocus={(event) => { if (opened.current) event.preventDefault(); }}
          // Pinned from the top, not centred, so results arriving never move the box (W14: 312→319→302).
          className="editorial-card fixed left-[50%] top-[14vh] z-50 grid w-full max-w-xl translate-x-[-50%] gap-0 overflow-hidden border-0 p-0 shadow-lg sm:rounded-lg"
          style={{
            background: "var(--cream)",
            border: "1px solid var(--edge)",
            color: "var(--midnight)",
          }}
        >
          <DialogTitle className="sr-only">Command palette</DialogTitle>
          {open ? <CommandPaletteContent open={open} onOpenChange={onOpenChange} onRun={() => { opened.current = true; }} /> : null}
        </DialogPrimitive.Content>
      </DialogPortal>
    </Dialog>
  );
}

function CommandPaletteContent({ open, onOpenChange, onRun }: CommandPaletteProps & { onRun?: () => void }) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [rawSelectedIndex, setSelectedIndex] = useState(0);

  const admin = useIsWorkspaceAdmin();
  const navResults = useMemo(() => filterNavActions(query, admin), [query, admin]);
  // Tasks, not Paperclip issues: the Tasks and Files groups take the old Issues group's place.
  const { agents } = useFilteredCommandResults(query, open);
  const found = useTaskSearch(query, open);
  const conversations = useRoomCommandResults(query, open);
  const workspace = useMessagingWorkspaceOptional();
  const messageSearch = useMessageSearch(query, { enabled: open && !!workspace, limit: 8, minLength: MIN_TERM });
  // Whole-word matches first: the index also matches near spellings ("Lindell" found "on-line hello", W15-10).
  const messageHits = useMemo(() => wholeWordFirst(messageSearch.hits, query).map((hit) => {
    const room = workspace?.directoryProps?.rooms.find((candidate) => candidate.roomId === hit.roomId);
    return {
      id: `message:${hit.messageId}`,
      label: plainSnippet(hit.snippet),
      snippet: hit.snippet,
      sublabel: room ? `${hit.authorName || "Unknown author"} · ${roomDisplayName(room, room.participants)}` : hit.authorName || "Unknown author",
      run: () => workspace?.openNotificationTarget(hit.roomId, hit.threadRootMessageId, hit.messageId),
    };
  }), [messageSearch.hits, query, workspace]);
  const warming = messageSearch.state === "warming";

  const flatItems = useMemo<FlatItem[]>(() => {
    const items: FlatItem[] = [];
    conversations.forEach((c) => items.push({ kind: "conversation", id: c.id, label: c.label, path: "", run: c.run }));
    found.tasks.forEach((t) => items.push({ kind: "task", id: t.taskId, label: t.title, path: "", run: () => openTaskPane(t.taskId) }));
    found.files.forEach((o) => items.push({ kind: "file", id: `${o.taskId}:${o.id}`, label: outputName(o), path: "", run: () => openOutput(o) }));
    messageHits.forEach((m) => items.push({ kind: "message", id: m.id, label: m.label, path: "", run: m.run }));
    navResults.forEach((a) => items.push({ kind: "nav", id: a.id, label: a.label, path: a.path }));
    agents.forEach((a) => items.push({ kind: "agent", id: a.id, label: a.name, path: agentInspectorUrl(a.id) }));
    return items;
  }, [conversations, found, messageHits, navResults, agents]);

  // Derive a clamped index from raw state — no setState-in-effect needed.
  const selectedIndex = Math.min(rawSelectedIndex, Math.max(0, flatItems.length - 1));

  const select = (item: FlatItem) => {
    onRun?.();
    onOpenChange(false);
    if (item.run) { item.run(); return; }
    // In-shell `/chat?...` targets commit through the History API (same
    // mechanism as handleShellLinkClick) so a jump costs no RSC round-trip on
    // the force-dynamic /chat route. Settings is its own route.
    if (item.path.startsWith("/chat")) navigateShell(item.path);
    else router.push(item.path);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIndex((i) => Math.min(i + 1, flatItems.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const item = flatItems[selectedIndex];
      if (item) select(item);
    }
  };

  return (
    <>
      <div
        className="flex items-center gap-2 px-3"
        style={{ borderBottom: "1px solid var(--edge)" }}
      >
        <Search
          className="h-4 w-4 shrink-0"
          style={{ color: "var(--midnight-muted)" }}
          aria-hidden="true"
        />
        <input
          autoFocus
          placeholder="Search projects, tasks, files and messages"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          className="flex-1 h-11 bg-transparent border-0 outline-none px-0 text-ui placeholder:text-[color:var(--midnight-muted)]"
          style={{ color: "var(--midnight)" }}
        />
      </div>
      {/* While a search is typed the list keeps its height (results land without the box resizing). */}
      <div className={query.trim() ? "h-[min(360px,60vh)] overflow-y-auto py-2" : "max-h-[60vh] overflow-y-auto py-2"} style={{ background: "var(--cream)" }}>
          {flatItems.length === 0 ? (
            <div
              className="px-4 py-8 text-center text-ui italic"
              style={{
                color: "var(--midnight-muted)",
                fontFamily: "var(--font-serif), Georgia, serif",
              }}
            >
              {warming ? "Search is starting up…" : messageSearch.state === "loading" ? "Searching…" : "No results."}
            </div>
          ) : (
            <>
              {conversations.length > 0 && (
                <Group label="Chats">
                  {conversations.map((c) => {
                    const idx = flatItems.findIndex((it) => it.kind === "conversation" && it.id === c.id);
                    return (
                      <Row
                        key={c.id}
                        label={c.label}
                        sublabel={c.sublabel}
                        selected={selectedIndex === idx}
                        onMouseEnter={() => setSelectedIndex(idx)}
                        onClick={() => select(flatItems[idx])}
                      />
                    );
                  })}
                </Group>
              )}
              {found.tasks.length > 0 && (
                <Group label="Tasks">
                  {found.tasks.map((t) => {
                    const idx = flatItems.findIndex((it) => it.kind === "task" && it.id === t.taskId);
                    return (
                      <Row key={t.taskId} label={t.title} sublabel="Task" selected={selectedIndex === idx}
                        onMouseEnter={() => setSelectedIndex(idx)} onClick={() => select(flatItems[idx])} />
                    );
                  })}
                </Group>
              )}
              {found.files.length > 0 && (
                <Group label="Files">
                  {found.files.map((o) => {
                    const id = `${o.taskId}:${o.id}`;
                    const idx = flatItems.findIndex((it) => it.kind === "file" && it.id === id);
                    return (
                      // Its readable name and the words it matched in, never its slug alone (W16-11).
                      <Row key={id} label={o.snippet ? <span className="grid min-w-0"><span className="truncate">{outputName(o)}</span>
                        <span className="truncate text-caption text-[var(--midnight-muted)]">{o.snippet}</span></span> : outputName(o)}
                        sublabel={KIND_WORD[o.kind]} selected={selectedIndex === idx}
                        onMouseEnter={() => setSelectedIndex(idx)} onClick={() => select(flatItems[idx])} />
                    );
                  })}
                </Group>
              )}
              {messageHits.length > 0 && (
                <Group label="Messages">
                  {messageHits.map((m) => {
                    const idx = flatItems.findIndex((it) => it.kind === "message" && it.id === m.id);
                    return (
                      <Row key={m.id} label={<SearchSnippet snippet={m.snippet} around />} sublabel={m.sublabel} selected={selectedIndex === idx}
                        onMouseEnter={() => setSelectedIndex(idx)} onClick={() => select(flatItems[idx])} />
                    );
                  })}
                </Group>
              )}
              {navResults.length > 0 && (
                <Group label="Navigate">
                  {navResults.map((a) => {
                    const idx = flatItems.findIndex((it) => it.kind === "nav" && it.id === a.id);
                    return (
                      <Row
                        key={a.id}
                        Icon={a.Icon}
                        label={a.label}
                        selected={selectedIndex === idx}
                        onMouseEnter={() => setSelectedIndex(idx)}
                        onClick={() => select(flatItems[idx])}
                      />
                    );
                  })}
                </Group>
              )}
              {agents.length > 0 && (
                <Group label="Agents">
                  {agents.map((a) => {
                    const idx = flatItems.findIndex((it) => it.kind === "agent" && it.id === a.id);
                    return (
                      <Row
                        key={a.id}
                        label={a.name}
                        sublabel="Agent"
                        selected={selectedIndex === idx}
                        onMouseEnter={() => setSelectedIndex(idx)}
                        onClick={() => select(flatItems[idx])}
                      />
                    );
                  })}
                </Group>
              )}
            </>
          )}
        </div>
    </>
  );
}

function Group({ label, children }: { label: string; children: React.ReactNode }) {
  // Sentence-case heads and badges in Satoshi, no mono caps (dev walk W12-14).
  return (
    <div className="py-1">
      <div className="px-3 py-1 text-ui font-medium" style={{ color: "var(--midnight-muted)" }}>
        {label}
      </div>
      <ul role="listbox" className="flex flex-col">
        {children}
      </ul>
    </div>
  );
}

function Row({
  Icon,
  label,
  sublabel,
  selected,
  onMouseEnter,
  onClick,
}: {
  Icon?: CommandPaletteAction["Icon"];
  label: React.ReactNode;
  sublabel?: string;
  selected: boolean;
  onMouseEnter: () => void;
  onClick: () => void;
}) {
  return (
    <li
      role="option"
      aria-selected={selected}
      data-cmd-row
      onMouseEnter={onMouseEnter}
      onClick={onClick}
      className="flex items-center gap-2 px-3 py-1.5 text-ui cursor-pointer transition-colors"
      style={{
        background: selected ? "var(--cream-deeper)" : "transparent",
        borderLeft: selected ? "2px solid var(--midnight)" : "2px solid transparent",
        color: "var(--midnight)",
      }}
      onMouseLeave={(e) => {
        if (!selected) e.currentTarget.style.background = "transparent";
      }}
      onMouseOver={(e) => {
        if (!selected) e.currentTarget.style.background = "var(--cream-deep)";
      }}
    >
      {Icon && (
        <Icon
          className="h-4 w-4 shrink-0"
          style={{ color: selected ? "var(--midnight)" : "var(--midnight-muted)" }}
          aria-hidden="true"
        />
      )}
      <span className="flex-1 truncate">{label}</span>
      {sublabel && <span className="shrink-0 text-ui text-[var(--midnight-muted)]">{sublabel}</span>}
    </li>
  );
}
