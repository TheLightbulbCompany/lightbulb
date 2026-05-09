// apps/frontend/src/components/teams/inbox/ViewModeToggle.tsx

// Segmented list/board view-mode control for the inbox toolbar.
// Mirrors upstream IssuesList toggle (paperclip/ui/src/components/IssuesList.tsx)
// (MIT, (c) 2025 Paperclip AI). Editorial retheme: forest active fill, ink text.

"use client";

import { Columns3, Rows3 } from "lucide-react";
import { cn } from "@/lib/utils";
import type { InboxViewMode } from "@/components/teams/inbox/hooks/useInboxViewMode";

export interface ViewModeToggleProps {
  value: InboxViewMode;
  onChange: (value: InboxViewMode) => void;
  className?: string;
}

export function ViewModeToggle({ value, onChange, className }: ViewModeToggleProps) {
  return (
    <div
      role="group"
      aria-label="View mode"
      className={cn(
        "inline-flex items-center gap-0.5 rounded-md border p-0.5",
        className,
      )}
      style={{ borderColor: "var(--edge)", backgroundColor: "var(--paper)" }}
    >
      <SegmentButton
        label="List"
        Icon={Rows3}
        active={value === "list"}
        onClick={() => value !== "list" && onChange("list")}
      />
      <SegmentButton
        label="Board"
        Icon={Columns3}
        active={value === "board"}
        onClick={() => value !== "board" && onChange("board")}
      />
    </div>
  );
}

function SegmentButton({
  label,
  Icon,
  active,
  onClick,
}: {
  label: string;
  Icon: typeof Rows3;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded px-2 py-1 text-xs transition-colors",
      )}
      style={{
        backgroundColor: active ? "var(--forest-tint)" : "transparent",
        color: active ? "var(--forest)" : "var(--mute)",
      }}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
      {label}
    </button>
  );
}
