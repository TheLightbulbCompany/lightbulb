"use client";

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
} from "lucide-react";
import { TEAMS_PANEL_PREFIX } from "./InlineTeamsPanel";

type Item = {
  panel: string;
  label: string;
  Icon: React.ComponentType<{ size?: number }>;
};

// Grouped to mirror upstream Paperclip's sidebar: an unlabeled top block, then
// "Work", then "Company". Every panel renders inline via InlineTeamsPanel
// (TeamsPanelRouter maps each key). `agents` and `settings` are intentionally
// excluded — agents are the roster, settings is the Settings home.
const COMPANY_SECTIONS: { label: string | null; items: Item[] }[] = [
  {
    label: null,
    items: [
      { panel: `${TEAMS_PANEL_PREFIX}dashboard`, label: "Dashboard", Icon: LayoutDashboard },
      { panel: `${TEAMS_PANEL_PREFIX}search`, label: "Search", Icon: Search },
      { panel: `${TEAMS_PANEL_PREFIX}inbox`, label: "Inbox", Icon: Inbox },
    ],
  },
  {
    label: "Work",
    items: [
      { panel: `${TEAMS_PANEL_PREFIX}issues`, label: "Tasks", Icon: ListTodo },
      { panel: `${TEAMS_PANEL_PREFIX}routines`, label: "Automations", Icon: Repeat },
      { panel: `${TEAMS_PANEL_PREFIX}goals`, label: "Goals", Icon: Target },
      { panel: `${TEAMS_PANEL_PREFIX}projects`, label: "Projects", Icon: FolderKanban },
    ],
  },
  {
    label: "Company",
    items: [
      { panel: `${TEAMS_PANEL_PREFIX}org-chart`, label: "Org chart", Icon: Network },
      { panel: `${TEAMS_PANEL_PREFIX}artifacts`, label: "Artifacts", Icon: Images },
      { panel: `${TEAMS_PANEL_PREFIX}activity`, label: "Activity", Icon: History },
    ],
  },
];

export function CompanyNav({
  activePanel,
  onPanelChange,
}: {
  activePanel?: string;
  onPanelChange?: (panel: string) => void;
}): React.ReactElement {
  return (
    // No own scroll region: this nav lives inside the shared `.sidebar-scroll`
    // wrapper, which owns the single scroll for roster + CompanyNav + Gallery.
    // Giving it `overflow-y-auto` here would re-introduce the nested scroll that
    // collapsed the roster's height.
    <nav
      className="company-nav px-2 py-1"
      aria-label="Company"
    >
      {COMPANY_SECTIONS.map((section, i) => (
        <div key={section.label ?? `top-${i}`}>
          {section.label ? (
            <div className="agent-section-label">
              <span>{section.label}</span>
            </div>
          ) : null}
          {section.items.map(({ panel, label, Icon }) => {
            const active = activePanel === panel;
            return (
              <button
                key={panel}
                type="button"
                onClick={() => onPanelChange?.(panel)}
                className={`flex w-full items-center gap-2.5 rounded-full px-3 py-2 text-sm transition-colors ${
                  active
                    ? "bg-[var(--cream)] text-[var(--forest)]"
                    : "text-[var(--ink-soft)] hover:bg-[var(--forest-tint)] hover:text-[var(--forest)]"
                }`}
              >
                <Icon size={16} />
                <span>{label}</span>
              </button>
            );
          })}
        </div>
      ))}
    </nav>
  );
}
