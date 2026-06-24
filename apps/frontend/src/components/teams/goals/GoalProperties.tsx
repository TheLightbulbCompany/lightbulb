// apps/frontend/src/components/teams/goals/GoalProperties.tsx

// Ported from upstream Paperclip's components/GoalProperties.tsx
// (paperclip/ui/src/components/GoalProperties.tsx) (MIT, © 2025 Paperclip AI).
// Mirrors the issue IssueProperties.tsx PropertyRow layout verbatim. Status +
// Level are inline pickers; Owner + Parent Goal are links; Created + Updated
// are read-only timestamps.

"use client";

import { useState, type ReactNode } from "react";
import Link from "next/link";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { cn } from "@/lib/utils";
import {
  GOAL_LEVELS,
  type Goal,
  type GoalLevel,
  type GoalStatus,
  type CompanyAgent,
} from "@/components/teams/shared/types";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Button } from "@/components/ui/button";
import { GoalStatusIcon } from "./GoalStatusIcon";

export interface GoalPropertiesProps {
  goal: Goal;
  agents?: CompanyAgent[];
  allGoals?: Goal[];
  onStatusChange: (status: GoalStatus) => void;
  onLevelChange: (level: GoalLevel) => void;
  className?: string;
}

function PropertyRow({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div
      className="grid grid-cols-[100px_1fr] gap-3 items-baseline py-2 text-sm"
      style={{
        color: "var(--ink-soft)",
        borderBottom: "1px solid var(--edge)",
      }}
    >
      <dt className="section-label" style={{ marginTop: 0, marginBottom: 0 }}>
        <b>{label}</b>
      </dt>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

const EMPTY_DASH_STYLE = { color: "var(--mute)" } as const;

function levelLabel(level: string): string {
  return level.replace(/\b\w/g, (c) => c.toUpperCase());
}

function LevelPicker({
  level,
  onChange,
}: {
  level: GoalLevel;
  onChange: (level: GoalLevel) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="capitalize cursor-pointer hover:opacity-80 transition-opacity"
          style={{ color: "var(--ink)" }}
        >
          {level}
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-40 p-1" align="start">
        {GOAL_LEVELS.map((l) => (
          <Button
            key={l}
            variant="ghost"
            size="sm"
            className={cn(
              "w-full justify-start text-xs",
              l === level && "bg-[var(--cream-deeper)]",
            )}
            onClick={() => {
              onChange(l);
              setOpen(false);
            }}
          >
            {levelLabel(l)}
          </Button>
        ))}
      </PopoverContent>
    </Popover>
  );
}

export function GoalProperties({
  goal,
  agents = [],
  allGoals = [],
  onStatusChange,
  onLevelChange,
  className,
}: GoalPropertiesProps) {
  const ownerAgent = goal.ownerAgentId
    ? agents.find((a) => a.id === goal.ownerAgentId)
    : null;
  const parentGoal = goal.parentId
    ? allGoals.find((g) => g.id === goal.parentId)
    : null;

  return (
    <dl className={cn("flex flex-col", className)}>
      <PropertyRow label="Status">
        <GoalStatusIcon status={goal.status} onChange={onStatusChange} showLabel />
      </PropertyRow>
      <PropertyRow label="Level">
        <LevelPicker level={goal.level} onChange={onLevelChange} />
      </PropertyRow>
      <PropertyRow label="Owner">
        {ownerAgent ? (
          <Link
            href={`/teams/agents/${ownerAgent.id}`}
            className="hover:underline"
            style={{ color: "var(--ink)" }}
          >
            {ownerAgent.name}
          </Link>
        ) : (
          <span data-empty="true" style={EMPTY_DASH_STYLE}>
            —
          </span>
        )}
      </PropertyRow>
      {goal.parentId && (
        <PropertyRow label="Parent">
          <Link
            href={`/teams/goals/${goal.parentId}`}
            className="hover:underline"
            style={{ color: "var(--ink)" }}
          >
            {parentGoal?.title ?? goal.parentId.slice(0, 8)}
          </Link>
        </PropertyRow>
      )}
      <PropertyRow label="Created">
        {goal.createdAt ? (
          <time dateTime={goal.createdAt} style={{ color: "var(--mute)" }}>
            {timeAgo(goal.createdAt)}
          </time>
        ) : (
          <span data-empty="true" style={EMPTY_DASH_STYLE}>
            —
          </span>
        )}
      </PropertyRow>
      <PropertyRow label="Updated">
        {goal.updatedAt ? (
          <time dateTime={goal.updatedAt} style={{ color: "var(--mute)" }}>
            {timeAgo(goal.updatedAt)}
          </time>
        ) : (
          <span data-empty="true" style={EMPTY_DASH_STYLE}>
            —
          </span>
        )}
      </PropertyRow>
    </dl>
  );
}
