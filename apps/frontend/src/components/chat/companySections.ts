// The sidebar IA as data, lifted out of CompanyNav.tsx so the ⌘K palette can
// derive its nav actions from the same declaration without an import cycle
// (CompanyNav → InlineTeamsPanel → CommandPaletteProvider → CommandPalette →
// commandPaletteActions). Panel keys are the literal `teams-*` grammar
// (`TEAMS_PANEL_PREFIX` + list key) — importing the prefix constant from
// InlineTeamsPanel would re-enter that cycle; CompanyNav's no-dead-ends test
// resolves every key through panelRegistry, so a typo fails there.

import {
  LayoutDashboard,
  Inbox,
  ListTodo,
  FolderOpen,
  Target,
  Repeat,
  Images,
  Search,
  Plug,
  CreditCard,
  Send,
  SearchCheck,
  Megaphone,
  Layers,
  Landmark,
  Globe,
  ShieldCheck,
  FileSignature,
  Users2,
} from "lucide-react";
import type { ComponentType, SVGProps } from "react";

// One icon prop type for every consumer of the section list (sidebar renders
// with `size`, the ⌘K palette with className/style) — lucide icons satisfy it.
export type CompanyNavIcon = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

export type CompanySectionItem = {
  panel: string;
  label: string;
  Icon: CompanyNavIcon;
  /**
   * The tab exists only to show what the Stripe Projects rail provisioned.
   * (Accounts is NOT marked: it also hosts the company Stripe card, the
   * connections list, the capability guardrails and the revoke-all switch.)
   * Hidden while that rail is off (`provisioning_enabled` on
   * /company/stripe/status) — a tab whose only possible content is
   * "Nothing provisioned yet" is not a tab.
   */
  provisioning?: true;
  /**
   * A shell: the tab renders, but the capability behind it cannot do anything
   * for an owner yet, so offering it is a promise we do not keep. Hidden from
   * the sidebar AND the palette until its rail is live; the panel key stays
   * registered, so a saved link, a bookmark or an agent's URL still resolves.
   * Each one carries a comment naming exactly what unblocks it — when that
   * lands, delete the flag, not the tab.
   */
  comingSoon?: true;
};

// Spec §5.1 IA. An item appears here ONLY when its tab is live;
// the Growth/Operations groups arrive with their tabs (P1+). Money is one
// item (spec §3 ledger page) — Payments/Products/Customers/Spend are sections
// of it, not sidebar peers.
// Every panel renders inline via InlineTeamsPanel (TeamsPanelRouter maps each
// key). `agents` and `settings` are intentionally excluded — agents are the
// roster, settings is the Settings home.
export const COMPANY_SECTIONS: { label: string | null; items: CompanySectionItem[] }[] = [
  {
    label: null,
    items: [
      { panel: "teams-dashboard", label: "Home", Icon: LayoutDashboard },
      { panel: "teams-search", label: "Search", Icon: Search },
      { panel: "teams-inbox", label: "Inbox", Icon: Inbox },
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
  {
    // Spec §5.1 Growth. Shells state their exit (connect accounts, or a
    // prompt Friday can act on); Leads is a real read. Content and Blog wait
    // for their rails and are deliberately absent rather than shown as cards
    // with no exit.
    label: "Growth",
    items: [
      // Unblocked by: a post record to schedule against — which waits on Meta
      // App Review and a BYO X app. Scheduling itself already exists twice.
      { panel: "teams-publish", label: "Publish", Icon: Send, comingSoon: true },
      // Unblocked by: an Automations preset plus a Library report; there is no
      // crawl behind this tab today.
      { panel: "teams-seo", label: "SEO & AEO", Icon: SearchCheck, comingSoon: true },
      // Unblocked by: an ad account connection. Nothing to read without one.
      { panel: "teams-ads", label: "Ads", Icon: Megaphone, comingSoon: true },
      // The first Growth tab with a real read behind it — the owner's pasted
      // Apollo key, held backend-side.
      { panel: "teams-leads", label: "Leads", Icon: Users2 },
    ],
  },
  {
    label: "Company",
    items: [
      // The org chart moved INSIDE the Agents panel as its second view — same
      // roster, drawn as a tree. `teams-org-chart` still resolves and opens
      // Agents on the chart (RETIRED_PANEL_ALIASES).
      // What the company has accounts with, what agents set up on its behalf,
      // and the kill switch. Sits in Company, not Settings, because it is the
      // page an owner opens the first time an agent does something surprising.
      { panel: "teams-accounts", label: "Accounts", Icon: Plug },
      // Unblocked by: an incorporation/registered-agent partner. Today the tab
      // can only describe paperwork the platform does not hold.
      { panel: "teams-identity", label: "Identity", Icon: Landmark, comingSoon: true },
      { panel: "teams-domain-site", label: "Domain & Site", Icon: Globe, provisioning: true },
      // Unblocked by: a compliance partner rail.
      { panel: "teams-compliance", label: "Compliance", Icon: ShieldCheck, comingSoon: true },
      // Unblocked by: nothing external — it becomes a Library filter once the
      // Library grows document types.
      { panel: "teams-contracts", label: "Contracts", Icon: FileSignature, comingSoon: true },
    ],
  },
];

/**
 * The sections a given deployment actually offers. Two filters, both about
 * capability rather than taste:
 *
 * - the provisioning rail: while it is off, the tabs that only render its
 *   output are dropped;
 * - `comingSoon`: a shell whose capability is not live. A rail of tabs that
 *   each say "nothing here yet" teaches an owner the product is empty, so the
 *   shells wait outside until they can answer something.
 *
 * Both drop from the sidebar AND the command palette (each calls this, so they
 * cannot disagree). Direct URLs still resolve — the panel registry is
 * untouched — they are simply not offered.
 */
export function visibleCompanySections(
  opts: { provisioningEnabled: boolean },
): { label: string | null; items: CompanySectionItem[] }[] {
  return COMPANY_SECTIONS.map((section) => ({
    ...section,
    items: section.items.filter(
      (item) =>
        !item.comingSoon && (opts.provisioningEnabled || !item.provisioning),
    ),
  })).filter((section) => section.items.length > 0);
}
