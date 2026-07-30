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
  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Hire ${listing.name}`}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm rounded-2xl border border-[#e0dbd0] bg-[#fbf8f1] p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="font-editorial-serif text-lg text-[#1a1a1a]">
          Hire {listing.name}
        </h2>
        <p className="mt-1 text-[13px] text-[#5b5b5b]">
          Where should this agent sit in your team?
        </p>

        <label className="mt-4 block text-[11.5px] tracking-widest uppercase font-bold text-[#5b5b5b]">
          Reports to
        </label>
        <select
          data-testid="reports-to-select"
          disabled={isLoading || isHiring}
          value={selected ?? ""}
          onChange={(e) => setPicked(e.target.value === "" ? null : e.target.value)}
          className="mt-1.5 w-full rounded-lg border border-[#d8cfbf] bg-white px-3 py-2 text-[13px] text-[#1a1a1a] disabled:opacity-60"
        >
          <option value="">No manager (top level)</option>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
              {a.role === "ceo" ? " — CEO" : a.title ? ` — ${a.title}` : ""}
            </option>
          ))}
        </select>
        {error && (
          <p className="mt-1.5 text-[12px] text-[#8a3a2a]">
            Couldn&apos;t load your team — the agent will be added at the top
            level. You can reassign it later in Teams.
          </p>
        )}

        <div className="mt-5 flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={isHiring}
            className="px-3.5 py-1.5 rounded-full text-[12.5px] font-semibold text-[#5b5b5b] hover:text-[#1a1a1a] disabled:opacity-60"
          >
            Cancel
          </button>
          <button
            type="button"
            data-testid="confirm-hire"
            onClick={() => onConfirm(selected)}
            disabled={isHiring || isLoading}
            className="bg-[#06402b] text-white px-4 py-1.5 rounded-full text-[12.5px] font-semibold hover:bg-[#0a5a3d] disabled:opacity-60"
          >
            {isHiring ? "Hiring…" : "Hire"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
