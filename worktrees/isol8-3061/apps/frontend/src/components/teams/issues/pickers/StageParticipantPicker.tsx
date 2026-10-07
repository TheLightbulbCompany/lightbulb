"use client";

// Ported from upstream Paperclip's ExecutionParticipantPicker.tsx
// (paperclip/ui/src/components/ExecutionParticipantPicker.tsx) (MIT,
// (c) 2025 Paperclip AI). Adapted to the Isol8 rail: agents + members arrive as
// props (matching AssigneePicker) rather than a useQuery directory fetch, and
// the surface is themed with the cream/forest tokens. A multi-select popover —
// the LabelPicker pattern, but over the agent + human-member union — toggling an
// option commits the FULL desired participant-value set so the host can rebuild
// the executionPolicy stage wholesale (replace semantics, never a partial diff).

import { useMemo, useState } from "react";
import { Check, Plus, User } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Identity } from "@/components/teams/shared/components/Identity";
import { cn } from "@/lib/utils";
import type { CompanyAgent, CompanyMember } from "@/components/teams/shared/types";

// Shared chip style so selected participants read identically to the rest of
// the rail's pill rows (Labels, Related Tasks, blockers).
const CHIP_STYLE = {
  background: "var(--cream-deep)",
  color: "var(--midnight-soft)",
  border: "1px solid var(--edge)",
} as const;

export interface StageParticipantPickerProps {
  /** Currently-selected participant values (`agent:<id>` / `user:<id>`). */
  selected: string[];
  /** Pickable agents — the agent half of the participant union. */
  agents: CompanyAgent[];
  /** Pickable human members — the user half of the participant union. Empty is
   * fine (the picker then offers agents only). */
  members?: CompanyMember[];
  /** Receives the FULL desired participant-value set (replace semantics). The
   * host maps it onto the executionPolicy stage of the matching type. */
  onChange: (values: string[]) => void;
  /** Word shown on the empty-state add affordance, e.g. "reviewer". */
  noun: string;
  className?: string;
}

/** A multi-select rail control over the agent + member union. Renders the
 * applied participants as chips and an "Add {noun}" trigger that opens a themed
 * search popover; toggling an option sends the full desired set. */
export function StageParticipantPicker({
  selected,
  agents,
  members = [],
  onChange,
  noun,
  className,
}: StageParticipantPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const agentName = (id: string) =>
    agents.find((a) => a.id === id)?.name ?? `${id.slice(0, 8)}`;
  const memberName = (id: string) => {
    const member = members.find((m) => m.userId === id);
    return member?.name ?? member?.email ?? "Unknown member";
  };

  // Human-readable chip label for a selection value.
  const valueLabel = (value: string): string => {
    if (value.startsWith("agent:")) return agentName(value.slice("agent:".length));
    if (value.startsWith("user:")) return memberName(value.slice("user:".length));
    return value;
  };

  const q = query.trim().toLowerCase();
  const visibleAgents = useMemo(
    () =>
      agents
        .filter((a) => a.status !== "terminated")
        .filter((a) => (q ? a.name.toLowerCase().includes(q) : true)),
    [agents, q],
  );
  const visibleMembers = useMemo(
    () =>
      members.filter((m) =>
        q ? `${m.name ?? ""} ${m.email ?? ""}`.toLowerCase().includes(q) : true,
      ),
    [members, q],
  );

  const toggle = (value: string) => {
    const next = selected.includes(value)
      ? selected.filter((v) => v !== value)
      : [...selected, value];
    onChange(next);
  };

  return (
    <span className={cn("flex flex-wrap items-center gap-1", className)}>
      {selected.map((value) => (
        <span
          key={value}
          className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-xs"
          style={CHIP_STYLE}
        >
          <User size={12} strokeWidth={1.75} className="shrink-0" style={{ color: "var(--midnight-muted)" }} />
          <span className="truncate">{valueLabel(value)}</span>
        </span>
      ))}
      <Popover
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) setQuery("");
        }}
      >
        <PopoverTrigger asChild>
          <button
            type="button"
            className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-xs transition-colors hover:underline"
            style={{ border: "1px solid var(--edge)", color: "var(--midnight-muted)" }}
          >
            <Plus size={12} strokeWidth={1.75} />
            Add {noun}
          </button>
        </PopoverTrigger>
        <PopoverContent
          align="start"
          className="w-64 p-1"
          style={{
            background: "var(--cream)",
            border: "1px solid var(--edge)",
            color: "var(--midnight)",
          }}
        >
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={
              members.length > 0
                ? `Search agents & people...`
                : `Search agents...`
            }
            aria-label={`Search ${noun}s`}
            className="w-full px-2 py-1.5 text-xs bg-transparent mb-1 rounded-sm"
            style={{ borderBottom: "1px solid var(--edge)" }}
          />
          <ul
            className="max-h-48 overflow-y-auto"
            role="listbox"
            aria-multiselectable="true"
          >
            <li
              className="px-2 pt-1 pb-1 label-caps"
              style={{ color: "var(--midnight-muted)" }}
              aria-hidden="true"
            >
              Agents
            </li>
            {visibleAgents.map((agent) => {
              const value = `agent:${agent.id}`;
              const isSelected = selected.includes(value);
              return (
                <li key={agent.id} role="option" aria-selected={isSelected}>
                  <button
                    type="button"
                    onClick={() => toggle(value)}
                    className={cn(
                      "flex w-full items-center gap-2 px-2 py-1.5 text-left text-sm rounded-sm hover:bg-[var(--cream-deep)]",
                      isSelected && "bg-[var(--cream-deeper)]",
                    )}
                  >
                    <span className="truncate">{agent.name}</span>
                    {isSelected && (
                      <Check size={16}
                        className="ml-auto shrink-0"
                        aria-hidden="true"
                      />
                    )}
                  </button>
                </li>
              );
            })}
            {visibleAgents.length === 0 && (
              <li className="px-2 py-1.5 text-xs" style={{ color: "var(--midnight-muted)" }}>
                No matching agents.
              </li>
            )}
            {members.length > 0 && (
              <>
                <li
                  className="px-2 pt-2 pb-1 label-caps"
                  style={{ color: "var(--midnight-muted)" }}
                  aria-hidden="true"
                >
                  People
                </li>
                {visibleMembers.map((member) => {
                  const value = `user:${member.userId}`;
                  const isSelected = selected.includes(value);
                  return (
                    <li
                      key={member.userId}
                      role="option"
                      aria-selected={isSelected}
                    >
                      <button
                        type="button"
                        onClick={() => toggle(value)}
                        className={cn(
                          "flex w-full items-center gap-2 px-2 py-1.5 text-left rounded-sm hover:bg-[var(--cream-deep)]",
                          isSelected && "bg-[var(--cream-deeper)]",
                        )}
                      >
                        <Identity
                          name={member.name ?? member.email ?? member.userId}
                          avatarUrl={member.imageUrl}
                          size="xs"
                        />
                        {isSelected && (
                          <Check size={16}
                            className="ml-auto shrink-0"
                            aria-hidden="true"
                          />
                        )}
                      </button>
                    </li>
                  );
                })}
                {visibleMembers.length === 0 && (
                  <li
                    className="px-2 py-1.5 text-xs"
                    style={{ color: "var(--midnight-muted)" }}
                  >
                    No matching people.
                  </li>
                )}
              </>
            )}
          </ul>
        </PopoverContent>
      </Popover>
    </span>
  );
}
