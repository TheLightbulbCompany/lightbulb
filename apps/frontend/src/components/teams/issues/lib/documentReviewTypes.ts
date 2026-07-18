// Wire types for the plan-document review surface (A3). Local subset of the
// upstream Paperclip `DocumentRevision` / `DocumentAnnotationThreadWithComments`
// shapes — only the fields the Isol8 Teams UI reads. Kept local to the issues
// module (not shared/types.ts) so this A3 addition doesn't touch a file other
// agents edit in parallel.

export type AnnotationThreadStatus = "open" | "resolved";
export type AnnotationAnchorState = "active" | "stale" | "orphaned" | string;

export interface AnnotationTextQuoteSelector {
  exact: string;
  prefix: string;
  suffix: string;
}

export interface AnnotationTextPositionSelector {
  normalizedStart: number;
  normalizedEnd: number;
  markdownStart: number;
  markdownEnd: number;
}

export interface AnnotationAnchorSelector {
  quote: AnnotationTextQuoteSelector;
  position: AnnotationTextPositionSelector;
}

export interface DocumentAnnotationComment {
  id: string;
  threadId: string;
  body: string;
  authorType?: string | null;
  authorAgentId?: string | null;
  authorUserId?: string | null;
  authorName?: string | null;
  createdAt: string;
}

export interface DocumentAnnotationThread {
  id: string;
  documentKey: string;
  status: AnnotationThreadStatus;
  anchorState: AnnotationAnchorState;
  selectedText: string;
  normalizedStart?: number;
  markdownStart?: number;
  currentRevisionNumber?: number;
  createdByAgentId?: string | null;
  createdByUserId?: string | null;
  createdAt: string;
  updatedAt?: string;
  comments: DocumentAnnotationComment[];
}

export interface DocumentRevision {
  id: string;
  key: string;
  revisionNumber: number;
  title: string | null;
  body: string;
  changeSummary: string | null;
  createdByAgentId: string | null;
  createdByUserId: string | null;
  createdAt: string;
}

/** The revision fields the upstream wire payload carries on an issue document,
 * absent from the trimmed local `IssueDocument` type. */
export interface DocumentRevisionRef {
  latestRevisionId?: string | null;
  latestRevisionNumber?: number | null;
}

/** A pending selection ready to be turned into an annotation thread. */
export interface PendingAnchor {
  selector: AnnotationAnchorSelector;
  selectedText: string;
}
