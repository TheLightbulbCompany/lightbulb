"use client";

// Paperclip-backed budgets: spend vs monthly ceiling for the workspace and each
// agent. Any member can read; only an org admin gets the set-budget control
// (the BFF re-checks). Amounts are monthly cents upstream, dollars here.
// Independent of the retired useBudgets / PUT /billing/budgets surface.

import { useState } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { useBilling } from "@/hooks/useBilling";
import { PurchasesSection } from "./PurchasesSection";

interface Policy {
  scopeType: string;
  scopeId: string;
  scopeName: string;
  amount: number;
  observedAmount: number;
  remainingAmount: number;
  status?: string;
}

interface Row {
  scopeType: "company" | "agent";
  id: string;
  name: string;
  amount: number;
  observed: number;
}

// Budgets meter Lightbulb credits (the openrouter-gate). ChatGPT and BYO-key
// owners never pass the gate, so nothing is metered: the editor is off for them.
const usesOwnProvider = (providerChoice: string | null | undefined) =>
  providerChoice === "chatgpt_oauth" || providerChoice === "byo_key";

const usd = (cents: number) => `$${(cents / 100).toFixed(2)}`;

function BudgetRow({ row, canEdit, onSave }: { row: Row; canEdit: boolean; onSave: (row: Row, cents: number) => Promise<void> }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const hasLimit = row.amount > 0;
  const over = hasLimit && row.observed >= row.amount;
  const cents = Math.round(parseFloat(value) * 100);
  const valid = value.trim() !== "" && Number.isFinite(cents) && cents >= 0;

  return (
    <li className="flex flex-wrap items-center gap-3 px-4 py-3 border-b border-[var(--plane-edge)]">
      <div className="min-w-[140px] flex-1">
        <div className="text-sm font-medium text-[var(--fg)]">{row.name}</div>
        <div className="text-xs text-[var(--fg-soft)]">
          {hasLimit ? `${usd(row.observed)} of ${usd(row.amount)}` : `${usd(row.observed)} spent`}
        </div>
      </div>
      <div className="text-xs" style={{ color: over ? "var(--alert)" : "var(--fg-soft)" }}>
        {over ? "Limit reached" : hasLimit ? `${usd(Math.max(0, row.amount - row.observed))} left` : "No limit"}
      </div>
      {canEdit && (
        <form
          className="flex items-center gap-2"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!valid || busy) return;
            setBusy(true);
            try {
              await onSave(row, cents);
              setValue("");
            } finally {
              setBusy(false);
            }
          }}
        >
          <input
            type="number"
            min="0"
            step="0.01"
            inputMode="decimal"
            placeholder="$ / month"
            aria-label={`Monthly budget for ${row.name}`}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            className="w-28 rounded-md border border-[var(--plane-edge)] bg-[var(--plane-raised)] px-2 py-1 text-sm text-[var(--fg)]"
          />
          <button
            type="submit"
            disabled={!valid || busy}
            aria-label={`Save budget for ${row.name}`}
            className="rounded-md border border-[var(--plane-edge)] px-2 py-1 text-sm text-[var(--fg)] disabled:opacity-50"
          >
            Save
          </button>
        </form>
      )}
    </li>
  );
}

export function BudgetsPanel() {
  const isAdmin = useIsWorkspaceAdmin();
  const { account } = useBilling();
  if (usesOwnProvider(account?.provider_choice)) {
    return (
      <div className="flex flex-col gap-3">
        <p className="text-sm text-[var(--fg-soft)]">
          Budgets track Lightbulb credits. Your workspace uses its own model provider, so there is nothing to track.
        </p>
        <PurchasesSection canEdit={isAdmin} />
      </div>
    );
  }
  return <BudgetsEditor isAdmin={isAdmin} />;
}

function BudgetsEditor({ isAdmin }: { isAdmin: boolean }) {
  const { read, patch } = useTeamsApi();
  const overview = read<{ policies?: Policy[] }>("/budgets/overview");
  const agents = read<{ agents?: { id: string; name: string; spentMonthlyCents?: number }[] }>("/agents");
  const [error, setError] = useState<string | null>(null);

  const policies = overview.data?.policies ?? [];
  const byAgent = new Map(policies.filter((p) => p.scopeType === "agent").map((p) => [p.scopeId, p]));
  const company = policies.find((p) => p.scopeType === "company");

  const rows: Row[] = [];
  if (company) {
    rows.push({ scopeType: "company", id: company.scopeId, name: company.scopeName, amount: company.amount, observed: company.observedAmount });
  }
  for (const a of agents.data?.agents ?? []) {
    const p = byAgent.get(a.id);
    rows.push({ scopeType: "agent", id: a.id, name: a.name, amount: p?.amount ?? 0, observed: p?.observedAmount ?? a.spentMonthlyCents ?? 0 });
  }

  async function save(row: Row, cents: number) {
    setError(null);
    try {
      await patch(row.scopeType === "company" ? "/budgets" : `/agents/${row.id}/budget`, { budgetMonthlyCents: cents });
      await overview.mutate();
    } catch {
      setError("Could not save the budget.");
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-[var(--fg-soft)]">Monthly spend against each budget. Set a budget to $0 to remove it.</p>
      {error && <p role="alert" className="text-sm" style={{ color: "var(--alert)" }}>{error}</p>}
      {overview.isLoading ? (
        <p className="text-sm text-[var(--fg-mute)]">Loading…</p>
      ) : (
        <ul className="rounded-lg border border-[var(--plane-edge)] bg-[var(--plane-raised)]">
          {rows.map((row) => (
            <BudgetRow key={`${row.scopeType}:${row.id}`} row={row} canEdit={isAdmin} onSave={save} />
          ))}
        </ul>
      )}
      <PurchasesSection canEdit={isAdmin} />
    </div>
  );
}
