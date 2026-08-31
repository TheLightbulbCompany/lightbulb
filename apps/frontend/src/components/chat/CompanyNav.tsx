"use client";

// The section declaration lives in companySections.ts (data-only) so the ⌘K
// palette can derive from it without an import cycle; re-exported here as the
// canonical home consumers import.
import { COMPANY_SECTIONS } from "./companySections";

export { COMPANY_SECTIONS };

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
                    ? "bg-[var(--cream)] text-[var(--midnight)]"
                    : "text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
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
