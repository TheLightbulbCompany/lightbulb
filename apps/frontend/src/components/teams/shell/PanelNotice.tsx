import type { LucideIcon } from "lucide-react";
import type { MouseEvent, ReactNode } from "react";
import { cn } from "@/lib/utils";
import { EditorialButton } from "./EditorialButton";

/**
 * The notice a panel renders INSTEAD of its rows — while keeping its own
 * chrome. This is the difference from `PanelEmptyState`, which replaces the
 * whole panel with a centred icon: an owner who lands on an empty Payments tab
 * should still see that Payments is a table of charges with a date range, and
 * learn the page's shape before it has data. Use `PanelEmptyState` for a list
 * that is genuinely, healthily empty (no tasks yet); use this whenever the
 * emptiness has a REASON the owner needs told.
 *
 * ── WHY SIX STATES ────────────────────────────────────────────────────────
 * They are not a taxonomy invented for symmetry; each one exists because the
 * backend can distinguish it and the owner needs a different sentence:
 *
 *   zero        We can see the source and it is genuinely nothing.
 *   unbound     No source connected. `stripe_bound: false`.
 *   partial     Source connected but not finished — a bound Stripe account
 *               with `details_submitted: false` returns legitimately empty
 *               lists, and "finish setup" is a different instruction from
 *               "no payments yet". Without this the two are indistinguishable.
 *   unavailable We cannot see. A read failed, or the column is a known hole
 *               (`unavailable: ["followers","open_threads"]`). NEVER render
 *               this as 0 — a zero is a claim we have not earned.
 *   locked      The owner is BLOCKED, not empty: credits exhausted, plan
 *               lapsed. The exit is billing, never support.
 *   soon        Not built yet. States the value, so the map is legible before
 *               the rail exists.
 *
 * ── TONE IS LOAD-BEARING ──────────────────────────────────────────────────
 * `.interface-design/system.md`: "calm when healthy, loud only on incidents",
 * and "unknown / unreadable renders neutral, never ok". An unonboarded Stripe
 * account and an empty table are HEALTHY — they are the normal first hour of
 * every account. Only `locked` is an incident from the owner's side, so only
 * `locked` takes a state colour. Everything else is `--edge` + muted text.
 * Resist the urge to make `unbound` amber: it would make a working product
 * look broken to every new owner on their first visit.
 *
 * A state colour marks a border or text, never a fill (system.md), so the
 * accent here is a 2px left rule on the card ground.
 */
export type PanelNoticeState =
  | "zero"
  | "unbound"
  | "partial"
  | "unavailable"
  | "locked"
  | "soon";

export interface PanelNoticeProps {
  state: PanelNoticeState;
  /** The fact, in the owner's language. One sentence. */
  headline: ReactNode;
  /** Why, and what changes it. */
  supporting?: ReactNode;
  /** The named exit. Every notice should have one except `unavailable`,
   *  where there is genuinely nothing for the owner to do but wait. */
  action?: { label: ReactNode; onClick: (e: MouseEvent<HTMLElement>) => void };
  /** Renders the exit as a link instead (top-up flows, provider dashboards).
   *  `action.onClick` still fires, so an in-shell caller can preventDefault +
   *  pushState while `href` stays the native new-tab / middle-click fallback —
   *  the same `to` + `onClick` pair pattern used elsewhere for panel-card links. */
  href?: string;
  /**
   * A sentence the owner can hand to their agent, shown in italics above the
   * action — the "Add the support chat widget to my app, on every page."
   * pattern. Use where the agent can genuinely do the thing; a prompt that
   * produces "I can't do that yet" is worse than no prompt.
   */
  prompt?: string;
  icon?: LucideIcon;
  className?: string;
}

/** Only `locked` is an incident. Everything else stays neutral by design. */
function accentClass(state: PanelNoticeState): string {
  return state === "locked" ? "border-l-err" : "border-l-[var(--edge)]";
}

export function PanelNotice({
  state,
  headline,
  supporting,
  action,
  href,
  prompt,
  icon: Icon,
  className,
}: PanelNoticeProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      data-panel-notice={state}
      className={cn(
        "rounded-md border border-[var(--edge)] border-l-2 bg-card px-4 py-3",
        accentClass(state),
        className,
      )}
    >
      <div className="flex items-start gap-3">
        {Icon && (
          <Icon
            aria-hidden="true"
            size={16}
            strokeWidth={1.75}
            className={cn(
              "mt-0.5 shrink-0",
              state === "locked" ? "text-err" : "text-[var(--midnight-muted)]",
            )}
          />
        )}
        <div className="min-w-0 flex-1">
          <p
            className={cn(
              "text-ui",
              state === "locked" ? "text-err" : "text-[var(--midnight)]",
            )}
          >
            {headline}
          </p>
          {supporting && (
            <p className="mt-1 max-w-[60ch] text-xs text-[var(--midnight-muted)]">
              {supporting}
            </p>
          )}
          {prompt && (
            <p className="mt-2 font-editorial-serif text-xs italic text-[var(--midnight-soft)]">
              &ldquo;{prompt}&rdquo;
            </p>
          )}
          {(action || href) && (
            <div className="mt-2.5">
              {href ? (
                <a
                  href={href}
                  onClick={action?.onClick}
                  className="text-xs font-medium text-[var(--midnight)] underline"
                >
                  {action?.label ?? "Open"}
                </a>
              ) : (
                <EditorialButton size="sm" onClick={action!.onClick}>
                  {action!.label}
                </EditorialButton>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
