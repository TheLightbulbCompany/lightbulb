// apps/frontend/src/components/teams/inbox/DecisionCard.tsx

// Ported from upstream Paperclip's DecisionCard
// (paperclip/ui/src/components/DecisionCard.tsx) (MIT, (c) 2025 Paperclip AI).
//
// Swaps vs upstream (markup, states, copy and behaviour are upstream's):
//   - MarkdownBody -> MarkdownView
//   - issue / run links commit through handleShellLinkClick (in-shell swap)
//     instead of a router href
//   - sky -> --link, rose -> --alert, emerald -> --live, amber -> --warn;
//     shadcn semantic classes -> --fg / --fg-mute / --edge / --plane-*
//   - the per-control focus ring is dropped for the one global :focus-visible
//     outline

"use client";

import { useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Ban,
  CheckCircle2,
  Clock,
  ExternalLink,
  Loader2,
  MinusCircle,
  ShieldAlert,
  XCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { handleShellLinkClick } from "@/lib/shellNav";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { decisionEffectTargetIssueIds } from "@/components/teams/shared/lib/decisions";
import type {
  Decision,
  DecisionEffect,
  DecisionEffectExecution,
  DecisionOption,
  DecisionTargetSnapshot,
} from "@/components/teams/shared/types";

/**
 * Presentational card for a single decision. It renders every state — pending /
 * stale-target / destructive cancel-tree / decided / partial / failed / expired
 * / cancelled / dismissed — and delegates the actual decide / dismiss mutations
 * to the parent (`DecisionResolver`). It never fetches: issue labels + the
 * cancel-tree preview arrive via resolvers.
 */

export interface DecisionIssueRef {
  id: string;
  identifier: string | null;
  title: string | null;
  /** In-shell URL (teamsDetailUrl), committed through handleShellLinkClick. */
  href: string;
  status?: string | null;
}

export interface DecisionCardProps {
  decision: Decision;
  /** Per-effect execution rows (present once decided). */
  executions?: DecisionEffectExecution[] | null;
  /** Which target issues drifted since the snapshot (open decisions only). */
  targetChanged?: Record<string, boolean> | null;
  /** Resolve an issue id to a display ref (identifier / title / link). */
  resolveIssue?: (issueId: string) => DecisionIssueRef | null;
  /** Full sub-tree that a `cancel_issue_tree` option would cancel. */
  cancelTreePreview?: (targetIssueId: string) => DecisionIssueRef[] | null;
  originAgentName?: string | null;
  originIssue?: DecisionIssueRef | null;
  runHref?: string | null;
  busy?: boolean;
  errorMessage?: string | null;
  onDecide?: (optionId: string, inputValues: Record<string, string>) => void;
  onDismiss?: (reason?: string) => void;
  className?: string;
}

// --- small helpers ----------------------------------------------------------

function humanStatus(status: string | null | undefined): string {
  if (!status) return "unknown";
  return status.replaceAll("_", " ");
}

function issueLabel(link: DecisionIssueRef | null, fallbackId: string): string {
  if (link?.identifier) return link.identifier;
  if (link?.title) return link.title;
  return `issue ${fallbackId.slice(0, 8)}`;
}

function pluralize(count: number, singular: string): string {
  return `${count} ${count === 1 ? singular : `${singular}s`}`;
}

function isDestructiveOption(option: DecisionOption): boolean {
  return option.style === "destructive" || option.effects.some((effect) => effect.type === "cancel_issue_tree");
}

function cancelTreeEffect(option: DecisionOption): Extract<DecisionEffect, { type: "cancel_issue_tree" }> | null {
  return (option.effects.find((effect) => effect.type === "cancel_issue_tree") ?? null) as
    | Extract<DecisionEffect, { type: "cancel_issue_tree" }>
    | null;
}

/** One-line feed-forward preview of what an effect will do, before you commit. */
function effectSummary(
  effect: DecisionEffect,
  resolve: (id: string) => DecisionIssueRef | null,
  snapshots: Record<string, DecisionTargetSnapshot>,
): string {
  const target = issueLabel(resolve(effect.targetIssueId), effect.targetIssueId);
  switch (effect.type) {
    case "comment_on_issue":
      return `Comment on ${target}`;
    case "create_issue": {
      const parent = effect.draft.parentId
        ? issueLabel(resolve(effect.draft.parentId), effect.draft.parentId)
        : target;
      return `Create issue “${effect.draft.title}” under ${parent}`;
    }
    case "update_issue_status":
      return `Set ${target} to ${humanStatus(effect.status)}`;
    case "assign_issue":
      return `Reassign ${target}`;
    case "resolve_blocker":
      return `Unblock ${target} — remove ${pluralize(effect.removeBlockedByIssueIds.length, "blocker")}`;
    case "cancel_issue_tree": {
      const snapshot = snapshots[effect.targetIssueId];
      const descendantCount = snapshot?.descendantCount ?? snapshot?.descendantIds?.length ?? snapshot?.childCount ?? 0;
      return `Cancel ${target} and its sub-tree (${pluralize(descendantCount + 1, "issue")})`;
    }
    default:
      return "Apply effect";
  }
}

const FAILURE_CAUSE: Record<string, string> = {
  deny_decision_intersection: "blocked by the permission boundary (fail-closed)",
  invalid_effect_reference: "a referenced issue no longer exists",
  target_changed: "the target changed since this was proposed",
  effect_execution_failed: "the effect errored while running",
};

interface ResultRow {
  key: string;
  status: DecisionEffectExecution["status"];
  summary: string;
  link: DecisionIssueRef | null;
}

function executionRow(
  execution: DecisionEffectExecution,
  resolve: (id: string) => DecisionIssueRef | null,
): ResultRow {
  const targetRef = resolve(execution.targetIssueId);
  const target = issueLabel(targetRef, execution.targetIssueId);
  const result = execution.result ?? {};
  if (execution.status === "skipped") {
    return { key: execution.id, status: "skipped", summary: `Skipped ${target} — target changed since proposal`, link: targetRef };
  }
  if (execution.status === "failed") {
    const cause = FAILURE_CAUSE[execution.error ?? ""] ?? execution.error ?? "the effect could not run";
    return { key: execution.id, status: "failed", summary: `Failed on ${target} — ${cause}`, link: targetRef };
  }
  if (execution.status === "claimed") {
    return { key: execution.id, status: "claimed", summary: `Running on ${target}…`, link: targetRef };
  }
  // executed
  switch (execution.effectType) {
    case "comment_on_issue":
      return { key: execution.id, status: "executed", summary: `Commented on ${target}`, link: targetRef };
    case "create_issue": {
      const createdId = typeof result.issueId === "string" ? result.issueId : null;
      const created = createdId ? resolve(createdId) : null;
      return {
        key: execution.id,
        status: "executed",
        summary: `Created ${created ? issueLabel(created, createdId!) : "a new issue"}`,
        link: created ?? targetRef,
      };
    }
    case "update_issue_status":
      return { key: execution.id, status: "executed", summary: `Set ${target} to ${humanStatus(typeof result.status === "string" ? result.status : null)}`, link: targetRef };
    case "assign_issue":
      return { key: execution.id, status: "executed", summary: `Reassigned ${target}`, link: targetRef };
    case "resolve_blocker": {
      const removed = Array.isArray(result.removedBlockedByIssueIds) ? result.removedBlockedByIssueIds.length : 0;
      return { key: execution.id, status: "executed", summary: `Removed ${pluralize(removed, "blocker")} from ${target}`, link: targetRef };
    }
    case "cancel_issue_tree": {
      const cancelled = Array.isArray(result.cancelledIssueIds) ? result.cancelledIssueIds.length : 0;
      return { key: execution.id, status: "executed", summary: `Cancelled ${pluralize(cancelled, "issue")} under ${target}`, link: targetRef };
    }
    default:
      return { key: execution.id, status: "executed", summary: `Applied effect on ${target}`, link: targetRef };
  }
}

// --- shell / badge palette ---------------------------------------------------

type CardTone = "pending" | "destructive" | "success" | "partial" | "failed" | "neutral";

const SHELL: Record<CardTone, string> = {
  pending: "border-[var(--link)]",
  destructive: "border-2 border-[var(--alert)]",
  success: "border-[var(--live)]",
  partial: "border-[var(--warn)]",
  failed: "border-[var(--alert)]",
  neutral: "border-[var(--edge)]",
};

const BADGE: Record<CardTone, string> = {
  pending: "border-[var(--link)] bg-[var(--link)]/10 text-[var(--link)]",
  destructive: "border-[var(--alert)] bg-[var(--alert)]/10 text-[var(--alert)]",
  success: "border-[var(--live)] bg-[var(--live)]/10 text-[var(--live)]",
  partial: "border-[var(--warn)] bg-[var(--warn)]/10 text-[var(--warn)]",
  failed: "border-[var(--alert)] bg-[var(--alert)]/10 text-[var(--alert)]",
  neutral: "border-[var(--edge)] bg-[var(--plane-sidebar)] text-[var(--fg-mute)]",
};

const BADGE_BASE =
  "inline-flex items-center rounded-full border px-2 py-0.5 text-2xs font-semibold uppercase tracking-wide";
const INLINE_LINK = "font-medium text-[var(--link)] underline-offset-2 hover:underline";
const NOTE = "rounded-lg border border-[var(--edge)] bg-[var(--plane-raised)] px-3 py-2 text-xs text-[var(--fg-mute)]";

/** An in-shell link: a real href, committed client-side on a plain click. */
function ShellLink({ href, className, children }: { href: string; className?: string; children: ReactNode }) {
  return (
    <a href={href} onClick={(event) => handleShellLinkClick(event, href)} className={className}>
      {children}
    </a>
  );
}

function IssueLink({ link }: { link: DecisionIssueRef | null }) {
  if (!link) return null;
  return (
    <ShellLink
      href={link.href}
      className="inline-flex items-center gap-1 rounded-sm border border-[var(--edge)] bg-[var(--plane-pane)] px-1.5 py-0.5 text-xs font-medium text-[var(--fg)] hover:border-[var(--link)] hover:text-[var(--link)]"
    >
      {issueLabel(link, link.id)}
      <ExternalLink className="h-3 w-3" aria-hidden />
    </ShellLink>
  );
}

const RESULT_ICON: Record<ResultRow["status"], ReactNode> = {
  executed: <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-[var(--live)]" aria-hidden />,
  failed: <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--alert)]" aria-hidden />,
  skipped: <MinusCircle className="mt-0.5 h-4 w-4 shrink-0 text-[var(--warn)]" aria-hidden />,
  claimed: <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-[var(--fg-mute)]" aria-hidden />,
};

