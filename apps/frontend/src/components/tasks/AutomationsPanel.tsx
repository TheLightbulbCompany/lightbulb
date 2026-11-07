"use client";

// Work › Automations. Without the super-agent, today's RoutinesPanel renders untouched. With it, Work dissolved (UX
// slice 4): a project's schedules are its pane's Scheduled view and Activity's Scheduled group, and one opens in the
// pane (AutomationDetail). This page is reached only by a stale link the alias missed, so it shows the one list.

import type { ReactElement } from "react";
import { openPanel, openTaskPane } from "@/components/workspace/workspaceController";
import { ScheduledList } from "./ScheduledList";

const WORKSPACE = { kind: "workspace" } as const;

export function AutomationsPanel(): ReactElement {
  return <div className="px-6 py-4">
    <ScheduledList scope={WORKSPACE} onOpen={openTaskPane} onOpenAutomation={(id) => openPanel("automation", { id }, { pushUrl: true })} />
  </div>;
}
