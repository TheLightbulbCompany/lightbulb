// Ported from upstream Paperclip's CommandPalette
// (paperclip/ui/src/components/CommandPalette.tsx) (MIT, (c) 2025 Paperclip AI).
// Static "Go to" entries that route between Teams panels in TeamsSidebar.

import {
  LayoutDashboard, Inbox, Bot, CircleDot, ClipboardCheck, Repeat, Target,
  FolderOpen, History, DollarSign, Hexagon, Users, Settings, Network,
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
  { id: "go-dashboard", label: "Dashboard", path: "/teams/dashboard", Icon: LayoutDashboard },
  { id: "go-inbox", label: "Inbox", path: "/teams/inbox", Icon: Inbox, keywords: ["mine", "issues"] },
  { id: "go-agents", label: "Agents", path: "/teams/agents", Icon: Bot, keywords: ["bot"] },
  { id: "go-org-chart", label: "Org chart", path: "/teams/org-chart", Icon: Network, keywords: ["network", "hierarchy"] },
  { id: "go-issues", label: "Issues", path: "/teams/issues", Icon: CircleDot, keywords: ["tasks", "tickets"] },
  { id: "go-approvals", label: "Approvals", path: "/teams/approvals", Icon: ClipboardCheck },
  { id: "go-routines", label: "Routines", path: "/teams/routines", Icon: Repeat, keywords: ["cron", "schedule"] },
  { id: "go-goals", label: "Goals", path: "/teams/goals", Icon: Target },
  { id: "go-projects", label: "Projects", path: "/teams/projects", Icon: FolderOpen },
  { id: "go-activity", label: "Activity", path: "/teams/activity", Icon: History, keywords: ["events", "log"] },
  { id: "go-costs", label: "Costs", path: "/teams/costs", Icon: DollarSign, keywords: ["billing", "spend"] },
  { id: "go-skills", label: "Skills", path: "/teams/skills", Icon: Hexagon, keywords: ["tools"] },
  { id: "go-members", label: "Members", path: "/teams/members", Icon: Users, keywords: ["people", "team"] },
  { id: "go-settings", label: "Settings", path: "/teams/settings", Icon: Settings },
];

export function filterNavActions(query: string): CommandPaletteAction[] {
  const q = query.trim().toLowerCase();
  if (!q) return NAV_ACTIONS;
  return NAV_ACTIONS.filter((action) => {
    if (action.label.toLowerCase().includes(q)) return true;
    return (action.keywords ?? []).some((kw) => kw.toLowerCase().includes(q));
  });
}
