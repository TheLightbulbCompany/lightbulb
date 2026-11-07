// Ported from upstream Paperclip's CommandPalette
// (paperclip/ui/src/components/CommandPalette.tsx) (MIT, (c) 2025 Paperclip AI).
// SWR read for the palette's Agents group.

import { useMemo } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { CompanyAgent } from "@/components/teams/shared/types";

export interface FilteredCommandResults {
  agents: CompanyAgent[];
}

const RESULT_LIMIT = 10;

function normalizeArray<T>(data: unknown, key: string): T[] {
  if (!data) return [];
  if (Array.isArray(data)) return data as T[];
  const obj = data as Record<string, unknown>;
  if (Array.isArray(obj[key])) return obj[key] as T[];
  if (Array.isArray(obj.items)) return obj.items as T[];
  return [];
}

function matches(query: string, ...fields: (string | null | undefined)[]): boolean {
  if (!query) return true;
  const q = query.toLowerCase();
  return fields.some((f) => f && f.toLowerCase().includes(q));
}

export function useFilteredCommandResults(query: string, enabled: boolean): FilteredCommandResults {
  const { read } = useTeamsApi();
  // Always read; SWR de-dupes by key.
  const agentsData = read<unknown>("/agents");

  return useMemo(() => {
    if (!enabled) return { agents: [] };
    const agents = normalizeArray<CompanyAgent>(agentsData.data, "agents")
      .filter((a) => matches(query, a.name))
      .slice(0, RESULT_LIMIT);
    return { agents };
  }, [agentsData.data, query, enabled]);
}
