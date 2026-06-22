"use client";

import { LayoutDashboard, History } from "lucide-react";
import { TEAMS_PANEL_PREFIX } from "./InlineTeamsPanel";

// Phase A starts with the two read-only panels. Later phases append the
// rest (issues, routines, goals, …) to this array.
const COMPANY_ITEMS: {
  panel: string;
  label: string;
  Icon: React.ComponentType<{ size?: number }>;
}[] = [
  { panel: `${TEAMS_PANEL_PREFIX}dashboard`, label: "Dashboard", Icon: LayoutDashboard },
  { panel: `${TEAMS_PANEL_PREFIX}activity`, label: "Activity", Icon: History },
];

export function CompanyNav({
  activePanel,
  onPanelChange,
}: {
  activePanel?: string;
  onPanelChange?: (panel: string) => void;
}): React.ReactElement {
  return (
    <nav className="company-nav px-2 py-1" aria-label="Company">
      <div className="agent-section-label">
        <span>Company</span>
      </div>
      {COMPANY_ITEMS.map(({ panel, label, Icon }) => {
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
    </nav>
  );
}
