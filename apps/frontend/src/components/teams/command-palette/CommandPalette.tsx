"use client";

// Ported from upstream Paperclip's CommandPalette
// (paperclip/ui/src/components/CommandPalette.tsx) (MIT, (c) 2025 Paperclip AI).
// Vanilla shadcn Dialog + Input + result list (no cmdk dep). v1: navigate +
// search across agents/issues/projects. Drops "create" actions until #3d
// (NewIssueDialog) merges.

import { useState, useMemo } from "react";
import { useRouter } from "next/navigation";
import { Dialog as DialogPrimitive } from "radix-ui";
import { Dialog, DialogPortal, DialogTitle } from "@/components/ui/dialog";
import { Search } from "lucide-react";
import { filterNavActions, type CommandPaletteAction } from "./commandPaletteActions";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { navigateShell, teamsDetailUrl } from "@/lib/shellNav";
import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";
import { useFilteredCommandResults } from "./useFilteredCommandResults";
import { useRoomCommandResults } from "@/components/messaging/roomCommands";
import { useMessageSearch } from "@/components/messaging/useMessageSearch";
import { plainSnippet } from "@/components/messaging/searchSnippet";
import { useMessagingWorkspaceOptional } from "@/components/messaging/MessagingWorkspaceContext";
import { roomDisplayName } from "@lightbulb/shared";

type FlatItem = {
  kind: "conversation" | "message" | "nav" | "agent" | "issue" | "project";
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
export function CommandPalette({ open, onOpenChange }: CommandPaletteProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogPortal>
        <DialogPrimitive.Overlay
          className="fixed inset-0 z-50"
          style={{ background: "color-mix(in srgb, var(--midnight) 45%, transparent)" }}
        />
        <DialogPrimitive.Content
          className="editorial-card fixed left-[50%] top-[50%] z-50 grid w-full max-w-xl translate-x-[-50%] translate-y-[-50%] gap-0 overflow-hidden border-0 p-0 shadow-lg sm:rounded-lg"
          style={{
            background: "var(--cream)",
            border: "1px solid var(--edge)",
            color: "var(--midnight)",
          }}
        >
          <DialogTitle className="sr-only">Command palette</DialogTitle>
          {open ? <CommandPaletteContent open={open} onOpenChange={onOpenChange} /> : null}
        </DialogPrimitive.Content>
      </DialogPortal>
    </Dialog>
  );
}

