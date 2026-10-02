// Ported from upstream Paperclip's useIssueExternalObjects.ts
// (paperclip/ui/src/hooks/useIssueExternalObjects.ts) (MIT, (c) 2025 Paperclip AI).
// TanStack Query -> SWR through the Teams BFF. Upstream gates each query on the
// instance flag `enableExternalObjects`; the browser cannot read that here, and
// does not need to: with the flag off the BFF answers an empty list, and every
// consumer renders nothing for an empty list.

import { useMemo } from "react";
import useSWR from "swr";
import { useAuth } from "@clerk/nextjs";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type {
  ExternalObjectMention,
  ExternalObjectMentionGroup,
  ExternalObjectSummary,
} from "@/components/teams/shared/types";
import type { ExternalObjectPillData } from "./ExternalObjectPill";

export const EXTERNAL_OBJECT_SUMMARY_BATCH_SIZE = 500;

function formatMentionSourceLabel(mention: ExternalObjectMention): string {
  switch (mention.sourceKind) {
    case "title":
      return "Title";
    case "description":
      return "Description";
    case "comment":
      return "Comment";
    case "document":
      return mention.documentKey ? `Document: ${mention.documentKey}` : "Document";
    case "property":
      return mention.propertyKey ? `Property: ${mention.propertyKey}` : "Property";
    case "plugin":
      return "Plugin";
    default:
      return "Source";
  }
}

export interface IssueExternalObjectGroup {
  pill: ExternalObjectPillData;
  mentionCount: number;
  sourceLabels: string[];
  group: ExternalObjectMentionGroup;
}

export interface IssueExternalObjectsResult {
  groups: IssueExternalObjectGroup[];
  isLoading: boolean;
  isError: boolean;
  refetch: () => void;
}

/**
 * Loads the external objects an issue mentions and shapes them into per-group
 * rows for the properties rail.
 */
export function useIssueExternalObjects(issueId: string): IssueExternalObjectsResult {
  const { read } = useTeamsApi();
  const { data, error, isLoading, mutate } = read<{ externalObjects?: ExternalObjectMentionGroup[] }>(
    teamsQueryKeys.issues.externalObjects(issueId),
    { dedupingInterval: 60_000 },
  );

  const rows = data?.externalObjects;
  const groups: IssueExternalObjectGroup[] = (Array.isArray(rows) ? rows : [])
    .filter((entry) => Boolean(entry.object))
    .map((entry) => {
      const object = entry.object!;
      const mentions = entry.mentions ?? [];
      const sourceLabels = entry.sourceLabels && entry.sourceLabels.length > 0
        ? entry.sourceLabels
        : Array.from(new Set(mentions.map(formatMentionSourceLabel)));
      return {
        group: entry,
        mentionCount: entry.mentionCount ?? mentions.length,
        sourceLabels,
        pill: {
          providerKey: object.providerKey,
          objectType: object.objectType,
          displayKey: object.displayKey,
          iconKey: object.iconKey,
          statusCategory: object.statusCategory,
          liveness: object.liveness,
          displayTitle: object.displayTitle,
          statusLabel: object.statusLabel,
          statusIconKey: object.statusIconKey,
          url: safeHref(object.sanitizedCanonicalUrl) ?? null,
        },
      };
    });

  return {
    groups,
    isLoading,
    isError: !!error,
    refetch: () => void mutate(),
  };
}

type SummariesResponse = { summaries?: Record<string, ExternalObjectSummary> };

/**
 * Status rollups for a list of issues: one request per list (per 500 ids), not
 * one per row. The ids travel in a POST body, so this is `useSWR` directly
 * rather than `read()`; the key mirrors `useTeamsApi`'s `[path, userId, orgId]`
 * shape so TeamsEventsProvider's invalidation reaches it.
 */
export function useIssueExternalObjectSummaries(
  issueIds: readonly string[],
): Map<string, ExternalObjectSummary> {
  const { post } = useTeamsApi();
  const { isLoaded, userId, orgId } = useAuth();
  const ids = [...new Set(issueIds.filter((issueId) => issueId.length > 0))].sort();
  const path = teamsQueryKeys.issues.externalObjectSummaries();
  const key = isLoaded && userId && ids.length > 0
    ? ([`/teams${path}?${ids.join(",")}`, userId, orgId ?? null] as const)
    : null;
  const { data } = useSWR(
    key,
    async () => {
      const summaries: Record<string, ExternalObjectSummary> = {};
      for (let index = 0; index < ids.length; index += EXTERNAL_OBJECT_SUMMARY_BATCH_SIZE) {
        const issueIdBatch = ids.slice(index, index + EXTERNAL_OBJECT_SUMMARY_BATCH_SIZE);
        const response = await post<SummariesResponse>(path, { issueIds: issueIdBatch });
        Object.assign(summaries, response?.summaries ?? {});
      }
      return summaries;
    },
    { dedupingInterval: 60_000, shouldRetryOnError: false },
  );
  return useMemo(() => new Map(Object.entries(data ?? {})), [data]);
}
