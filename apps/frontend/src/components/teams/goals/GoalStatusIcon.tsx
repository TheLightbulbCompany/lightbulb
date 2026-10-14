// apps/frontend/src/components/teams/goals/GoalStatusIcon.tsx

// A goal-status swatch + picker, modeled on the issue StatusIcon
// (paperclip/ui/src/components/StatusBadge.tsx, retheme-mapped). Goal statuses
// are distinct from issue statuses (planned / active / achieved / cancelled),
// so they get their own color map on the editorial palette.

"use client";

import { useState } from "react";
import { cn } from "@/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { GOAL_STATUSES, type GoalStatus } from "@/components/teams/shared/types";

// Retheme-mapped onto the editorial palette (mirrors StatusIcon.tsx):
//   planned    -> --midnight-muted        (not started — muted)
//   active     -> --warn        (in flight — attention amber)
//   achieved   -> --midnight      (success)
//   cancelled  -> --midnight-muted        (abandoned — muted)
const goalStatusColor: Record<GoalStatus, string> = {
  planned: "var(--midnight-muted)",
  active: "var(--warn)",
  achieved: "var(--midnight)",
  cancelled: "var(--midnight-muted)",
};

const goalStatusColorDefault = "var(--midnight-muted)";

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

interface GoalStatusIconProps {
  status: GoalStatus | string;
  onChange?: (status: GoalStatus) => void;
  className?: string;
  showLabel?: boolean;
}

export function GoalStatusIcon({
  status,
  onChange,
  className,
  showLabel,
}: GoalStatusIconProps) {
  const [open, setOpen] = useState(false);
  const swatchColor =
    goalStatusColor[status as GoalStatus] ?? goalStatusColorDefault;
  const isAchieved = status === "achieved";

  const circle = (
    <span
      className={cn(
        "relative inline-flex h-4 w-4 rounded-full border-2 shrink-0",
        onChange && !showLabel && "cursor-pointer",
        className,
      )}
      style={{ color: swatchColor, borderColor: swatchColor }}
      aria-label={statusLabel(status)}
      title={statusLabel(status)}
    >
      {isAchieved && (
        <span className="absolute inset-0 m-auto h-2 w-2 rounded-full bg-current" />
      )}
    </span>
  );

  if (!onChange)
    return showLabel ? (
      <span className="inline-flex items-center gap-1.5">
        {circle}
        <span className="text-ui">{statusLabel(status)}</span>
      </span>
    ) : (
      circle
    );

  const trigger = showLabel ? (
    <button className="inline-flex items-center gap-1.5 cursor-pointer hover:bg-[var(--cream-deep)] rounded-sm px-1 -mx-1 py-0.5 transition-colors">
      {circle}
      <span className="text-ui">{statusLabel(status)}</span>
    </button>
  ) : (
    circle
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent
        className="w-40 p-1"
        align="start"
        style={{
          background: "var(--cream)",
          border: "1px solid var(--edge)",
          color: "var(--midnight)",
        }}
      >
        {GOAL_STATUSES.map((s) => (
          <Button
            key={s}
            variant="ghost"
            size="sm"
            className={cn(
              "w-full justify-start gap-2 text-caption",
              s === status && "bg-[var(--cream-deeper)]",
            )}
            onClick={() => {
              onChange(s);
              setOpen(false);
            }}
          >
            <GoalStatusIcon status={s} />
            {statusLabel(s)}
          </Button>
        ))}
      </PopoverContent>
    </Popover>
  );
}
