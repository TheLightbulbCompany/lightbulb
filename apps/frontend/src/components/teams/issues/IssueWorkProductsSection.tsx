"use client";

import { useState } from "react";
import {
  ExternalLink,
  FileArchive,
  FileText,
  GitBranch,
  GitCommit,
  GitPullRequest,
  Globe,
  Server,
} from "lucide-react";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { safeHref } from "@/components/teams/shared/lib/safeHref";
import { useIssueWorkProducts } from "./hooks/useIssueWorkProducts";
import type {
  IssueWorkProduct,
  IssueWorkProductReviewState,
} from "@/components/teams/shared/types";

/** Icon per work-product type. Falls back to a generic file glyph. */
function TypeIcon({ type }: { type: string }) {
  const common = { size: 14, className: "shrink-0", style: { color: "var(--midnight-muted)" } };
  switch (type) {
    case "preview_url":
      return <Globe {...common} />;
    case "runtime_service":
      return <Server {...common} />;
    case "pull_request":
      return <GitPullRequest {...common} />;
    case "branch":
      return <GitBranch {...common} />;
    case "commit":
      return <GitCommit {...common} />;
    case "artifact":
      return <FileArchive {...common} />;
    default:
      return <FileText {...common} />;
  }
}

/** Human label for a work-product type (snake_case → Title Case). */
function typeLabel(type: string): string {
  return type
    .split("_")
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

/** Small pill. `tone` shifts the color for review-state emphasis. */
function Pill({
  children,
  tone = "neutral",
}: {
  children: React.ReactNode;
  tone?: "neutral" | "attention" | "positive" | "danger";
}) {
  const color =
    tone === "attention"
      ? "var(--warn)"
      : tone === "positive"
        ? "var(--midnight)"
        : tone === "danger"
          ? "var(--err)"
          : "var(--midnight-soft)";
  return (
    <span
      className="label-caps px-1.5 py-0.5 rounded-sm shrink-0"
      style={{ border: `1px solid var(--edge)`, color }}
    >
      {children}
    </span>
  );
}

const REVIEW_STATE_LABEL: Record<string, string> = {
  needs_board_review: "Needs review",
  approved: "Approved",
  changes_requested: "Changes requested",
};

function reviewTone(state: string): "neutral" | "attention" | "positive" | "danger" {
  if (state === "approved") return "positive";
  if (state === "changes_requested") return "danger";
  if (state === "needs_board_review") return "attention";
  return "neutral";
}

function WorkProductRow({
  wp,
  onReview,
}: {
  wp: IssueWorkProduct;
  onReview: (id: string, state: IssueWorkProductReviewState) => Promise<void>;
}) {
  const [busy, setBusy] = useState<IssueWorkProductReviewState | null>(null);
  const when = wp.createdAt ?? wp.updatedAt;
  const needsReview = wp.reviewState === "needs_board_review";
  // The BFF only length-checks a work-product url, so a compromised/buggy agent
  // could emit a non-web scheme (data:/javascript:). Gate it through the same
  // whitelist Teams links use before rendering a live Open affordance.
  const openUrl = safeHref(wp.url);

  const act = async (state: IssueWorkProductReviewState) => {
    setBusy(state);
    try {
      await onReview(wp.id, state);
      // No reset on success — the list revalidates and the row re-renders with
      // the new review state (buttons drop away).
    } catch {
      setBusy(null);
    }
  };

  return (
    <li className="flex flex-col gap-2 px-3 py-2" style={{ border: "1px solid var(--edge)", borderRadius: 6 }}>
      <div className="flex items-center gap-2">
        <TypeIcon type={wp.type} />
        {wp.isPrimary ? <Pill tone="attention">Primary</Pill> : null}
        <span className="flex-1 truncate text-sm text-[var(--midnight)]" title={wp.title}>
          {wp.title}
        </span>
        {openUrl ? (
          <a
            href={openUrl}
            target="_blank"
            rel="noreferrer"
            aria-label={`Open ${wp.title}`}
            className="inline-flex items-center gap-1 text-xs shrink-0 rounded-sm px-1.5 py-1 transition-colors hover:bg-[var(--cream-deep)]"
            style={{ color: "var(--midnight)" }}
          >
            <ExternalLink size={12} strokeWidth={1.75} />
            Open
          </a>
        ) : null}
      </div>
      <div className="flex items-center gap-1.5 flex-wrap pl-[22px]">
        <Pill>{typeLabel(wp.type)}</Pill>
        {wp.reviewState && wp.reviewState !== "none" ? (
          <Pill tone={reviewTone(wp.reviewState)}>
            {REVIEW_STATE_LABEL[wp.reviewState] ?? wp.reviewState}
          </Pill>
        ) : null}
        {when ? (
          <span className="text-xs" style={{ color: "var(--midnight-muted)" }}>
            {timeAgo(when)}
          </span>
        ) : null}
      </div>
      {needsReview ? (
        <div className="flex items-center gap-2 pl-[22px]">
          <button
            type="button"
            onClick={() => void act("approved")}
            disabled={busy !== null}
            className="rounded-sm px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-50"
            style={{ background: "var(--midnight)", color: "var(--cream)" }}
          >
            {busy === "approved" ? "Approving…" : "Approve"}
          </button>
          <button
            type="button"
            onClick={() => void act("changes_requested")}
            disabled={busy !== null}
            className="rounded-sm px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-50"
            style={{ border: "1px solid var(--edge)", color: "var(--midnight)" }}
          >
            {busy === "changes_requested" ? "Requesting…" : "Request changes"}
          </button>
        </div>
      ) : null}
    </li>
  );
}

/**
 * Issue "Output" section — surfaces the typed deliverables an agent produced
 * (preview_url / pull_request / artifact / document / …), each with its review
 * state and, when it needs board review, Approve / Request-changes controls.
 * This closes the "where did my agent's deliverable go" gap: previously the UI
 * showed only raw attachments. Rendered after the Documents section, mirroring
 * IssueDocumentsSection's shape (omitted entirely when there is nothing to show
 * and nothing to report).
 */
export function IssueWorkProductsSection({ issueId }: { issueId: string }) {
  const { workProducts, isLoading, isError, setReviewState } = useIssueWorkProducts(issueId);

  if (workProducts.length === 0 && !isError && !isLoading) return null;

  return (
    <section className="flex flex-col gap-3 pt-4" style={{ borderTop: "1px solid var(--edge)" }}>
      <h2 className="section-label">
        <b>Output</b>
      </h2>
      {isError && (
        <p className="text-xs" style={{ color: "var(--err)" }}>
          Couldn&apos;t load deliverables.
        </p>
      )}
      {isLoading && workProducts.length === 0 && !isError && (
        <p className="text-xs italic" style={{ color: "var(--midnight-muted)" }}>
          Loading deliverables…
        </p>
      )}
      {workProducts.length > 0 && (
        <ul className="flex flex-col gap-2">
          {workProducts.map((wp) => (
            <WorkProductRow key={wp.id} wp={wp} onReview={setReviewState} />
          ))}
        </ul>
      )}
    </section>
  );
}
