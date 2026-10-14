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
  activity: ["events", "log"],
  mail: ["email", "meetings", "transcripts"],
  // Money absorbed Payments/Products/Customers/Spend as sections of one
  // page — their old search words, plus "usage" (the label the same numbers
  // carry under /settings, ?panel=usage), still need to find it.
  money: ["spend", "usage", "cost", "costs", "billing", "credits", "payments", "customers", "products", "revenue"],
};

/**
 * Derived from the SAME sections the sidebar renders, so the palette
 * can never offer a tab the sidebar hides (or vice versa).
 */
export function navActions(): CommandPaletteAction[] {
  return [
    ...COMPANY_SECTIONS.flatMap((section) =>
      section.items.map(({ panel, label, Icon }) => {
        const key = panel.replace(/^teams-/, "");
        // Mail is its own panel, not a teams-* one.
        const path = panel.startsWith("teams-") ? teamsPanelUrl(key) : `/chat?panel=${panel}`;
        return { id: `go-${key}`, label, path, Icon, keywords: KEYWORDS[key] };
      }),
    ),
    // The two non-sidebar surfaces: the agent roster and the Settings home.
    // The org chart is a view of this panel now, so its search words point here.
    {
      id: "go-agents",
      label: "Agents",
      path: teamsPanelUrl("agents"),
      Icon: Bot,
      keywords: ["bot", "roster", "org chart", "network", "hierarchy"],
    },
    { id: "go-settings", label: "Settings", path: teamsPanelUrl("settings"), Icon: Settings },
  ];
}

export const NAV_ACTIONS: CommandPaletteAction[] = navActions();

export function filterNavActions(
  query: string,
): CommandPaletteAction[] {
  const actions = navActions();
  const q = query.trim().toLowerCase();
  if (!q) return actions;
  return actions.filter((action) => {
    if (action.label.toLowerCase().includes(q)) return true;
    return (action.keywords ?? []).some((kw) => kw.toLowerCase().includes(q));
  });
}
