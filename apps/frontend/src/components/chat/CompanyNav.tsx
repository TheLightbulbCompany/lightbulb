"use client";

// The section declaration lives in companySections.ts (data-only) so the ⌘K
// palette can derive from it without an import cycle; re-exported here as the
// canonical home consumers import.
import { COMPANY_SECTIONS, visibleCompanySections } from "./companySections";
import { useCompanyStripeStatus } from "@/components/settings/CompanyStripeSection";

export { COMPANY_SECTIONS };

export function CompanyNav({
  activePanel,
  onPanelChange,
  disabled = false,
  disabledTitle,
}: {
  activePanel?: string;
  onPanelChange?: (panel: string) => void;
  /**
   * Render the items as unavailable rather than enabled-and-inert.
   *
   * The desktop shell keeps this nav painted while the provisioning stepper
   * has swapped the dockview host out, and `openPanel` early-returns when the
   * dock's api is null — so every click was a silent no-op with no error, no
   * toast, nothing. The create-agent / new-task pills beside this nav already
   * disable themselves off the same `chatReady` signal; these items just never
   * got the same treatment. Opt-in, because ChatLayout (mobile) routes
   * `onPanelChange` somewhere that genuinely works.
   */
  disabled?: boolean;
  disabledTitle?: string;
}): React.ReactElement {
  // One SWR key shared with the Stripe card, so this costs no extra request.
  // Until the status has loaded the provisioning tabs are HIDDEN, not shown:
  // flashing a tab in and then out is worse than a late appearance.
  const { data: stripeStatus } = useCompanyStripeStatus();
  const sections = visibleCompanySections({
    provisioningEnabled: Boolean(stripeStatus?.provisioning_enabled),
  });
  return (
    // No own scroll region: this nav lives inside the shared `.sidebar-scroll`
    // wrapper, which owns the single scroll for roster + CompanyNav + Gallery.
    // Giving it `overflow-y-auto` here would re-introduce the nested scroll that
    // collapsed the roster's height.
    <nav
      className="company-nav px-2 py-1"
      aria-label="Company"
    >
      {sections.map((section, i) => (
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
                disabled={disabled}
                title={disabled ? disabledTitle : undefined}
                className={`flex w-full items-center gap-2.5 rounded-full px-3 py-2 text-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                  active
                    ? "bg-[var(--cream)] text-[var(--midnight)]"
                    : "text-[var(--midnight-soft)] enabled:hover:bg-[var(--cream-deep)] enabled:hover:text-[var(--midnight)]"
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