export function DecisionCard({
  decision,
  executions,
  targetChanged,
  resolveIssue = () => null,
  cancelTreePreview,
  originAgentName,
  originIssue,
  runHref,
  busy = false,
  errorMessage,
  onDecide,
  onDismiss,
  className,
}: DecisionCardProps) {
  const [inputValues, setInputValues] = useState<Record<string, string>>({});
  const [confirmOptionId, setConfirmOptionId] = useState<string | null>(null);
  const [confirmText, setConfirmText] = useState("");

  const open = decision.status === "open";
  const dismissed =
    decision.chosenOptionId === "dismissed" || (decision.metadata as { dismissed?: boolean } | null)?.dismissed === true;
  const snapshots = (decision.targetSnapshots ?? {}) as Record<string, DecisionTargetSnapshot>;

  const staleTargetIds = useMemo(
    () => (open ? Object.entries(targetChanged ?? {}).filter(([, changed]) => changed).map(([id]) => id) : []),
    [open, targetChanged],
  );
  const staleTargetIdSet = useMemo(() => new Set(staleTargetIds), [staleTargetIds]);
  const isStale = staleTargetIds.length > 0;
  const hasCancelTree = decision.options.some((option) => option.effects.some((effect) => effect.type === "cancel_issue_tree"));

  const tone: CardTone = open
    ? hasCancelTree
      ? "destructive"
      : "pending"
    : decision.status !== "decided"
      ? "neutral" // expired / cancelled
      : dismissed
        ? "neutral"
        : decision.executionStatus === "succeeded"
          ? "success"
          : decision.executionStatus === "partial"
            ? "partial"
            : "failed";

  const badgeLabel = open
    ? "Pending"
    : decision.status === "expired"
      ? "Expired"
      : decision.status === "cancelled"
        ? "Cancelled"
        : dismissed
          ? "Dismissed"
          : decision.executionStatus === "succeeded"
            ? "Decided"
            : decision.executionStatus === "partial"
              ? "Partial"
              : "Failed";

  const requiredUnmet = (decision.inputs ?? []).some(
    (field) => field.required && !(inputValues[field.id] ?? "").trim(),
  );

  const runOption = (option: DecisionOption) => {
    if (busy) return;
    const cancelTree = cancelTreeEffect(option);
    if (cancelTree && confirmOptionId !== option.id) {
      setConfirmOptionId(option.id);
      setConfirmText("");
      return;
    }
    onDecide?.(option.id, inputValues);
  };

  const dimmed = decision.status === "expired" || decision.status === "cancelled";
  const expiredReason = (decision.metadata as { expiredReason?: string } | null)?.expiredReason;

  // The issues this decision acts on. The origin issue is only where the agent
  // was running when it proposed the decision — the queue links there, but the
  // decision may target a different issue entirely, so name the targets
  // explicitly or the card is undiscoverable from the issue it applies to.
  const targetRefs = useMemo(() => {
    const ids = new Set<string>();
    for (const option of decision.options) {
      for (const effect of option.effects) {
        for (const id of decisionEffectTargetIssueIds(effect)) ids.add(id);
      }
    }
    if (originIssue?.id) ids.delete(originIssue.id);
    return [...ids].map((id) => ({ id, link: resolveIssue(id) }));
  }, [decision.options, originIssue?.id, resolveIssue]);

  return (
    <div
      className={cn(
        "rounded-2xl border bg-[var(--plane-pane)] p-4 text-sm",
        SHELL[tone],
        dimmed && "opacity-80",
        className,
      )}
      data-decision-state={badgeLabel.toLowerCase()}
    >
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="min-w-0 flex-1 text-base font-semibold text-[var(--fg)]">{decision.title}</h3>
        <div className="flex shrink-0 items-center gap-1.5">
          {open && hasCancelTree && (
            <span className={cn(BADGE_BASE, "gap-1", BADGE.destructive)}>
              <ShieldAlert className="h-3 w-3" aria-hidden /> Destructive
            </span>
          )}
          <span className={cn(BADGE_BASE, BADGE[tone])}>{badgeLabel}</span>
        </div>
      </div>

      {/* Provenance */}
      <p className="mt-1 text-xs text-[var(--fg-mute)]">
        Proposed by <span className="font-medium text-[var(--fg)]">{originAgentName ?? "an agent"}</span>
        {originIssue && (
          <>
            {" "}while running{" "}
            <ShellLink href={originIssue.href} className={INLINE_LINK}>
              {issueLabel(originIssue, originIssue.id)}
            </ShellLink>
          </>
        )}
        {targetRefs.length > 0 && (
          <>
            {" · applies to "}
            {targetRefs.map(({ id, link }, index) => (
              <span key={id}>
                {index > 0 && ", "}
                {link ? (
                  <ShellLink href={link.href} className={INLINE_LINK}>
                    {issueLabel(link, id)}
                  </ShellLink>
                ) : (
                  <span className="font-medium text-[var(--fg)]">{issueLabel(null, id)}</span>
                )}
              </span>
            ))}
          </>
        )}
        {runHref && (
          <>
            {" · "}
            <ShellLink href={runHref} className="hover:underline">view run</ShellLink>
          </>
        )}
      </p>

      {/* Body */}
      {decision.body?.trim() && (
        <div className="mt-3 text-sm leading-6 text-[var(--fg)]">
          <MarkdownView>{decision.body}</MarkdownView>
        </div>
      )}

      {/* Stale-target warning (open only) */}
      {open && isStale && (
        <div className="mt-3 rounded-lg border border-[var(--warn)] bg-[var(--warn)]/10 px-3 py-2 text-[var(--warn)]">
          <div className="flex items-center gap-2 text-sm font-medium">
            <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden />
            {pluralize(staleTargetIds.length, "target")} changed since this was proposed
          </div>
          <ul className="mt-1.5 space-y-1 text-xs">
            {staleTargetIds.map((id) => {
              const current = resolveIssue(id);
              const from = snapshots[id];
              return (
                <li key={id} className="flex flex-wrap items-center gap-1">
                  <span className="font-medium">{issueLabel(current, id)}:</span>
                  <span className="tabular-nums">{humanStatus(from?.status)}</span>
                  <ArrowRight className="h-3 w-3" aria-hidden />
                  <span className="tabular-nums">{humanStatus(current?.status) || "changed"}</span>
                </li>
              );
            })}
          </ul>
          <p className="mt-1.5 text-xs">
            Options that require an unchanged target are disabled below.
          </p>
        </div>
      )}

      {/* Inputs (open only) */}
      {open && (decision.inputs ?? []).length > 0 && (
        <div className="mt-3 space-y-2">
          {(decision.inputs ?? []).map((field) => (
            <label key={field.id} className="block">
              <span className="text-xs font-medium text-[var(--fg-mute)]">
                {field.label}
                {field.required && <span className="text-[var(--alert)]"> *</span>}
              </span>
              <Textarea
                value={inputValues[field.id] ?? ""}
                onChange={(event) => setInputValues((prev) => ({ ...prev, [field.id]: event.target.value }))}
                placeholder={field.placeholder ?? undefined}
                maxLength={field.maxLength ?? undefined}
                className="mt-1 min-h-16 bg-[var(--plane-pane)] text-sm"
              />
            </label>
          ))}
        </div>
      )}

      {/* Options (open only) */}
      {open && (
        <div className="mt-3 space-y-2">
          {decision.options.map((option) => {
            const destructive = isDestructiveOption(option);
            const blockedStale = option.effects.some(
              (effect) => effect.staleness === "strict" && decisionEffectTargetIssueIds(effect).some((id) => staleTargetIdSet.has(id)),
            );
            const disabled = busy || requiredUnmet || blockedStale;
            const cancelTree = cancelTreeEffect(option);
            const confirming = confirmOptionId === option.id;
            const previewRows = cancelTree && cancelTreePreview ? cancelTreePreview(cancelTree.targetIssueId) : null;
            const confirmRef = cancelTree ? resolveIssue(cancelTree.targetIssueId) : null;
            const confirmToken = confirmRef?.identifier ?? confirmRef?.id ?? cancelTree?.targetIssueId ?? "";
            return (
              <div key={option.id} className="space-y-2">
                <button
                  type="button"
                  disabled={disabled}
                  onClick={() => runOption(option)}
                  className={cn(
                    "w-full rounded-sm border px-4 py-3 text-left transition-colors",
                    disabled && "cursor-not-allowed opacity-60",
                    destructive
                      ? "border-[var(--alert)] bg-[var(--alert)]/5 text-[var(--fg)] hover:bg-[var(--alert)]/10"
                      : "border-[var(--edge)] bg-transparent text-[var(--fg)] hover:border-[var(--link)] hover:bg-[var(--link)]/10",
                  )}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className={cn("text-sm font-medium", destructive && "text-[var(--alert)]")}>
                      {option.label}
                    </span>
                    {blockedStale && (
                      <span className="shrink-0 rounded-full border border-[var(--warn)] bg-[var(--warn)]/10 px-2 py-0.5 text-2xs font-medium text-[var(--warn)]">
                        Blocked · stale
                      </span>
                    )}
                  </div>
                  {option.description && (
                    <div className="mt-1 text-sm leading-6 text-[var(--fg-mute)]">{option.description}</div>
                  )}
                  {option.effects.length > 0 && (
                    <ul className="mt-2 space-y-0.5">
                      {option.effects.map((effect, index) => (
                        <li
                          key={index}
                          className={cn(
                            "flex items-start gap-1.5 text-xs",
                            effect.type === "cancel_issue_tree" ? "text-[var(--alert)]" : "text-[var(--fg-mute)]",
                          )}
                        >
                          <ArrowRight className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                          {effectSummary(effect, resolveIssue, snapshots)}
                        </li>
                      ))}
                    </ul>
                  )}
                </button>

                {/* Destructive cancel-tree confirm gate */}
                {confirming && cancelTree && (
                  <div className="rounded-lg border border-[var(--alert)] bg-[var(--alert)]/5 p-3">
                    <div className="flex items-center gap-2 text-sm font-semibold text-[var(--alert)]">
                      <Ban className="h-4 w-4" aria-hidden /> This cancels an entire issue tree
                    </div>
                    {previewRows && previewRows.length > 0 ? (
                      <>
                        <p className="mt-1 text-xs text-[var(--fg-mute)]">
                          {pluralize(previewRows.length, "issue")} will be cancelled:
                        </p>
                        <ul className="mt-1 max-h-40 space-y-0.5 overflow-auto text-xs">
                          {previewRows.map((row) => (
                            <li key={row.id} className="flex items-center gap-1.5">
                              <Ban className="h-3 w-3 shrink-0 text-[var(--alert)]" aria-hidden />
                              <span className="font-medium">{issueLabel(row, row.id)}</span>
                              {row.title && row.identifier && (
                                <span className="truncate text-[var(--fg-mute)]">{row.title}</span>
                              )}
                            </li>
                          ))}
                        </ul>
                      </>
                    ) : (
                      <p className="mt-1 text-xs text-[var(--fg-mute)]">
                        This issue and every sub-issue beneath it will be cancelled.
                      </p>
                    )}
                    <p className="mt-2 text-xs text-[var(--fg-mute)]">
                      Type <span className="font-mono font-medium text-[var(--fg)]">{confirmToken}</span> to confirm.
                    </p>
                    <Input
                      value={confirmText}
                      onChange={(event) => setConfirmText(event.target.value)}
                      placeholder={confirmToken}
                      aria-label="Type the issue identifier to confirm"
                      autoFocus
                      className="mt-1"
                    />
                    <div className="mt-2 flex justify-end gap-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setConfirmOptionId(null);
                          setConfirmText("");
                        }}
                      >
                        Cancel
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        disabled={busy || confirmText.trim() !== confirmToken}
                        onClick={() => onDecide?.(option.id, inputValues)}
                      >
                        {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                        {previewRows ? `Cancel ${pluralize(previewRows.length, "issue")}` : "Cancel tree"}
                      </Button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}

          {/* Always-present zero-effect Dismiss (telemetered "no", distinct from expiry) */}
          {!decision.options.some((option) => option.effects.length === 0) && (
            <div className="flex items-center justify-between gap-2 pt-1">
              <span className="text-xs text-[var(--fg-mute)]">Not now?</span>
              <Button variant="ghost" size="sm" disabled={busy} onClick={() => onDismiss?.()}>
                Dismiss — no effects
              </Button>
            </div>
          )}
          {errorMessage && <p className="text-xs text-[var(--alert)]">{errorMessage}</p>}
        </div>
      )}

      {/* Terminal states */}
      {!open && (
        <div className="mt-3 space-y-2">
          {decision.status === "expired" && (
            <div className={NOTE}>
              <div className="flex items-center gap-2 font-medium text-[var(--fg)]">
                <Clock className="h-4 w-4" aria-hidden /> The decision window closed
              </div>
              <p className="mt-1">
                {expiredReason === "target_gone"
                  ? "A target issue was cancelled before this was decided."
                  : expiredReason === "target_completed"
                    ? "All target issues were completed before this was decided."
                    : "No response before the expiry deadline."}
                {decision.continuationPolicy === "wake_origin_agent" && " The proposer was re-woken."}
              </p>
            </div>
          )}
          {decision.status === "cancelled" && (
            <p className={NOTE}>This decision was withdrawn by the proposer before a response.</p>
          )}
          {decision.status === "decided" && dismissed && (
            <p className={NOTE}>Dismissed — no effects were run.</p>
          )}
          {decision.status === "decided" && !dismissed && (executions ?? []).length > 0 && (
            <>
              <ul className="space-y-1.5">
                {(executions ?? []).map((execution) => {
                  const row = executionRow(execution, resolveIssue);
                  return (
                    <li key={row.key} className="flex items-start gap-2">
                      {RESULT_ICON[row.status]}
                      <span className="min-w-0 flex-1 text-sm text-[var(--fg)]">{row.summary}</span>
                      <IssueLink link={row.link} />
                    </li>
                  );
                })}
              </ul>
              {decision.executionStatus !== "succeeded" && (
                <p className="text-xs text-[var(--fg-mute)]">
                  Some effects may already have been applied. Review the results before asking the proposer to re-propose.
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
