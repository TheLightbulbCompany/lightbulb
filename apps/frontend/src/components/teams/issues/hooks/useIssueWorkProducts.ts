// Issue work-products data layer: list (SWR) + review-state PATCH, all through
// the Teams BFF (`/teams/issues/{id}/work-products` for both list and the
// per-product review PATCH). The PATCH is issue-scoped so the BFF can verify
// the product belongs to this issue AND is awaiting board review before
// forwarding — see routers/teams/issues.py:patch_work_product.
// Mirrors useIssueAttachments' shape so the Output section stays declarative.

import { useCallback } from "react";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import type {
  IssueWorkProduct,
  IssueWorkProductReviewState,
} from "@/components/teams/shared/types";

const SWR_PREFIX = "/teams";
const swrKey = (path: string) => `${SWR_PREFIX}${path}`;

export interface UseIssueWorkProductsResult {
  workProducts: IssueWorkProduct[];
  isLoading: boolean;
  isError: boolean;
  error: Error | null;
  /** Drive a work-product's board-review transition; revalidates on success. */
  setReviewState: (
    workProductId: string,
    reviewState: IssueWorkProductReviewState,
  ) => Promise<void>;
}

function wpTime(wp: IssueWorkProduct): number {
  const v = wp.createdAt ?? wp.updatedAt ?? null;
  const t = v ? new Date(v).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

export function useIssueWorkProducts(issueId: string): UseIssueWorkProductsResult {
  const { read, patch } = useTeamsApi();
  const { mutate } = useSWRConfig();
  const listPath = `/issues/${issueId}/work-products`;
  const { data, error, isLoading } = read<{ workProducts: IssueWorkProduct[] }>(listPath);

  // Primary first, then newest first — matches the Output surface ordering
  // (the isPrimary deliverable is the headline; the rest by recency).
  const workProducts = [...(data?.workProducts ?? [])].sort((a, b) => {
    if (Boolean(a.isPrimary) !== Boolean(b.isPrimary)) return a.isPrimary ? -1 : 1;
    return wpTime(b) - wpTime(a);
  });

  const setReviewState = useCallback(
    async (
      workProductId: string,
      reviewState: IssueWorkProductReviewState,
    ): Promise<void> => {
      await patch(`/issues/${issueId}/work-products/${workProductId}`, { reviewState });
      void mutate(swrKey(listPath));
    },
    [patch, mutate, listPath, issueId],
  );

  return {
    workProducts,
    isLoading,
    isError: !!error,
    error: error ?? null,
    setReviewState,
  };
}
