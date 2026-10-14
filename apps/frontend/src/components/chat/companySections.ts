// The sidebar IA as data, lifted out of CompanyNav.tsx so the ⌘K palette can
// derive its nav actions from the same declaration without an import cycle
// (CompanyNav → InlineTeamsPanel → CommandPaletteProvider → CommandPalette →
// commandPaletteActions). Panel keys are the literal `teams-*` grammar
// (`TEAMS_PANEL_PREFIX` + list key) — importing the prefix constant from
// InlineTeamsPanel would re-enter that cycle; CompanyNav's no-dead-ends test
// resolves every key through panelRegistry, so a typo fails there.

import {
  Bell,
  Inbox,
  ListTodo,
  Mail,
  FolderOpen,
  Target,
  Repeat,
  Images,
  Search,
  CreditCard,
  Layers,
} from "lucide-react";
import type { ComponentType, SVGProps } from "react";

// One icon prop type for every consumer of the section list (sidebar renders
// with `size`, the ⌘K palette with className/style) — lucide icons satisfy it.
export type CompanyNavIcon = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

export type CompanySectionItem = {
  panel: string;
  label: string;
  Icon: CompanyNavIcon;
};

// An item appears here ONLY when its tab is live. Money is one item
// (Payments/Products/Customers/Spend are sections of it, not sidebar peers).
// Every panel renders inline via InlineTeamsPanel (TeamsPanelRouter maps each
// key). `agents` and `settings` are intentionally excluded — agents are the
// roster, settings is the Settings home.
export const COMPANY_SECTIONS: { label: string | null; items: CompanySectionItem[] }[] = [
  {
    label: null,
    items: [
      // Home was retired into Activity (RETIRED_PANEL_ALIASES), so Activity leads.
      { panel: "activity", label: "Activity", Icon: Bell },
      { panel: "teams-search", label: "Search", Icon: Search },
      { panel: "teams-inbox", label: "Inbox", Icon: Inbox },
      // The agents' shared mailbox. Not a teams-* key: its own panel, on the
      // desktop rail (so the desktop "More" list skips it, like Tasks).
      { panel: "mail", label: "Mail", Icon: Mail },
    ],
  },
  {
    // One ledger page (spec §3): what landed this month, then Payments,
    // Customers, What you sell and Agent spend as sections. The four old keys
    // still resolve — they open this page at their section.
    // No caption: a one-item group's item IS the caption.
    label: null,
    items: [{ panel: "teams-money", label: "Money", Icon: CreditCard }],
  },
  {
    // Order matches stock Paperclip's Work section (Sidebar.tsx @
    // v2026.916.1-isol8.2): Tasks, Projects, Routines, Artifacts, then Goals.
    // Activity folded into Inbox's "Recently" band (still RETIRED_PANEL_ALIASES).
    label: "Work",
    items: [
      { panel: "teams-issues", label: "Tasks", Icon: ListTodo },
      { panel: "teams-projects", label: "Projects", Icon: FolderOpen },
      { panel: "teams-routines", label: "Automations", Icon: Repeat },
      // Library is the spec's noun for stock's Artifacts gallery; the panel
      // key stays `teams-artifacts` (mobile builds these URLs as strings). It
      // is also a top-level destination on the workspace rail.
      { panel: "teams-artifacts", label: "Library", Icon: Images },
      { panel: "teams-goals", label: "Goals", Icon: Target },
      // Stock Paperclip's Cases page (same Layers icon as its sidebar). It
      // replaced the Support placeholder; `teams-support` aliases to it.
      { panel: "teams-cases", label: "Cases", Icon: Layers },
    ],
  },
];
