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
  Repeat,
  Target,
  FolderKanban,
  Network,
  History,
  Images,
  Search,
  BookOpen,
  Plug,
  Wallet,
  CreditCard,
  Package,
  Users,
  Send,
  SearchCheck,
  Megaphone,
  LifeBuoy,
  Landmark,
  Globe,
  ShieldCheck,
  FileSignature,
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

// Spec §5.1 IA. An item appears here ONLY when its tab is live;
// the Growth/Operations groups arrive with their tabs (P1+), and Money holds
// only Spend until Payments/Products/Customers have a Connect binding and a
// list route to read.
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
    // Spec §5.1 order: the three Stripe reads, then what the agents cost.
    // Products is its own tab, not a section of Payments — they are three peer
    // reads of one connected account, and a section would have made one a
    // second-class citizen of another's tab.
    label: "Money",
    items: [
      { panel: "teams-payments", label: "Payments", Icon: CreditCard },
      { panel: "teams-products", label: "Products", Icon: Package },
      { panel: "teams-customers", label: "Customers", Icon: Users },
      { panel: "teams-spend", label: "Spend", Icon: Wallet },
    ],
  },
  {
    label: "Work",
    items: [
      { panel: "teams-issues", label: "Tasks", Icon: ListTodo },
      { panel: "teams-routines", label: "Automations", Icon: Repeat },
      { panel: "teams-goals", label: "Goals", Icon: Target },
      { panel: "teams-projects", label: "Projects", Icon: FolderKanban },
      // Support sits with Work (spec's "Operations"), not Company: it is a
      // thing the company DOES, not a thing the company IS.
      { panel: "teams-support", label: "Support", Icon: LifeBuoy },
    ],
  },
  {
    // Spec §5.1 Growth. All shells today — each states its exit (connect
    // accounts, or a prompt Friday can act on). Content, Leads and Blog wait
    // for their rails and are deliberately absent rather than shown as cards
    // with no exit.
    label: "Growth",
    items: [
      { panel: "teams-publish", label: "Publish", Icon: Send },
      { panel: "teams-seo", label: "SEO & AEO", Icon: SearchCheck },
      { panel: "teams-ads", label: "Ads", Icon: Megaphone },
    ],
  },
  {
    label: "Knowledge",
    items: [
      // Brain first: what the company knows is read before what it produced.
      { panel: "teams-brain", label: "Brain", Icon: BookOpen },
      // Library is the spec's noun for the work-products gallery; the panel
      // key stays `teams-artifacts` (mobile builds these URLs as strings).
      { panel: "teams-artifacts", label: "Library", Icon: Images },
    ],
  },
  {
    label: "Company",
    items: [
      { panel: "teams-org-chart", label: "Org chart", Icon: Network },
      // What the company has accounts with, what agents set up on its behalf,
      // and the kill switch. Sits in Company, not Settings, because it is the
      // page an owner opens the first time an agent does something surprising.
      { panel: "teams-accounts", label: "Accounts", Icon: Plug },
      { panel: "teams-identity", label: "Identity", Icon: Landmark },
      { panel: "teams-domain-site", label: "Domain & Site", Icon: Globe },
      { panel: "teams-compliance", label: "Compliance", Icon: ShieldCheck },
      { panel: "teams-contracts", label: "Contracts", Icon: FileSignature },
      { panel: "teams-activity", label: "Activity", Icon: History },
    ],
  },
];
