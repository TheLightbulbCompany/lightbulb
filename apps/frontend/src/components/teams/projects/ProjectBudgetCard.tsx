// apps/frontend/src/components/teams/projects/ProjectBudgetCard.tsx

// Ported from upstream Paperclip's BudgetPolicyCard.tsx save affordance
// (paperclip/ui/src/components/BudgetPolicyCard.tsx) (MIT, © 2025 Paperclip AI),
// reskinned onto the editorial cream/forest palette and trimmed to the data the
// slim Teams BFF carries: the editable monthly budget cap (budgetMonthlyCents).
//
// Deferred vs upstream: observed-spend / utilization gauge / hard-stop status /
// pause banner all depend on the upstream `budgets/overview` API, which the
// Teams BFF does not proxy — so this v1 is the editable cap only. The cap drives
// nothing on its own here; spend tracking is a clean follow-up once the BFF
// exposes a per-project spend read.

"use client";

import { useEffect, useState } from "react";
import { Wallet } from "lucide-react";
import { EditorialButton, EditorialCard } from "@/components/teams/shell";
import { formatCents } from "@/components/teams/shared/lib/money";

export interface ProjectBudgetCardProps {
  /** Current monthly budget cap in cents (upstream `budgetMonthlyCents`). */
  budgetMonthlyCents: number;
  isSaving?: boolean;
  onSave: (amountCents: number) => void;
}

function centsInputValue(value: number): string {
  return (value / 100).toFixed(2);
}

/** Parse a dollar string → cents. Returns null for invalid / negative input. */
function parseDollarInput(value: string): number | null {
  const normalized = value.trim();
  if (normalized.length === 0) return 0;
  const parsed = Number(normalized);
  if (!Number.isFinite(parsed) || parsed < 0) return null;
  return Math.round(parsed * 100);
}

export function ProjectBudgetCard({
  budgetMonthlyCents,
  isSaving,
  onSave,
}: ProjectBudgetCardProps) {
  const [draft, setDraft] = useState(centsInputValue(budgetMonthlyCents));

  // Re-sync when the saved value changes externally (mutation resolves).
  useEffect(() => {
    setDraft(centsInputValue(budgetMonthlyCents));
  }, [budgetMonthlyCents]);

  const parsed = parseDollarInput(draft);
  const canSave =
    typeof parsed === "number" && parsed !== budgetMonthlyCents && !isSaving;
  const hasCap = budgetMonthlyCents > 0;

  return (
    <EditorialCard className="flex flex-col gap-5 p-5 max-w-2xl">
      <div className="flex items-start justify-between gap-4">
        <div>
          <span
            className="text-[11px] uppercase tracking-[0.18em]"
            style={{ color: "var(--mute)" }}
          >
            Monthly budget
          </span>
          <div
            className="mt-2 font-editorial-serif text-3xl tabular-nums"
            style={{ color: "var(--ink)" }}
          >
            {hasCap ? formatCents(budgetMonthlyCents) : "No cap"}
          </div>
          <p className="mt-1 text-xs" style={{ color: "var(--mute)" }}>
            {hasCap
              ? "Per-month spending cap for this project."
              : "Set a per-month spending cap for this project."}
          </p>
        </div>
        <Wallet
          className="h-5 w-5 shrink-0"
          style={{ color: "var(--mute)" }}
          aria-hidden="true"
        />
      </div>

      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <label className="min-w-0 flex-1">
          <span
            className="text-[11px] uppercase tracking-[0.18em]"
            style={{ color: "var(--mute)" }}
          >
            Budget (USD)
          </span>
          <div className="mt-2 flex items-center gap-2">
            <span style={{ color: "var(--mute)" }}>$</span>
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              inputMode="decimal"
              placeholder="0.00"
              aria-label="Monthly budget in US dollars"
              className="w-full rounded border px-3 py-2 text-[var(--ink)] outline-none"
              style={{
                borderColor: "var(--edge)",
                background: "var(--cream)",
              }}
            />
          </div>
        </label>
        <EditorialButton
          variant="primary"
          disabled={!canSave || parsed === null}
          onClick={() => {
            if (typeof parsed === "number") onSave(parsed);
          }}
        >
          {isSaving ? "Saving…" : hasCap ? "Update budget" : "Set budget"}
        </EditorialButton>
      </div>

      {parsed === null && (
        <p className="text-xs" style={{ color: "var(--danger)" }}>
          Enter a valid non-negative dollar amount.
        </p>
      )}

      <p className="text-xs italic" style={{ color: "var(--mute)" }}>
        Spend tracking against this cap is coming soon.
      </p>
    </EditorialCard>
  );
}
