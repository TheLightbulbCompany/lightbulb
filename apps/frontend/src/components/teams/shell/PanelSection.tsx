"use client";

import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";

export interface PanelSectionProps {
  label: ReactNode;
  count?: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
  className?: string;
}

export function PanelSection({
  label,
  count,
  open,
  onOpenChange,
  children,
  className,
}: PanelSectionProps) {
  return (
    <Collapsible open={open} onOpenChange={onOpenChange} className={className}>
      <CollapsibleTrigger
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 transition-colors hover:bg-[var(--edge)]",
          "border-b border-[var(--edge)]",
        )}
      >
        <ChevronRight
          aria-hidden="true"
          className="h-3.5 w-3.5 shrink-0 transition-transform [[data-state=open]>&]:rotate-90 text-[var(--mute)]"
        />
        <span className="text-[12px] font-semibold uppercase tracking-[0.08em] text-[var(--ink)]">
          {label}
        </span>
        {typeof count === "number" && (
          <span className="text-[11px] tabular-nums text-[var(--mute)]">{count}</span>
        )}
      </CollapsibleTrigger>
      <CollapsibleContent>{children}</CollapsibleContent>
    </Collapsible>
  );
}
