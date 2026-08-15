"use client";

import {
  useId,
  useMemo,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { User } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Identity } from "@/components/teams/shared/components/Identity";
import { cn } from "@/lib/utils";
import type { CompanyAgent, CompanyMember } from "@/components/teams/shared/types";
import {
  getRecentAssignees,
  trackRecentAssignee,
} from "./recent-assignees";

export interface AssigneePickerProps {
  agents: CompanyAgent[];
  selectedAgentId: string | null;
  onChange: (agentId: string | null) => void;
  /** Optional human members. When present the popover gains a "People" section
   * and ``onUserChange`` is required to commit a human-assignee pick. */
  members?: CompanyMember[];
  /** Currently-assigned human member's Clerk user id (mutually exclusive with
   * ``selectedAgentId`` — only one is ever non-null). */
  selectedUserId?: string | null;
  /** Commit a human-member assignment (or ``null`` to clear). Required to
   * surface the People section; without it the picker stays agent-only. */
  onUserChange?: (userId: string | null) => void;
  /** Display variant: `"row"` is the IssueProperties left-aligned style; `"inline"` is
   * the IssueHeader chip style (sits next to status + priority pickers). */
  variant?: "row" | "inline";
  className?: string;
}

type AssigneeOption =
  | { kind: "clear" }
  | { kind: "agent"; id: string }
  | { kind: "member"; id: string };

