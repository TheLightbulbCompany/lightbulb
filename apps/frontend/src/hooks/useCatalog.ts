import useSWR from "swr";
import { useCallback, useMemo } from "react";

import { useApi } from "@/lib/api";

/**
 * Source of the chat sidebar's "From the marketplace" gallery.
 *
 * Reads ``/marketplace/listings/featured`` (curator's published listings —
 * admin@isol8.co treated as a regular seller, no special flag) and
 * ``/marketplace/me/installed-templates`` (this user's installed templates,
 * derived from EFS .template sidecars), then renders the difference.
 *
 * Replaces the old admin-catalog ``/catalog`` endpoints. The hook + return
 * shape are unchanged so consumers (GallerySection, AgentDetailPanel) don't
 * need to change.
 */

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

interface MarketplaceListing {
  slug: string;
  name?: string;
  version?: number;
  description_md?: string;
  manifest_json?: {
    name?: string;
    description?: string;
    emoji?: string;
    vibe?: string;
    suggested_model?: string;
    suggested_channels?: string[];
    required_skills?: string[];
    required_plugins?: string[];
  };
}

function listingToCatalogAgent(li: MarketplaceListing): CatalogAgent {
  const m = li.manifest_json ?? {};
  return {
    slug: li.slug,
    name: m.name ?? li.name ?? li.slug,
    version: li.version ?? 1,
    emoji: m.emoji ?? "",
    vibe: m.vibe ?? "",
    description: m.description ?? li.description_md ?? "",
    suggested_model: m.suggested_model ?? "",
    suggested_channels: m.suggested_channels ?? [],
    required_skills: m.required_skills ?? [],
    required_plugins: m.required_plugins ?? [],
  };
}

export function useCatalog() {
  const api = useApi();

  const { data: featuredData, mutate: mutateFeatured } = useSWR<{ items: MarketplaceListing[] }>(
    "/marketplace/listings/featured",
    () => api.get("/marketplace/listings/featured") as Promise<{ items: MarketplaceListing[] }>,
  );
  const { data: installedData, mutate: mutateInstalled } = useSWR<{ installed: DeployedAgent[] }>(
    "/marketplace/me/installed-templates",
    () =>
      api.get("/marketplace/me/installed-templates") as Promise<{ installed: DeployedAgent[] }>,
  );

  const installedSlugs = useMemo(
    () => new Set((installedData?.installed ?? []).map((d) => d.template_slug)),
    [installedData],
  );

  const visibleAgents = useMemo(
    () =>
      (featuredData?.items ?? [])
        .map(listingToCatalogAgent)
        .filter((a) => !installedSlugs.has(a.slug)),
    [featuredData, installedSlugs],
  );

  const deploy = useCallback(
    async (slug: string): Promise<DeployResult> => {
      const result = (await api.post(
        `/marketplace/listings/${encodeURIComponent(slug)}/deploy`,
        {},
      )) as DeployResult;
      await Promise.all([mutateFeatured(), mutateInstalled()]);
      return result;
    },
    [api, mutateFeatured, mutateInstalled],
  );

  return {
    agents: visibleAgents,
    isLoading: !featuredData || !installedData,
    deploy,
    refresh: () => Promise.all([mutateFeatured(), mutateInstalled()]),
  };
}
