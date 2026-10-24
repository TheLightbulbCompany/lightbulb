import useSWR from "swr";
import { useApi } from "@/lib/api";

export interface ProjectBudget {
  ceiling_microcents: number;
  spent_microcents: number;
  month: string;
}

/** Per-project budgets (`GET /billing/budgets`, admin-only: pass `enabled=false` for others).
 *  `__default__` is the workspace bucket everything accrues to today. */
export function useBudgets(enabled: boolean) {
  const api = useApi();
  return useSWR<Record<string, ProjectBudget>>(
    enabled ? "/billing/budgets" : null,
    async () => ((await api.get("/billing/budgets")) as { budgets: Record<string, ProjectBudget> }).budgets,
    { dedupingInterval: 30000 },
  );
}