export function AssigneePicker({
  agents,
  selectedAgentId,
  onChange,
  members = [],
  selectedUserId = null,
  onUserChange,
  variant = "row",
  className,
}: AssigneePickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const listboxId = useId();

  // People are only offered when the host wired an onUserChange handler.
  const peopleEnabled = onUserChange !== undefined && members.length > 0;

  const selectedAgent = selectedAgentId
    ? agents.find((a) => a.id === selectedAgentId)
    : null;
  const selectedMember = selectedUserId
    ? members.find((m) => m.userId === selectedUserId)
    : null;

  // Trigger label: agent name, member name, an "Unknown …" fallback when the
  // id resolves to nothing (e.g. list not yet loaded), or "Unassigned".
  const triggerLabel = selectedAgent
    ? selectedAgent.name
    : selectedMember
      ? selectedMember.name ?? selectedMember.email ?? "Unknown member"
      : selectedAgentId
        ? "Unknown agent"
        : selectedUserId
          ? "Unknown member"
          : "Unassigned";
  const hasAssignee = Boolean(selectedAgent || selectedMember);

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

  const q = query.trim().toLowerCase();
  const visibleAgents = q
    ? orderedAgents.filter((a) => a.name.toLowerCase().includes(q))
    : orderedAgents;
  const visibleMembers = peopleEnabled
    ? q
      ? members.filter((m) =>
          `${m.name ?? ""} ${m.email ?? ""}`.toLowerCase().includes(q),
        )
      : members
    : [];
  const options: AssigneeOption[] = [
    { kind: "clear" },
    ...visibleAgents.map((agent) => ({ kind: "agent" as const, id: agent.id })),
    ...visibleMembers.map((member) => ({ kind: "member" as const, id: member.userId })),
  ];

  // Picking an agent clears any human assignee, and vice versa — the two are
  // mutually exclusive. The host wires that clear via the change handlers
  // (selecting an agent sends assigneeUserId: null and conversely).
  const handleSelectAgent = (agentId: string | null) => {
    if (agentId !== null) trackRecentAssignee(agentId);
    onChange(agentId);
    setOpen(false);
    setQuery("");
    setActiveIndex(-1);
  };
  const handleSelectMember = (userId: string | null) => {
    onUserChange?.(userId);
    setOpen(false);
    setQuery("");
    setActiveIndex(-1);
  };
  const handleSelectOption = (option: AssigneeOption) => {
    if (option.kind === "agent") handleSelectAgent(option.id);
    else if (option.kind === "member") handleSelectMember(option.id);
    else if (selectedUserId !== null) handleSelectMember(null);
    else handleSelectAgent(null);
  };
  const handleOptionMouseDown = (
    event: MouseEvent<HTMLButtonElement>,
    option: AssigneeOption,
  ) => {
    if (event.button !== 0) return;
    // Commit before the search input blurs and the portal can rerender under
    // the pointer. Touch and assistive clicks still use the click handler.
    event.preventDefault();
    handleSelectOption(option);
  };
  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      setQuery("");
      setActiveIndex(-1);
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((current) => {
        if (event.key === "ArrowUp") {
          return current <= 0 ? options.length - 1 : current - 1;
        }
        if (current < 0) return q && options.length > 1 ? 1 : 0;
        return (current + 1) % options.length;
      });
      return;
    }
    if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      const option = options[activeIndex];
      if (option) handleSelectOption(option);
    }
  };

  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    if (!next) {
      setQuery("");
      setActiveIndex(-1);
    }
  };

  const triggerClass =
    variant === "inline"
      ? "inline-flex items-center gap-1.5 rounded px-2 py-1 text-xs hover:bg-[var(--cream-deeper)]"
      : "inline-flex items-center gap-2 text-sm hover:underline";

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(triggerClass, className)}
          style={{ color: hasAssignee ? "var(--ink)" : "var(--mute)" }}
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
          onChange={(e) => {
            setQuery(e.target.value);
            setActiveIndex(-1);
          }}
          onKeyDown={handleSearchKeyDown}
          placeholder={peopleEnabled ? "Search agents & people..." : "Search agents..."}
          role="combobox"
          aria-autocomplete="list"
          aria-controls={listboxId}
          aria-expanded={open}
          aria-activedescendant={
            activeIndex >= 0 ? `${listboxId}-option-${activeIndex}` : undefined
          }
          className="w-full px-2 py-1.5 text-xs bg-transparent outline-none mb-1 rounded-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--forest)]"
          style={{ borderBottom: "1px solid var(--edge)" }}
        />
        <ul id={listboxId} className="max-h-56 overflow-y-auto" role="listbox">
          <li
            id={`${listboxId}-option-0`}
            role="option"
            aria-selected={selectedAgentId === null && selectedUserId === null}
          >
            <button
              type="button"
              tabIndex={-1}
              onMouseDown={(event) =>
                handleOptionMouseDown(event, { kind: "clear" })
              }
              onClick={() => handleSelectOption({ kind: "clear" })}
              onMouseEnter={() => setActiveIndex(0)}
              className={cn(
                "w-full text-left px-2 py-1.5 text-sm rounded hover:bg-[var(--cream-deeper)]",
                activeIndex === 0 && "bg-[var(--cream-deeper)]",
              )}
              style={{ color: "var(--mute)" }}
            >
              No assignee
            </button>
          </li>
          {peopleEnabled && (
            <li
              className="px-2 pt-2 pb-1 text-[10px] uppercase tracking-wide"
              style={{ color: "var(--mute)" }}
              aria-hidden="true"
            >
              Agents
            </li>
          )}
          {visibleAgents.map((agent, index) => {
            const optionIndex = index + 1;
            const option: AssigneeOption = { kind: "agent", id: agent.id };
            return (
              <li
                id={`${listboxId}-option-${optionIndex}`}
                key={agent.id}
                role="option"
                aria-selected={selectedAgentId === agent.id}
              >
                <button
                  type="button"
                  tabIndex={-1}
                  onMouseDown={(event) => handleOptionMouseDown(event, option)}
                  onClick={() => handleSelectOption(option)}
                  onMouseEnter={() => setActiveIndex(optionIndex)}
                  className={cn(
                    "w-full text-left px-2 py-1.5 text-sm rounded hover:bg-[var(--cream-deeper)]",
                    activeIndex === optionIndex && "bg-[var(--cream-deeper)]",
                  )}
                >
                  {agent.name}
                </button>
              </li>
            );
          })}
          {peopleEnabled && (
            <>
              <li
                className="px-2 pt-2 pb-1 text-[10px] uppercase tracking-wide"
                style={{ color: "var(--mute)" }}
                aria-hidden="true"
              >
                People
              </li>
              {visibleMembers.map((member, index) => {
                const optionIndex = visibleAgents.length + index + 1;
                const option: AssigneeOption = {
                  kind: "member",
                  id: member.userId,
                };
                return (
                  <li
                    id={`${listboxId}-option-${optionIndex}`}
                    key={member.userId}
                    role="option"
                    aria-selected={selectedUserId === member.userId}
                  >
                    <button
                      type="button"
                      tabIndex={-1}
                      onMouseDown={(event) => handleOptionMouseDown(event, option)}
                      onClick={() => handleSelectOption(option)}
                      onMouseEnter={() => setActiveIndex(optionIndex)}
                      className={cn(
                        "w-full text-left px-2 py-1.5 rounded hover:bg-[var(--cream-deeper)]",
                        activeIndex === optionIndex && "bg-[var(--cream-deeper)]",
                      )}
                    >
                      <Identity
                        name={member.name ?? member.email ?? member.userId}
                        avatarUrl={member.imageUrl}
                        size="xs"
                      />
                    </button>
                  </li>
                );
              })}
              {visibleMembers.length === 0 && (
                <li className="px-2 py-1.5 text-xs" style={{ color: "var(--mute)" }}>
                  No people
                </li>
              )}
            </>
          )}
          {visibleAgents.length === 0 && !peopleEnabled && (
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
