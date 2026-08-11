// Ported from upstream Paperclip's CommandPalette
// (paperclip/ui/src/components/CommandPalette.tsx) (MIT, (c) 2025 Paperclip AI).
// Static "Go to" entries that route between Teams panels. Paths are the
// canonical in-shell `/chat?panel=teams-*` URLs (there is no standalone
// `/teams` route tree).

import { teamsPanelUrl } from "@/lib/shellNav";
import {
  LayoutDashboard, Inbox, Bot, CircleDot, ClipboardCheck, Repeat, Target,
  FolderOpen, History, Settings, Network,
} from "lucide-react";
import type { ComponentType, SVGProps } from "react";

export interface CommandPaletteAction {
  id: string;
  label: string;
  path: string;
  Icon: ComponentType<SVGProps<SVGSVGElement>>;
  keywords?: string[];
}

export const NAV_ACTIONS: CommandPaletteAction[] = [
  { id: "go-dashboard", label: "Dashboard", path: teamsPanelUrl("dashboard"), Icon: LayoutDashboard },
  { id: "go-inbox", label: "Inbox", path: teamsPanelUrl("inbox"), Icon: Inbox, keywords: ["mine", "issues"] },
  { id: "go-agents", label: "Agents", path: teamsPanelUrl("agents"), Icon: Bot, keywords: ["bot"] },
  { id: "go-org-chart", label: "Org chart", path: teamsPanelUrl("org-chart"), Icon: Network, keywords: ["network", "hierarchy"] },
  { id: "go-issues", label: "Issues", path: teamsPanelUrl("issues"), Icon: CircleDot, keywords: ["tasks", "tickets"] },
  // Approvals fold into the inbox "Needs attention" band — same target.
  { id: "go-approvals", label: "Approvals", path: teamsPanelUrl("inbox"), Icon: ClipboardCheck },
  { id: "go-routines", label: "Automations", path: teamsPanelUrl("routines"), Icon: Repeat, keywords: ["cron", "schedule", "routines"] },
  { id: "go-goals", label: "Goals", path: teamsPanelUrl("goals"), Icon: Target },
  { id: "go-projects", label: "Projects", path: teamsPanelUrl("projects"), Icon: FolderOpen },
  { id: "go-activity", label: "Activity", path: teamsPanelUrl("activity"), Icon: History, keywords: ["events", "log"] },
  { id: "go-settings", label: "Settings", path: teamsPanelUrl("settings"), Icon: Settings },
];

export function filterNavActions(query: string): CommandPaletteAction[] {
  const q = query.trim().toLowerCase();
  if (!q) return NAV_ACTIONS;
  return NAV_ACTIONS.filter((action) => {
    if (action.label.toLowerCase().includes(q)) return true;
    return (action.keywords ?? []).some((kw) => kw.toLowerCase().includes(q));
  });
}
