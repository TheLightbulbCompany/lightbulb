// apps/frontend/src/components/teams/issues/IssueWorkProductsSection.tsx
//
// The side panel's deliverables block, composed from stock Paperclip @
// v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI):
//   - ./issue-output/IssueOutputSection — ui/src/components/issue-output/*: the
//     primary card / video player / file tiles for attachment-backed outputs;
//   - ./issue-output/WorkProductArtifactRows — the work-product rows of
//     ui/src/components/issue-properties/IssuePropertiesArtifactsTab.tsx, which
//     is where stock's task-chat side panel lists work products: Markdown
//     artifacts expand into their `artifact-review-*` document, everything else
//     is a compact card.
// Stock shows the Output section only on the classic page and the Artifacts tab
// only in the shell; our side panel stacks both, so rows already shown as an
// output are not repeated. Documents and agent attachments keep their own
// sections below (IssueDocumentsSection hides the `artifact-review-*` proxies,
// IssueAttachmentsSection the promoted outputs), and the Artifacts tab's
// per-run grouping is not ported.

"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";
import type { IssueDocument } from "@/components/teams/shared/types";
import { useIssueWorkProducts } from "./hooks/useIssueWorkProducts";
import { useScrollToHashAnchor } from "@/components/teams/shared/lib/useScrollToHashAnchor";
import {
  artifactReviewDocumentKey,
  getIssueOutputs,
  getMarkdownWorkProductAttachmentMetadata,
} from "./lib/issue-output";
import { IssueOutputSection } from "./issue-output/IssueOutputSection";
import {
  MarkdownWorkProductRow,
  WorkProductCard,
  WorkProductReviewActions,
} from "./issue-output/WorkProductArtifactRows";

export function IssueWorkProductsSection({ issueId }: { issueId: string }) {
  const { workProducts, isLoading, isError, setReviewState } = useIssueWorkProducts(issueId);
  const { read } = useTeamsApi();
  const { data: docsResp } = read<{ documents: IssueDocument[] }>(`/issues/${issueId}/documents`);
  const reviewDocsByKey = new Map((docsResp?.documents ?? []).map((doc) => [doc.key, doc]));

  // Deep-link scroll for `#work-product-<id>` (posted by comments/interactions
  // linking a deliverable) — the row's `id` is set below; this makes the
  // anchor actually land there after a pushState navigation.
  useScrollToHashAnchor(workProducts.length > 0);

  if (workProducts.length === 0 && !isError && !isLoading) return null;

  const outputIds = new Set(getIssueOutputs(workProducts).items.map((item) => item.id));
  // An output awaiting board review is listed again so it keeps its Approve.
  const rows = workProducts.filter(
    (wp) => !outputIds.has(wp.id) || wp.reviewState === "needs_board_review",
  );

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <IssueOutputSection workProducts={workProducts} />
      {isError && (
        <p className="text-caption" style={{ color: "var(--err)" }}>
          Couldn&apos;t load deliverables.
        </p>
      )}
      {isLoading && workProducts.length === 0 && !isError && (
        <p className="text-caption italic" style={{ color: "var(--midnight-muted)" }}>
          Loading deliverables…
        </p>
      )}
      {rows.length > 0 && (
        <>
          <h2 className="section-label">
            <b>Work products</b>
          </h2>
          <ul className="flex flex-col gap-1">
            {rows.map((wp) => {
              const markdownMetadata = getMarkdownWorkProductAttachmentMetadata(wp);
              return (
                <li key={wp.id} id={`work-product-${wp.id}`}>
                  {markdownMetadata ? (
                    <MarkdownWorkProductRow
                      issueId={issueId}
                      workProduct={wp}
                      metadata={markdownMetadata}
                      reviewDoc={reviewDocsByKey.get(artifactReviewDocumentKey(wp.id))}
                    />
                  ) : (
                    <WorkProductCard workProduct={wp} />
                  )}
                  {wp.reviewState === "needs_board_review" ? (
                    <WorkProductReviewActions onReview={(state) => setReviewState(wp.id, state)} />
                  ) : null}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}
