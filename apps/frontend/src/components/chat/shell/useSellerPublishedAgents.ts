"use client";

import { useMemo } from "react";
import useSWR from "swr";
import { useAuth, useUser } from "@clerk/nextjs";

import { myListings } from "@/lib/marketplace/api";

// Fetch the seller's listings so we can render a gold pub-dot on the
// sidebar avatar of agents the user has published. Cross-references
// listing.manifest_json.agent_id (source agent at publish time) with the
// current installed agent set. Best-effort — failure drops the badge
// silently rather than breaking the chat shell.
// SWR (vs. a mount effect) so the call dedupes across ChatLayout
// remounts and never re-fires on Clerk's getToken identity churn — the
// effect form refetched on every auth-state render. Best-effort: errors
// drop the badge silently (no retry; the badge isn't load-bearing).
export function useSellerPublishedAgents(): Set<string> {
  const { getToken } = useAuth();
  const { user } = useUser();

  const { data: sellerListings } = useSWR(
    user?.id ? ["seller-listings", user.id] : null,
    async ([, sellerId]: [string, string]) => {
      const jwt = await getToken();
      if (!jwt) return { items: [] };
      return myListings({ sellerId, jwt });
    },
    { revalidateOnFocus: false, dedupingInterval: 300_000, shouldRetryOnError: false },
  );
  const publishedAgentIds = useMemo(() => {
    const ids = new Set<string>();
    for (const listing of sellerListings?.items ?? []) {
      const sourceAgentId = (listing as unknown as { manifest_json?: { agent_id?: string } })
        .manifest_json?.agent_id;
      if (sourceAgentId) ids.add(sourceAgentId);
    }
    return ids;
  }, [sellerListings]);

  return publishedAgentIds;
}
