// Ported from upstream Paperclip's pages/IssueDetail.tsx mutations + the
// NewIssueDialog form submit (paperclip/ui/src/pages/IssueDetail.tsx,
// components/NewIssueDialog.tsx) (MIT, (c) 2025 Paperclip AI).
// v1: create + update + addComment. Defers checkout/feedback/interactions/etc.

import { useCallback } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import { captureHandoffCompleted } from "@/lib/analytics";
import type {
  Issue,
  IssueComment,
  IssueCreateInput,
  IssueUpdateInput,
} from "@/components/teams/shared/types";

const SWR_PREFIX = "/teams";
const swrKey = (path: string) => `${SWR_PREFIX}${path}`;

/** In-flight `updateInList` writes per list path (see there). */
const pendingListWrites = new Map<string, Set<Promise<unknown>>>();

export interface UseIssueMutationsResult {
  create: (input: IssueCreateInput) => Promise<Issue>;
  createChild: (parentId: string, input: IssueCreateInput) => Promise<Issue>;
  update: (issueId: string, input: IssueUpdateInput) => Promise<Issue>;
  /** `update`, shown at once in the cached `{ issues }` read at `listPath`. */
  updateInList: (listPath: string, issueId: string, input: IssueUpdateInput) => Promise<void>;
  remove: (issueId: string) => Promise<void>;
  addComment: (issueId: string, body: string) => Promise<IssueComment>;
  /** Trigger an immediate monitor re-check (independent of nextCheckAt). */
  monitorCheckNow: (issueId: string) => Promise<void>;
}

export function useIssueMutations(): UseIssueMutationsResult {
  const { post, patch, del, mutate, workspaceId } = useTeamsApi();

  const create = useCallback(async (input: IssueCreateInput): Promise<Issue> => {
    // The Teams BFF accepts camelCase aliases (Pydantic populate_by_name=True)
    // and forwards them verbatim to Paperclip — IssueCreateInput's shape IS
    // the wire shape, no boundary translation needed.
    const created = await post<Issue>("/issues", input);
    // Invalidate inbox lists (any tab) so the new issue appears.
    mutate((key) => typeof key === "string" && key.startsWith("/teams/inbox?"));
    return created;
  }, [post, mutate]);

  const createChild = useCallback(
    async (parentId: string, input: IssueCreateInput): Promise<Issue> => {
      // Routes to the upstream children endpoint (parent/child wired server-side),
      // not the plain create. Revalidate the parent's children list so the new
      // sub-issue appears.
      const created = await post<Issue>(`/issues/${parentId}/children`, input);
      mutate(swrKey(`/issues/${parentId}/children`));
      return created;
    },
    [post, mutate],
  );

  const update = useCallback(
    async (issueId: string, input: IssueUpdateInput): Promise<Issue> => {
      const updated = await patch<Issue>(`/issues/${issueId}`, input);
      if (input.status && updated.assigneeAgentId) {
        captureHandoffCompleted(workspaceId, "task_moved");
      }
      // Every cached issue detail, not just this one: another task's
      // "Blocking"/"Blocked by" chips show this task and its status, so a
      // blocker or status change leaves them stale until reload otherwise.
      mutate((key) => typeof key === "string" && /^\/teams\/issues\/[^/?]+$/.test(key));
      mutate((key) => typeof key === "string" && key.startsWith("/teams/inbox?"));
      return updated;
    },
    [patch, mutate, workspaceId],
  );

  // Optimistic: the patch lands in the cached list synchronously, before the
  // PATCH is sent, so a board drop stays in the target column instead of
  // snapping back until a refetch. SWR drops any revalidation of `listPath`
  // that started before the write finished (its MUTATION timestamps — the
  // equivalent of cancelQueries), so the 30 s refresh can't land mid-flight and
  // undo the move. On failure the cache rolls back and the promise rejects; on
  // either outcome the list revalidates against the server.
  //
  // Several moves can be in flight on one list. Each optimistic patch stacks on
  // what is DISPLAYED (not the last committed list), and each write's mutation
  // settles only after every earlier write to the list has: SWR revalidates
  // only for the newest mutation, so that refetch must not run while an older
  // PATCH is still out, or it reads back the older card's old status.
  const updateInList = useCallback(
    async (listPath: string, issueId: string, input: IssueUpdateInput): Promise<void> => {
      const pending = pendingListWrites.get(listPath) ?? new Set<Promise<unknown>>();
      pendingListWrites.set(listPath, pending);
      const earlier = [...pending];
      const write = update(issueId, input);
      pending.add(write);
      const settled = write.finally(async () => {
        await Promise.allSettled(earlier);
        pending.delete(write);
      });
      await mutate(swrKey(listPath), settled, {
        // The list read is `{ issues }` or a bare array (the project read
        // accepts both); anything else is left alone.
        optimisticData: (_committed: unknown, displayed: unknown) => {
          const patchIn = (list: Issue[]) =>
            list.map((i) => (i.id === issueId ? ({ ...i, ...input } as Issue) : i));
          if (Array.isArray(displayed)) return patchIn(displayed as Issue[]);
          const body = displayed as { issues?: Issue[] } | undefined;
          return body?.issues ? { ...body, issues: patchIn(body.issues) } : displayed;
        },
        rollbackOnError: true,
        populateCache: false,
        revalidate: true,
      });
    },
    [mutate, update],
  );

  const remove = useCallback(
    async (issueId: string): Promise<void> => {
      await del(`/issues/${issueId}`);
      // Invalidate inbox lists (any tab) so the deleted issue drops out — the
      // detail key for this id is intentionally not revalidated since the
      // caller navigates away from the now-gone issue.
      mutate((key) => typeof key === "string" && key.startsWith("/teams/inbox?"));
    },
    [del, mutate],
  );

  const addComment = useCallback(
    async (issueId: string, body: string): Promise<IssueComment> => {
      const created = await post<IssueComment>(`/issues/${issueId}/comments`, { body });
      mutate(swrKey(teamsQueryKeys.issues.comments(issueId)));
      return created;
    },
    [post, mutate],
  );

  const monitorCheckNow = useCallback(
    async (issueId: string): Promise<void> => {
      // Upstream's check-now endpoint takes no body and returns {ok:true};
      // it re-runs the monitor now rather than waiting for nextCheckAt.
      await post(`/issues/${issueId}/monitor/check-now`, {});
    },
    [post],
  );

  return { create, createChild, update, updateInList, remove, addComment, monitorCheckNow };
}
