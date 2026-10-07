"use client";

import { Search } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export interface PanelToolbarProps {
  searchValue: string;
  onSearchChange: (value: string) => void;
  searchPlaceholder: string;
  searchAriaLabel?: string;
  filters?: ReactNode;
  actions?: ReactNode;
  /**
   * Hide the search box while keeping the toolbar (its filters and actions).
   * For a view where searching would be meaningless or misleading — a control
   * that visibly changes nothing is worse than no control.
   */
  hideSearch?: boolean;
  className?: string;
}

export function PanelToolbar({
  searchValue,
  onSearchChange,
  searchPlaceholder,
  searchAriaLabel,
  filters,
  actions,
  hideSearch = false,
  className,
}: PanelToolbarProps) {
  return (
    <div className={cn("flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between", className)}>
      <div className="flex flex-1 items-center gap-2">
        {!hideSearch && (
        <div className="relative flex-1 max-w-[320px]">
          <Search size={16}
            aria-hidden="true"
            className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[var(--midnight-muted)]"
          />
          <input
            type="search"
            value={searchValue}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder={searchPlaceholder}
            aria-label={searchAriaLabel ?? searchPlaceholder}
            className="w-full rounded-md border border-[var(--edge)] bg-[var(--cream)] py-1.5 pl-7 pr-2 text-ui text-[var(--midnight)] transition-colors"
          />
        </div>
        )}
        {filters && <div className="flex items-center gap-1.5">{filters}</div>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
