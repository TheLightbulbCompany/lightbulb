// apps/frontend/src/components/teams/cases/CaseStatusBadge.tsx

// Ported from upstream Paperclip's StatusBadge (paperclip/ui/src/components/StatusBadge.tsx)
// and the CaseStatusPicker in pages/Cases.tsx + pages/CaseDetail.tsx
// (paperclip/ui/src/pages/CaseDetail.tsx) (MIT, (c) 2025 Paperclip AI).
// Upstream's status palette maps onto our tokens: amber -> --warn,
// sky -> --info, green -> --live, muted -> --fg-mute. One picker serves both
// the list rows and the detail header (upstream keeps two copies).

"use client";

import { useState } from "react";
import { Check, ChevronDown } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { CASE_STATUSES, type CaseStatus } from "@/components/teams/shared/types";

const STATUS_COLOR: Record<string, string> = {
  draft: "var(--fg-mute)",
  in_progress: "var(--warn)",
  in_review: "var(--info)",
  approved: "var(--live)",
  done: "var(--live)",
  cancelled: "var(--fg-mute)",
};

export function CaseStatusBadge({ status }: { status: string }) {
  const color = STATUS_COLOR[status] ?? "var(--fg-mute)";
  return (
    <span
      className="inline-flex shrink-0 items-center rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap"
      style={{ color, background: `color-mix(in srgb, ${color} 12%, transparent)` }}
    >
      {status.replace(/[_-]/g, " ")}
    </span>
  );
}

/** Status dropdown — the primary human write in v1. */
export function CaseStatusPicker({
  status,
  onChange,
  disabled,
  align = "start",
}: {
  status: CaseStatus;
  onChange: (next: CaseStatus) => void;
  disabled?: boolean;
  align?: "start" | "end";
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          disabled={disabled}
          className="inline-flex items-center gap-1 rounded-md hover:bg-[var(--plane-raised)] disabled:opacity-50"
          aria-label="Change case status"
          onClick={(event) => {
            // Rows are links: the picker must not navigate.
            event.preventDefault();
            event.stopPropagation();
          }}
        >
          <CaseStatusBadge status={status} />
          <ChevronDown className="h-3.5 w-3.5 text-[var(--fg-mute)]" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align={align}
        className="w-44 p-1"
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
        }}
      >
        {CASE_STATUSES.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => {
              setOpen(false);
              if (s !== status) onChange(s);
            }}
            className="flex w-full items-center justify-between rounded px-2 py-1.5 text-left hover:bg-[var(--plane-raised)]"
          >
            <CaseStatusBadge status={s} />
            {s === status && <Check className="h-4 w-4 text-[var(--fg-mute)]" />}
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
}
