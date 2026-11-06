// Ported from upstream Paperclip's CommandPalette
// (paperclip/ui/src/components/CommandPalette.tsx) (MIT, (c) 2025 Paperclip AI).
// "Go to" entries DERIVED from the sidebar IA (COMPANY_SECTIONS) — the
// palette covers exactly what the sidebar shows, plus the roster and
// settings. Paths are the canonical in-shell `/chat?panel=teams-*` URLs
// (there is no standalone `/teams` route tree).

import { teamsPanelUrl } from "@/lib/shellNav";
import { COMPANY_SECTIONS, type CompanyNavIcon } from "@/components/chat/companySections";
import { Bot, Briefcase, Clock, Settings, ShieldCheck } from "lucide-react";

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
  issues: ["issues", "tickets"],
  routines: ["cron", "schedule", "routines"],
  artifacts: ["artifacts"],
  // Home and the Inbox tab folded into Activity, so their old words land here.
  activity: ["events", "log", "home", "dashboard", "inbox", "mine", "approvals"],
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
export function navActions(superAgent = false, admin = true): CommandPaletteAction[] {
  if (superAgent) return superAgentActions(admin);
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
    // Its own route, not a dock tab: the palette navigates there by router.
    { id: "go-settings", label: "Settings", path: "/settings", Icon: Settings },
  ];
}

/** Super-agent (UX slice 4): Work and Money are gone, so the palette offers where their things live now, inside
 *  the shell: Settings (and its Business and Approvals rows) and Activity's Scheduled group. */
function superAgentActions(admin: boolean): CommandPaletteAction[] {
  const dropped = new Set(["teams-money", "teams-issues", "teams-projects", "teams-routines", "teams-goals", "teams-cases"]);
  return [
    ...navActions().filter((action) => !dropped.has(action.path.match(/[?&]panel=([^&]+)/)?.[1] ?? "") && action.id !== "go-settings"),
    { id: "go-scheduled", label: "Scheduled", path: "/chat?panel=activity&section=scheduled", Icon: Clock, keywords: KEYWORDS.routines },
    { id: "go-settings", label: "Settings", path: "/chat?panel=settings", Icon: Settings },
    { id: "go-business", label: "Business", path: "/chat?panel=settings&section=business", Icon: Briefcase, keywords: KEYWORDS.money },
    // Approvals is an admin's row in Settings (ADMIN_ONLY_PANELS): never offered to anyone else.
    ...(admin ? [{ id: "go-approvals", label: "Approvals", path: "/chat?panel=settings&section=approvals", Icon: ShieldCheck, keywords: ["approve", "ask", "auto", "permissions"] }] : []),
  ];
}

export const NAV_ACTIONS: CommandPaletteAction[] = navActions();

export function filterNavActions(
  query: string,
  superAgent = false,
  admin = true,
): CommandPaletteAction[] {
  const actions = navActions(superAgent, admin);
  const q = query.trim().toLowerCase();
  if (!q) return actions;
  return actions.filter((action) => {
    if (action.label.toLowerCase().includes(q)) return true;
    return (action.keywords ?? []).some((kw) => kw.toLowerCase().includes(q));
  });
}
