"use client";

// A goal picker, modeled on issues/pickers/ProjectPicker.tsx + AssigneePicker.
// Resolves selectable parents from the caller's existing `/goals` list so the
// CreateGoalDialog no longer asks for a raw "Parent goal ID" text input. Takes
// a minimal `{ id; title }` shape so it works against both the panel's local
// Goal type and the shared `Goal`.

import { useState } from "react";
import { Target } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

export interface GoalPickerOption {
  id: string;
  title: string;
}

export interface GoalPickerProps {
  goals: GoalPickerOption[];
  selectedGoalId: string | null;
  onChange: (goalId: string | null) => void;
  /** Goal id to exclude (e.g. the goal being edited can't be its own parent). */
  excludeGoalId?: string | null;
  /** Label for the empty selection. Defaults to the create-goal dialog's
   * "No parent"; the task rail passes "No goal". */
  emptyLabel?: string;
  className?: string;
}

export function GoalPicker({
  goals,
  selectedGoalId,
  onChange,
  excludeGoalId,
  emptyLabel = "No parent",
  className,
}: GoalPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const selected = selectedGoalId
    ? goals.find((g) => g.id === selectedGoalId)
    : null;
  const triggerLabel = selected
    ? selected.title
    : selectedGoalId
      ? "Unknown goal"
      : emptyLabel;

  const selectable = goals.filter((g) => g.id !== excludeGoalId);
  const visibleGoals = query.trim()
    ? selectable.filter((g) =>
        g.title.toLowerCase().includes(query.toLowerCase().trim()),
      )
    : selectable;

  const handleSelect = (goalId: string | null) => {
    onChange(goalId);
    setOpen(false);
    setQuery("");
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex items-center gap-2 text-sm hover:underline",
            className,
          )}
          style={{ color: selected ? "var(--midnight)" : "var(--midnight-muted)" }}
        >
          <Target size={16} />
          <span className="truncate">{triggerLabel}</span>
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
          placeholder="Search goals..."
          className="w-full px-2 py-1.5 text-xs bg-transparent mb-1"
          style={{ borderBottom: "1px solid var(--edge)" }}
        />
        <ul className="max-h-48 overflow-y-auto" role="listbox">
          <li role="option" aria-selected={selectedGoalId === null}>
            <button
              type="button"
              onClick={() => handleSelect(null)}
              className="w-full text-left px-2 py-1.5 text-sm rounded-sm hover:bg-[var(--cream-deep)]"
              style={{ color: "var(--midnight-muted)" }}
            >
              {emptyLabel}
            </button>
          </li>
          {visibleGoals.map((goal) => (
            <li
              key={goal.id}
              role="option"
              aria-selected={selectedGoalId === goal.id}
            >
              <button
                type="button"
                onClick={() => handleSelect(goal.id)}
                className="w-full text-left px-2 py-1.5 text-sm rounded-sm hover:bg-[var(--cream-deep)]"
              >
                {goal.title}
              </button>
            </li>
          ))}
          {visibleGoals.length === 0 && (
            <li className="px-2 py-2 text-xs" style={{ color: "var(--midnight-muted)" }}>
              No matches
            </li>
          )}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
