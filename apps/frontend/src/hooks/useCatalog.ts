import useSWR from "swr";
import { useCallback, useMemo } from "react";

import { useApi } from "@/lib/api";

export interface CatalogAgent {
  slug: string;
  name: string;
  version: number;
  emoji: string;
  vibe: string;
  description: string;
  suggested_model: string;
  suggested_channels: string[];
  required_skills: string[];
  required_plugins: string[];
}

export interface DeployedAgent {
  agent_id: string;
  template_slug: string;
  template_version: number;
}

export interface DeployResult {
  slug: string;
  version: number;
  agent_id: string;
  name: string;
  skills_added: string[];
  plugins_enabled: string[];
}

export function useCatalog() {
  const api = useApi();

  const { data: catalogData, mutate: mutateCatalog } = useSWR<{ agents: CatalogAgent[] }>(
    "/catalog",
    () => api.get("/catalog") as Promise<{ agents: CatalogAgent[] }>,
  );
  const { data: deployedData, mutate: mutateDeployed } = useSWR<{ deployed: DeployedAgent[] }>(
    "/catalog/deployed",
    () => api.get("/catalog/deployed") as Promise<{ deployed: DeployedAgent[] }>,
  );

  const deployedSlugs = useMemo(
    () => new Set((deployedData?.deployed ?? []).map((d) => d.template_slug)),
    [deployedData],
  );

  const visibleAgents = useMemo(
    () => (catalogData?.agents ?? []).filter((a) => !deployedSlugs.has(a.slug)),
    [catalogData, deployedSlugs],
  );

  const deploy = useCallback(
    async (slug: string): Promise<DeployResult> => {
      const result = (await api.post("/catalog/deploy", { slug })) as DeployResult;
      await Promise.all([mutateCatalog(), mutateDeployed()]);
      return result;
    },
    [api, mutateCatalog, mutateDeployed],
  );

  return {
    agents: visibleAgents,
    isLoading: !catalogData || !deployedData,
    deploy,
    refresh: () => Promise.all([mutateCatalog(), mutateDeployed()]),
  };
}
