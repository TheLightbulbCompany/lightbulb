// Ported from upstream Paperclip's CommandPalette
// (paperclip/ui/src/components/CommandPalette.tsx) (MIT, (c) 2025 Paperclip AI).
// "Go to" entries DERIVED from the sidebar IA (COMPANY_SECTIONS) — the
// palette covers exactly what the sidebar shows, plus the roster and
// settings. Paths are the canonical in-shell `/chat?panel=teams-*` URLs
// (there is no standalone `/teams` route tree).

import { teamsPanelUrl } from "@/lib/shellNav";
import { COMPANY_SECTIONS, type CompanyNavIcon } from "@/components/chat/companySections";
import { Bot, Settings } from "lucide-react";

export interface CommandPaletteAction {
  id: string;
  label: string;
  path: string;
  Icon: CompanyNavIcon;
  keywords?: string[];
}

// Search aliases by teams-* list key: retired labels ("dashboard",
// "artifacts", "issues", "approvals") so muscle memory survives the renames,
// plus the still-applicable synonyms the palette always had.
const KEYWORDS: Record<string, string[]> = {
  dashboard: ["dashboard"],
  inbox: ["mine", "issues", "approvals"],
  issues: ["issues", "tickets"],
  routines: ["cron", "schedule", "routines"],
  artifacts: ["artifacts"],
  "org-chart": ["network", "hierarchy"],
  activity: ["events", "log"],
  // "usage" is the label the same numbers carry under /settings (?panel=usage),
  // so muscle memory finds Spend.
  spend: ["usage", "cost", "costs", "billing", "credits"],
};

export const NAV_ACTIONS: CommandPaletteAction[] = [
  ...COMPANY_SECTIONS.flatMap((section) =>
    section.items.map(({ panel, label, Icon }) => {
      const key = panel.replace(/^teams-/, "");
      return { id: `go-${key}`, label, path: teamsPanelUrl(key), Icon, keywords: KEYWORDS[key] };
    }),
  ),
  // The two non-sidebar surfaces: the agent roster and the Settings home.
  { id: "go-agents", label: "Agents", path: teamsPanelUrl("agents"), Icon: Bot, keywords: ["bot"] },
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
