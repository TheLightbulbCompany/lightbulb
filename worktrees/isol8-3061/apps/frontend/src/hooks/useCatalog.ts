import useSWR from "swr";
import { useCallback, useMemo } from "react";

import { useApi } from "@/lib/api";

/**
 * Marketplace catalog data source for the drawer.
 *
 * Reads ``/marketplace/featured-listings`` and
 * ``/marketplace/me/installed-templates``, exposes the difference as
 * ``agents`` plus a ``deploy(listingId)`` mutation. Consumed by the drawer's
 * BrowseView and the useHire deploy flow.
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
  /** ACP coding-harness delegation outcome (see catalog_service.deploy):
   * - acp_requested: the listing's manifest carries requires_acp.
   * - acp_enabled: the buyer's container actually got ACP turned on (false
   *   when the provider can't run any harness or the platform flag is off).
   * Both default-absent on listings published before ACP shipped. */
  acp_requested?: boolean;
  acp_enabled?: boolean;
  /** Webhook automations the listing ships that the deploy deliberately did
   * NOT arm — each binds a provider-side resource only the buyer can name
   * (their repo, their mailbox, their Page). Handed back as the buyer's
   * "finish wiring me up" list; absent on listings with none. */
  automations_pending?: { provider: string; event_id: string; needs_resource: boolean }[];
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
    "/marketplace/featured-listings",
    () => api.get("/marketplace/featured-listings") as Promise<{ items: MarketplaceListing[] }>,
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
    async (listingId: string, reportsTo?: string | null): Promise<DeployResult> => {
      // `reports_to` (org placement) is optional — omit the field when no
      // manager was chosen so the backend leaves the new agent unparented.
      const result = (await api.post(
        `/marketplace/listings/${encodeURIComponent(listingId)}/deploy`,
        reportsTo ? { reports_to: reportsTo } : {},
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
    installedSlugs,
  };
}
