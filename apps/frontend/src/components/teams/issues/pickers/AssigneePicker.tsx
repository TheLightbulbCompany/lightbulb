"use client";

import { useMemo, useState } from "react";
import { User } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import type { CompanyAgent } from "@/components/teams/shared/types";
import {
  getRecentAssignees,
  trackRecentAssignee,
} from "./recent-assignees";

export interface AssigneePickerProps {
  agents: CompanyAgent[];
  selectedAgentId: string | null;
  onChange: (agentId: string | null) => void;
  /** Display variant: `"row"` is the IssueProperties left-aligned style; `"inline"` is
   * the IssueHeader chip style (sits next to status + priority pickers). */
  variant?: "row" | "inline";
  className?: string;
}

export function AssigneePicker({
  agents,
  selectedAgentId,
  onChange,
  variant = "row",
  className,
}: AssigneePickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const selected = selectedAgentId
    ? agents.find((a) => a.id === selectedAgentId)
    : null;
  const triggerLabel = selected
    ? selected.name
    : selectedAgentId
      ? "Unknown agent"
      : "Unassigned";

  // Merge recent + remainder so frequently-picked bubble up. Recent ids
  // that no longer match an active agent are dropped.
  const orderedAgents = useMemo(() => {
    const recent = getRecentAssignees();
    const byId = new Map(agents.map((a) => [a.id, a]));
    const recentlyPicked = recent
      .map((id) => byId.get(id))
      .filter((a): a is CompanyAgent => a !== undefined);
    const recentSet = new Set(recentlyPicked.map((a) => a.id));
    const rest = agents.filter((a) => !recentSet.has(a.id));
    return [...recentlyPicked, ...rest];
  }, [agents]);

  const visibleAgents = query.trim()
    ? orderedAgents.filter((a) =>
        a.name.toLowerCase().includes(query.toLowerCase().trim()),
      )
    : orderedAgents;

  const handleSelect = (agentId: string | null) => {
    if (agentId !== null) trackRecentAssignee(agentId);
    onChange(agentId);
    setOpen(false);
    setQuery("");
  };

  const triggerClass =
    variant === "inline"
      ? "inline-flex items-center gap-1.5 rounded px-2 py-1 text-xs hover:bg-[var(--cream-deeper)]"
      : "inline-flex items-center gap-2 text-sm hover:underline";

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(triggerClass, className)}
          style={{ color: selected ? "var(--ink)" : "var(--mute)" }}
        >
          <User className="h-3.5 w-3.5" />
          <span>{triggerLabel}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-64 p-1"
        style={{
          background: "var(--cream)",
          border: "1px solid var(--edge)",
          color: "var(--ink)",
        }}
      >
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search agents..."
          className="w-full px-2 py-1.5 text-xs bg-transparent outline-none mb-1"
          style={{ borderBottom: "1px solid var(--edge)" }}
        />
        <ul className="max-h-48 overflow-y-auto" role="listbox">
          <li role="option" aria-selected={selectedAgentId === null}>
            <button
              type="button"
              onClick={() => handleSelect(null)}
              className="w-full text-left px-2 py-1.5 text-sm rounded hover:bg-[var(--cream-deeper)]"
              style={{ color: "var(--mute)" }}
            >
              No assignee
            </button>
          </li>
          {visibleAgents.map((agent) => (
            <li
              key={agent.id}
              role="option"
              aria-selected={selectedAgentId === agent.id}
            >
              <button
                type="button"
                onClick={() => handleSelect(agent.id)}
                className="w-full text-left px-2 py-1.5 text-sm rounded hover:bg-[var(--cream-deeper)]"
              >
                {agent.name}
              </button>
            </li>
          ))}
          {visibleAgents.length === 0 && (
            <li
              className="px-2 py-2 text-xs"
              style={{ color: "var(--mute)" }}
            >
              No matches
            </li>
          )}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
