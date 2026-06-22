"use client";

import {
  NAV_ITEMS,
  ADMIN_ONLY_PANELS,
  useIsControlAdmin,
} from "@/components/control/ControlSidebar";

// The global control panels surfaced as a unified-nav "System" section,
// mirroring CompanyNav's structure/styling. Items, labels, icons, and admin
// gating are reused from ControlSidebar (DRY) — not hand-duplicated. `agents`
// is excluded here because the per-agent inspector now covers per-agent
// management. Each button emits the PLAIN panel key (not teams--prefixed), so
// onPanelChange routes through ControlPanelRouter exactly as ControlSidebar
// does today.
const SYSTEM_ITEMS = NAV_ITEMS.filter((item) => item.key !== "agents");

export function SystemNav({
  activePanel,
  onPanelChange,
}: {
  activePanel?: string;
  onPanelChange?: (panel: string) => void;
}): React.ReactElement {
  const isOrgAdmin = useIsControlAdmin();

  return (
    // `min-h-0 overflow-y-auto`: mirrors CompanyNav — as a flex child of the
    // fixed-height `.cream-sidebar` (which clips overflow), the expanded nav
    // must be able to shrink below its content and scroll, or its lower items
    // + the Marketplace/footer below it would be clipped on short viewports.
    <nav
      className="system-nav px-2 py-1 min-h-0 overflow-y-auto"
      aria-label="System"
    >
      <div className="agent-section-label">
        <span>System</span>
      </div>
      {SYSTEM_ITEMS.map(({ key, label, icon: Icon }) => {
        if (ADMIN_ONLY_PANELS.has(key) && !isOrgAdmin) return null;
        const active = activePanel === key;
        return (
          <button
            key={key}
            type="button"
            onClick={() => onPanelChange?.(key)}
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
