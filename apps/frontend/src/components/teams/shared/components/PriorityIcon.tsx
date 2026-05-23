// apps/frontend/src/components/teams/shared/components/PriorityIcon.tsx

// Ported from upstream Paperclip's PriorityIcon.tsx
// (paperclip/ui/src/components/PriorityIcon.tsx) (MIT, (c) 2025 Paperclip AI).

import { useState } from "react";
import { ArrowUp, ArrowDown, Minus, AlertTriangle } from "lucide-react";
import { cn } from "@/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import type { IssuePriority } from "@/components/teams/shared/types";

// Inlined copy of the upstream `priorityColor` map from
// `paperclip/ui/src/lib/status-colors.ts`, retheme-mapped onto the editorial
// palette. Each priority resolves to a single CSS-variable color so the icon
// matches the rest of the Teams UI (no Tailwind hue classes).
//   critical / high  -> --danger  (urgent — red)
//   medium           -> --warn    (attention — amber)
//   low              -> --mute    (low signal — soft neutral)
const priorityColor: Record<IssuePriority, string> = {
  critical: "var(--danger)",
  high: "var(--danger)",
  medium: "var(--warn)",
  low: "var(--mute)",
};

const priorityConfig: Record<IssuePriority, { icon: typeof ArrowUp; color: string; label: string }> = {
  critical: { icon: AlertTriangle, color: priorityColor.critical, label: "Critical" },
  high: { icon: ArrowUp, color: priorityColor.high, label: "High" },
  medium: { icon: Minus, color: priorityColor.medium, label: "Medium" },
  low: { icon: ArrowDown, color: priorityColor.low, label: "Low" },
};

const allPriorities: IssuePriority[] = ["critical", "high", "medium", "low"];

interface PriorityIconProps {
  priority: IssuePriority;
  onChange?: (priority: IssuePriority) => void;
  className?: string;
  showLabel?: boolean;
}

export function PriorityIcon({ priority, onChange, className, showLabel }: PriorityIconProps) {
  const [open, setOpen] = useState(false);
  const config = priorityConfig[priority] ?? priorityConfig.medium;
  const Icon = config.icon;

  const icon = (
    <span
      className={cn(
        "inline-flex items-center justify-center shrink-0",
        onChange && !showLabel && "cursor-pointer",
        className,
      )}
      style={{ color: config.color }}
    >
      <Icon className="h-3.5 w-3.5" />
    </span>
  );

  if (!onChange)
    return showLabel ? (
      <span className="inline-flex items-center gap-1.5">
        {icon}
        <span className="text-sm">{config.label}</span>
      </span>
    ) : (
      icon
    );

  const trigger = showLabel ? (
    <button className="inline-flex items-center gap-1.5 cursor-pointer hover:bg-accent/50 rounded px-1 -mx-1 py-0.5 transition-colors">
      {icon}
      <span className="text-sm">{config.label}</span>
    </button>
  ) : (
    icon
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent className="w-36 p-1" align="start">
        {allPriorities.map((p) => {
          const c = priorityConfig[p];
          const PIcon = c.icon;
          return (
            <Button
              key={p}
              variant="ghost"
              size="sm"
              className={cn("w-full justify-start gap-2 text-xs", p === priority && "bg-accent")}
              onClick={() => {
                onChange(p);
                setOpen(false);
              }}
            >
              <PIcon className="h-3.5 w-3.5" style={{ color: c.color }} />
              {c.label}
            </Button>
          );
        })}
      </PopoverContent>
    </Popover>
  );
}
