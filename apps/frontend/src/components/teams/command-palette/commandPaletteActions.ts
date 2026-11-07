// The ⌘K palette's "Go to" entries: the places in the shell. Paths are the
// canonical in-shell `/chat?panel=` URLs.

import type { ComponentType, SVGProps } from "react";
import { Bell, Bot, Briefcase, Clock, Images, Mail, Settings, ShieldCheck } from "lucide-react";

// One icon prop type for every row (the palette renders with className/style) — lucide icons satisfy it.
export type CompanyNavIcon = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

export interface CommandPaletteAction {
  id: string;
  label: string;
  path: string;
  Icon: CompanyNavIcon;
  keywords?: string[];
}

/** Where things live inside the shell: Activity (tasks, Needs you, Scheduled), Mail, Creations, the roster and
 *  Settings (and its Business and Approvals rows). Retired names stay as search words so muscle memory lands. */
export function navActions(admin = true): CommandPaletteAction[] {
  return [
    // Home and the Inbox tab folded into Activity, and the Paperclip task board is gone: their old words land here.
    { id: "go-activity", label: "Activity", path: "/chat?panel=activity", Icon: Bell, keywords: ["events", "log", "home", "dashboard", "inbox", "mine", "approvals", "tasks", "issues", "tickets"] },
    { id: "go-mail", label: "Mail", path: "/chat?panel=mail", Icon: Mail, keywords: ["email", "meetings", "transcripts"] },
    { id: "go-artifacts", label: "Creations", path: "/chat?panel=creations", Icon: Images, keywords: ["library", "artifacts"] },
    // The org chart was a view of this panel, so its search words point here.
    { id: "go-agents", label: "Agents", path: "/chat?panel=teams-agents", Icon: Bot, keywords: ["bot", "roster", "org chart", "network", "hierarchy", "team"] },
    { id: "go-scheduled", label: "Scheduled", path: "/chat?panel=activity&section=scheduled", Icon: Clock, keywords: ["cron", "schedule", "routines"] },
    { id: "go-settings", label: "Settings", path: "/chat?panel=settings", Icon: Settings },
    { id: "go-business", label: "Business", path: "/chat?panel=settings&section=business", Icon: Briefcase, keywords: ["spend", "usage", "cost", "costs", "billing", "credits", "payments", "customers", "products", "revenue", "money"] },
    // Approvals is an admin's row in Settings (ADMIN_ONLY_PANELS): never offered to anyone else.
    ...(admin ? [{ id: "go-approvals", label: "Approvals", path: "/chat?panel=settings&section=approvals", Icon: ShieldCheck, keywords: ["approve", "ask", "auto", "permissions"] }] : []),
  ];
}

export function filterNavActions(query: string, admin = true): CommandPaletteAction[] {
  const actions = navActions(admin);
  const q = query.trim().toLowerCase();
  if (!q) return actions;
  return actions.filter((action) => {
    if (action.label.toLowerCase().includes(q)) return true;
    return (action.keywords ?? []).some((kw) => kw.toLowerCase().includes(q));
  });
}
