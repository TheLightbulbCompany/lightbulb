"use client";

import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { useSuperAgent } from "@/hooks/useLiveEvents";
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
  // Super-agent: a sentence-case head, no caps (dev walk W12-14).
  const superAgent = useSuperAgent();
  return (
    <Collapsible open={open} onOpenChange={onOpenChange} className={className}>
      <CollapsibleTrigger
        className={cn(
          "flex w-full items-center gap-2 px-3 py-2 transition-colors hover:bg-[var(--edge)]",
          "border-b border-[var(--edge)]",
        )}
      >
        <ChevronRight size={16}
          aria-hidden="true"
          className="shrink-0 transition-transform [[data-state=open]>&]:rotate-90 text-[var(--midnight-muted)]"
        />
        <span className={superAgent ? "text-ui font-medium text-[var(--midnight)]" : "label-caps text-[var(--midnight)]"}>
          {label}
        </span>
        {typeof count === "number" && (
          <span className="text-caption tabular-nums text-[var(--midnight-muted)]">{count}</span>
        )}
      </CollapsibleTrigger>
      <CollapsibleContent>{children}</CollapsibleContent>
    </Collapsible>
  );
}
