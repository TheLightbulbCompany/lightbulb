"use client";

import { useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent } from "@/components/teams/shared/types";
import type { Listing } from "@/lib/marketplace/types";

/**
 * Org-placement picker shown when hiring a marketplace agent. Lets the buyer
 * choose which existing agent the new hire reports to (its manager in the
 * Paperclip org chart). Defaults to the CEO (Friday). "No manager" leaves it
 * unparented. The chosen id is passed back as `reports_to`.
 */
export function HireManagerDialog({
  listing,
  isHiring,
  onConfirm,
  onClose,
}: {
  listing: Listing;
  isHiring: boolean;
  onConfirm: (reportsTo: string | null) => void;
  onClose: () => void;
}) {
  const { read } = useTeamsApi();
  const { data, isLoading, error } = read<{ agents: CompanyAgent[] }>("/agents");
  const agents = useMemo(() => data?.agents ?? [], [data]);

  // Default the manager to the CEO (Friday). `undefined` = "not chosen yet" so
  // we can apply the CEO default once agents load without clobbering a user pick.
  const [picked, setPicked] = useState<string | null | undefined>(undefined);
  const ceo = agents.find((a) => a.role === "ceo");
  const selected = picked === undefined ? (ceo?.id ?? null) : picked;

  // Portalled to <body>: the caller renders this inside the listing card, and
  // that card carries `hover:-translate-y-0.5`. A non-none `translate` makes
  // the card a containing block for fixed-position descendants, so in-tree the
  // backdrop collapsed into the card — clipped, half-width, and flickering
  // (full-screen backdrop keeps the card hovered → collapse → pointer leaves
  // the card → un-hover → full-screen again, forever).
  //
  // z-[70] is load-bearing, not decorative. MarketplaceDrawer's <aside> is an
  // OPAQUE z-[60] panel covering min(920px,98vw) from the right edge. In-tree,
  // this dialog lived inside that panel's stacking context, so its z-index was
  // never compared against it; portalled to <body> it becomes a root-level
  // sibling, and z-50 painted the whole dialog UNDERNEATH the drawer —
  // invisible on a normal viewport. Keep above the drawer's 60, below
  // MarketplaceToast's z-[100].
  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Hire ${listing.name}`}
      className="fixed inset-0 z-[70] flex items-center justify-center bg-[var(--midnight)]/30 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm rounded-xl border border-[var(--edge)] bg-card p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="font-editorial-serif text-lg text-[var(--midnight)]">
          Hire {listing.name}
        </h2>
        <p className="mt-1 text-ui text-muted-foreground">
          Where should this agent sit in your team?
        </p>

        <label className="mt-4 block label-caps">
          Reports to
        </label>
        <select
          data-testid="reports-to-select"
          disabled={isLoading || isHiring}
          value={selected ?? ""}
          onChange={(e) => setPicked(e.target.value === "" ? null : e.target.value)}
          className="mt-1.5 w-full rounded-lg border border-[var(--edge)] bg-cream-deep px-3 py-2 text-ui text-[var(--midnight)] disabled:opacity-50"
        >
          <option value="">No manager (top level)</option>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
              {a.role === "ceo" ? " — Coordinator" : a.title ? ` — ${a.title}` : ""}
            </option>
          ))}
        </select>
        {error && (
          <p className="mt-1.5 text-xs text-[var(--err)]">
            Couldn&apos;t load your team — the agent will be added at the top
            level. You can reassign it later in Teams.
          </p>
        )}

        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={isHiring}
            className="px-3.5 py-1.5 rounded-full text-ui font-semibold text-muted-foreground hover:text-[var(--midnight)] disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="confirm-hire"
            onClick={() => onConfirm(selected)}
            disabled={isHiring || isLoading}
            className="bg-[var(--midnight)] text-[var(--cream)] px-4 py-1.5 rounded-full text-ui font-semibold hover:bg-[var(--midnight-soft)] disabled:opacity-50"
          >
            {isHiring ? "Hiring…" : "Hire"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