function CommandPaletteContent({ open, onOpenChange }: CommandPaletteProps) {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [rawSelectedIndex, setSelectedIndex] = useState(0);

  const superAgent = useSuperAgent();
  const admin = useIsWorkspaceAdmin();
  const navResults = useMemo(
    () => filterNavActions(query, superAgent, admin),
    [query, superAgent, admin],
  );
  const { agents, issues, projects } = useFilteredCommandResults(query, open);
  const conversations = useRoomCommandResults(query, open);
  const workspace = useMessagingWorkspaceOptional();
  const messageSearch = useMessageSearch(query, { enabled: open && !!workspace, limit: 8, minLength: 2 });
  const messageHits = useMemo(() => messageSearch.hits.map((hit) => {
    const room = workspace?.directoryProps?.rooms.find((candidate) => candidate.roomId === hit.roomId);
    return {
      id: `message:${hit.messageId}`,
      label: plainSnippet(hit.snippet),
      sublabel: room ? `${hit.authorName || "Unknown author"} · ${roomDisplayName(room, room.participants)}` : hit.authorName || "Unknown author",
      run: () => workspace?.openNotificationTarget(hit.roomId, hit.threadRootMessageId, hit.messageId),
    };
  }), [messageSearch.hits, workspace]);
  const warming = messageSearch.state === "warming";

  const flatItems = useMemo<FlatItem[]>(() => {
    const items: FlatItem[] = [];
    conversations.forEach((c) => items.push({ kind: "conversation", id: c.id, label: c.label, path: "", run: c.run }));
    messageHits.forEach((m) => items.push({ kind: "message", id: m.id, label: m.label, path: "", run: m.run }));
    navResults.forEach((a) => items.push({ kind: "nav", id: a.id, label: a.label, path: a.path }));
    agents.forEach((a) => items.push({ kind: "agent", id: a.id, label: a.name, path: agentInspectorUrl(a.id) }));
    issues.forEach((i) => items.push({ kind: "issue", id: i.id, label: i.title, path: teamsDetailUrl("issue", i.id) }));
    projects.forEach((p) => items.push({ kind: "project", id: p.id, label: p.name, path: teamsDetailUrl("project", p.id) }));
    return items;
  }, [conversations, messageHits, navResults, agents, issues, projects]);

  // Derive a clamped index from raw state — no setState-in-effect needed.
  const selectedIndex = Math.min(rawSelectedIndex, Math.max(0, flatItems.length - 1));

  const select = (item: FlatItem) => {
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
          placeholder="Search messages, agents, issues, projects, or jump to..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          className="flex-1 h-11 bg-transparent border-0 outline-none px-0 text-ui placeholder:text-[color:var(--midnight-muted)]"
          style={{ color: "var(--midnight)" }}
        />
      </div>
      <div className="max-h-[60vh] overflow-y-auto py-2" style={{ background: "var(--cream)" }}>
          {flatItems.length === 0 ? (
            <div
              className="px-4 py-8 text-center text-ui italic"
              style={{
                color: "var(--midnight-muted)",
                fontFamily: "var(--font-serif), Georgia, serif",
              }}
            >
              {warming ? "Search is starting up…" : "No results."}
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
              {messageHits.length > 0 && (
                <Group label="Messages">
                  {messageHits.map((m) => {
                    const idx = flatItems.findIndex((it) => it.kind === "message" && it.id === m.id);
                    return (
                      <Row key={m.id} label={m.label} sublabel={m.sublabel} selected={selectedIndex === idx}
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
              {issues.length > 0 && (
                <Group label="Issues">
                  {issues.map((i) => {
                    const idx = flatItems.findIndex((it) => it.kind === "issue" && it.id === i.id);
                    return (
                      <Row
                        key={i.id}
                        label={i.title}
                        sublabel={i.identifier ?? "Issue"}
                        selected={selectedIndex === idx}
                        onMouseEnter={() => setSelectedIndex(idx)}
                        onClick={() => select(flatItems[idx])}
                      />
                    );
                  })}
                </Group>
              )}
              {projects.length > 0 && (
                <Group label="Projects">
                  {projects.map((p) => {
                    const idx = flatItems.findIndex((it) => it.kind === "project" && it.id === p.id);
                    return (
                      <Row
                        key={p.id}
                        label={p.name}
                        sublabel="Project"
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
  // Super-agent: sentence-case heads and badges in Satoshi, no mono caps (dev walk W12-14).
  const superAgent = useSuperAgent();
  return (
    <div className="py-1">
      <div
        className={superAgent ? "px-3 py-1 text-ui font-medium" : "px-3 py-1 text-2xs uppercase"}
        style={superAgent ? { color: "var(--midnight-muted)" } : {
          color: "var(--midnight-muted)",
          fontFamily: "var(--font-geist-mono), ui-monospace, SFMono-Regular, monospace",
          letterSpacing: "0.14em",
        }}
      >
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
  label: string;
  sublabel?: string;
  selected: boolean;
  onMouseEnter: () => void;
  onClick: () => void;
}) {
  const superAgent = useSuperAgent();
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
      {sublabel && (superAgent
        ? <span className="shrink-0 text-ui text-[var(--midnight-muted)]">{sublabel}</span>
        : <span
          className="agent-pill shrink-0"
          style={{ background: "var(--cream-deep)" }}
        >
          {sublabel}
        </span>
      )}
    </li>
  );
}
