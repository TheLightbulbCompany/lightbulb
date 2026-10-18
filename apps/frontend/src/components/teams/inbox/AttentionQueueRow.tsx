// apps/frontend/src/components/teams/inbox/AttentionQueueRow.tsx

// Ported from upstream Paperclip's AttentionQueueRow
// (paperclip/ui/src/components/AttentionQueueRow.tsx @ v2026.1012.0-isol8.4)
// (MIT, (c) 2025 Paperclip AI). The eyebrow (kind, task key, decide-by + "set
// by"), title + `detailLine ?? whyNow`, the compact verbs and their order,
// See more / See less, Open, Restore, the overflow menu (Snooze presets +
// custom, Dismiss, Open source), "Reappears in …" and the inline resolvers'
// verb order are verbatim, except where listed.
//
// Adaptations vs upstream (only what had to change):
//   - @paperclipai/shared types -> components/teams/shared/types
//   - react-query mutations     -> useApprovalMutations / useInteractionMutations
//                                  (the BFF routes the rest of Teams calls), then a
//                                  mutate over every /teams/attention key
//   - router Link               -> shell URLs (shellUrlFromPaperclipPath, else the
//                                  task's teamsDetailUrl) + handleShellLinkClick
//   - pushToast                 -> notifySuccess / notifyApiError
//   - StatusGlyph               -> StatusIcon (same blocked / in_review statuses)
//   - approval Reject           -> the BFF requires a reason, so the compact Reject
//                                  opens the row (as upstream's interaction Reject
//                                  does) and the expanded Reject needs the note
//   - AttentionInteractionResolver -> the BFF-hydrated `item.interaction` rendered
//                                  straight into IssueThreadInteractionCard (no
//                                  per-issue interactions read)
//   - not ported: DecisionTriageStrip (decision queues are not proxied), join-request
//     and stalled-review verbs (no BFF route: those rows show Open), screenshot
//     thumbnails / gallery (/api/assets not proxied), memo + keyboard
//     selection ring, and the Collapsible height animation (plain conditional render)
//   - Button / Textarea / palette classes -> editorial-btn, for-you-form__field, tokens

"use client";

import { useState, type KeyboardEvent, type ReactNode } from "react";
import { AlarmClock, CalendarClock, ChevronDown, ChevronUp, ExternalLink, Loader2, MoreHorizontal, RotateCcw, X } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { handleShellLinkClick, shellUrlFromPaperclipPath, teamsDetailUrl } from "@/lib/shellNav";
import { notifyApiError, notifySuccess } from "@/lib/notify";
import { StatusIcon } from "@/components/teams/shared/components/StatusIcon";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { AttachmentIssueContext } from "@/components/teams/shared/lib/attachments";
import { DecisionResolver } from "@/components/teams/inbox/DecisionResolver";
import { IssueThreadInteractionCard } from "@/components/teams/issues/IssueThreadInteractionCard";
import { InteractionAudienceLine } from "@/components/teams/issues/InteractionAudienceLine";
import { describeAttentionResolverAudience } from "@/components/teams/issues/lib/interaction-audience";
import { useInteractionMutations } from "@/components/teams/issues/hooks/useInteractionMutations";
import { useApprovalMutations } from "@/components/teams/inbox/hooks/useApprovalMutations";
import { isAttentionKey } from "@/components/teams/inbox/hooks/useAttentionFeed";
import {
  attentionDetailLine,
  attentionStatus,
  attentionTaskRef,
  decideByLabel,
  isInlineResolvable,
  sourceLabel,
} from "@/components/teams/inbox/lib/attention";
import type { AttentionDecisionVerb, AttentionItem, CompanyAgent } from "@/components/teams/shared/types";

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;

const ACTION_BTN = "editorial-btn editorial-btn--sm";

function tomorrowMorningIso(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(9, 0, 0, 0);
  return d.toISOString();
}

const SNOOZE_PRESETS: ReadonlyArray<{ label: string; resolve: () => string }> = [
  { label: "1 hour", resolve: () => new Date(Date.now() + HOUR_MS).toISOString() },
  { label: "4 hours", resolve: () => new Date(Date.now() + 4 * HOUR_MS).toISOString() },
  { label: "Tomorrow morning", resolve: tomorrowMorningIso },
  { label: "Next week", resolve: () => new Date(Date.now() + 7 * DAY_MS).toISOString() },
];

/** The in-shell place a row opens: its subject's page, else its task. */
export function attentionShellUrl(item: AttentionItem): string | null {
  const url = shellUrlFromPaperclipPath(item.subject.href);
  if (url) return url;
  const task = attentionTaskRef(item);
  return task ? teamsDetailUrl("issue", task.id) : null;
}

function ShellLink({ url, className, children }: { url: string; className?: string; children: ReactNode }) {
  return (
    <a href={url} className={className} onClick={(e) => { e.stopPropagation(); handleShellLinkClick(e, url); }}>
      {children}
    </a>
  );
}

export interface AttentionQueueRowProps {
  item: AttentionItem;
  expanded: boolean;
  onToggleExpand: (item: AttentionItem) => void;
  onDismiss: (item: AttentionItem) => void;
  onSnooze?: (item: AttentionItem, snoozedUntil: string) => void;
  /** Restore a snoozed/dismissed row (curtain variant only). */
  onRestore?: (item: AttentionItem) => void;
  /** "active" renders the live desk row; "hidden" renders a curtain row. */
  variant?: "active" | "hidden";
  agentMap?: Map<string, CompanyAgent>;
  currentUserId?: string | null;
}

export function AttentionQueueRow({
  item,
  expanded,
  onToggleExpand,
  onDismiss,
  onSnooze,
  onRestore,
  variant = "active",
  agentMap,
  currentUserId,
}: AttentionQueueRowProps) {
  const label = sourceLabel(item.sourceKind);
  const taskRef = attentionTaskRef(item);
  const taskUrl = taskRef ? teamsDetailUrl("issue", taskRef.id) : null;
  const isHidden = variant === "hidden";
  const inline = !isHidden && isInlineResolvable(item);
  const href = attentionShellUrl(item);
  const snoozedUntil = item.dismissal?.kind === "snooze" ? item.dismissal.snoozedUntil : null;
  const detailLine = attentionDetailLine(item) ?? item.whyNow;
  const expandable = inline;
  const activate = () => {
    if (expandable) onToggleExpand(item);
  };

  const compactActions = !isHidden ? collectCompactActions(item) : [];
  // Who the server will let resolve this interaction; null for every other
  // source and for a feed built before the metadata existed.
  const audience = describeAttentionResolverAudience(item);
  const showOpen = !inline && !!href;
  const showRestore = isHidden && !!onRestore;
  const provenance = decideByProvenance(item);

  const toggle = expandable ? (
    <button
      type="button"
      className="inline-flex shrink-0 items-center gap-1 text-caption font-medium text-[var(--fg-mute)] hover:text-[var(--fg)]"
      aria-label={expanded ? "Collapse decision" : "Expand decision"}
      aria-expanded={expanded}
      onClick={activate}
    >
      {expanded ? <ChevronUp size={16} aria-hidden="true" /> : <ChevronDown size={16} aria-hidden="true" />}
      {expanded ? "See less" : "See more"}
    </button>
  ) : null;

  const renderFooter = ({ compact }: { compact: boolean }) => {
    const showCompact = compactActions.length > 0 && (compact || !expanded);
    if (!toggle && !showCompact && !showOpen && !showRestore) return null;
    return (
      <div className="flex flex-wrap items-center justify-between gap-2" data-attention-actions="true">
        {toggle ?? <span />}
        <div className="flex flex-wrap items-center gap-2">
          {showCompact && <CompactDecisionActions item={item} onOpen={() => onToggleExpand(item)} />}
          {showOpen && (
            <ShellLink url={href!} className={cn(ACTION_BTN, "editorial-btn--primary inline-flex items-center gap-1")}>
              Open
              <ExternalLink size={12} aria-hidden="true" />
            </ShellLink>
          )}
          {showRestore && (
            <button
              type="button"
              className={cn(ACTION_BTN, "editorial-btn--secondary inline-flex items-center gap-1")}
              onClick={() => onRestore!(item)}
            >
              <RotateCcw size={12} aria-hidden="true" />
              Restore
            </button>
          )}
        </div>
      </div>
    );
  };

  return (
    <div
      className={cn(
        "editorial-card relative flex flex-col gap-4 px-4 pb-4 pt-3",
        isHidden && "opacity-80 hover:opacity-100",
      )}
      id={`attention-row-${item.id}`}
      data-attention-row
      data-attention-row-id={item.id}
      data-attention-source={item.sourceKind}
      data-attention-severity={item.severity}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-1 text-caption text-[var(--fg-mute)]">
          <span className="inline-flex items-center gap-1 font-medium">
            <StatusIcon status={attentionStatus(item)} />
            {label}
          </span>
          {taskRef && (
            <>
              <EyebrowSeparator />
              {taskUrl ? (
                <ShellLink url={taskUrl} className="font-mono hover:text-[var(--fg)]">
                  {taskRef.identifier}
                </ShellLink>
              ) : (
                <span className="font-mono">{taskRef.identifier}</span>
              )}
            </>
          )}
          {item.decideBy && (
            <>
              <EyebrowSeparator />
              <span
                className="inline-flex items-center gap-1"
                data-attention-decide-by={item.decideBy}
                title={provenance ? `Set by ${provenance}` : undefined}
              >
                <CalendarClock size={12} aria-hidden="true" />
                {decideByLabel(item.decideBy)}
                {provenance && <span>· set by {provenance}</span>}
              </span>
            </>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-1 text-caption text-[var(--fg-mute)]" data-attention-menu="true">
          {isHidden && snoozedUntil ? (
            <span title={`Reappears ${new Date(snoozedUntil).toLocaleString()}`}>Reappears {reappearLabel(snoozedUntil)}</span>
          ) : (
            <span>{timeAgo(item.activityAt)}</span>
          )}
          {!isHidden && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button type="button" className="rounded-sm p-0.5 hover:text-[var(--fg)]" aria-label="Row actions">
                  <MoreHorizontal size={16} aria-hidden="true" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {onSnooze && <SnoozeSubmenu onSnooze={(iso) => onSnooze(item, iso)} />}
                <DropdownMenuItem onClick={() => onDismiss(item)}>
                  <X size={16} aria-hidden="true" />
                  Dismiss
                </DropdownMenuItem>
                {href && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem asChild>
                      <ShellLink url={href}>Open source</ShellLink>
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </div>

      <div
        className={cn("min-w-0 rounded-md", expandable && "cursor-pointer")}
        {...(expandable
          ? {
              role: "button",
              tabIndex: 0,
              "aria-expanded": expanded,
              "aria-label": expanded ? "Collapse decision" : "Expand decision",
              onClick: activate,
              onKeyDown: (e: KeyboardEvent) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onToggleExpand(item);
                }
              },
            }
          : {})}
      >
        <span className="line-clamp-2 text-ui font-medium text-[var(--fg)]" title={item.subject.title ?? undefined}>
          {item.subject.title ?? label}
        </span>
        <p className="mt-0.5 line-clamp-2 text-caption text-[var(--fg-mute)]">{detailLine}</p>
      </div>

      {/* The audience reads before the verbs it qualifies. */}
      {inline && !expanded && audience && <InteractionAudienceLine audience={audience} variant="compact" />}
      {inline && !expanded && renderFooter({ compact: true })}

      {inline && expanded && (
        <InlineResolver item={item} agentMap={agentMap} currentUserId={currentUserId} toggle={toggle} />
      )}

      {!inline && renderFooter({ compact: false })}
    </div>
  );
}

function EyebrowSeparator() {
  return <span aria-hidden="true">·</span>;
}

type CompactDecisionAction = "accept" | "approve" | "reject" | "request_revision";

function compactDecisionAction(item: AttentionItem, verbId: string): CompactDecisionAction | null {
  if (item.sourceKind === "approval" && (verbId === "approve" || verbId === "reject" || verbId === "request_revision")) {
    return verbId;
  }
  if (
    item.sourceKind === "issue_thread_interaction"
    && (item.interaction?.kind ?? item.subject.metadata?.kind) === "request_confirmation"
    && (verbId === "accept" || verbId === "reject")
  ) {
    return verbId;
  }
  return null;
}

const VERB_ORDER: Record<"outline" | "destructive" | "default", number> = { outline: 0, destructive: 1, default: 2 };

interface CompactAction {
  action: CompactDecisionAction;
  label: string;
  id: string;
  description: string;
}

function collectCompactActions(item: AttentionItem): CompactAction[] {
  if (!isInlineResolvable(item)) return [];
  return (item.decisionVerbs ?? [])
    .slice(0, 3)
    .flatMap((verb) => {
      const action = compactDecisionAction(item, verb.id);
      return action ? [{ action, label: verb.label, id: verb.id, description: verb.description ?? "" }] : [];
    })
    .sort((a, b) => VERB_ORDER[decisionVerbVariant(a)] - VERB_ORDER[decisionVerbVariant(b)]);
}

function decisionVerbVariant(verb: Pick<AttentionDecisionVerb, "label"> & { description: string | null }): "default" | "outline" | "destructive" {
  const text = `${verb.label} ${verb.description ?? ""}`.toLowerCase();
  if (/\b(reject|decline|deny|delete|remove)\b/.test(text)) return "destructive";
  if (/\b(accept|approve|confirm|apply)\b/.test(text)) return "default";
  return "outline";
}

const VARIANT_CLASS = {
  default: "editorial-btn--primary",
  destructive: "editorial-btn--destructive",
  outline: "editorial-btn--secondary",
} as const;

function decisionLabel(action: CompactDecisionAction): string {
  if (action === "request_revision") return "sent for revision";
  if (action === "accept" || action === "approve") return "approved";
  return "rejected";
}

function compactDecisionSuccessLabel(sourceKind: AttentionItem["sourceKind"], action: CompactDecisionAction): string {
  if (sourceKind === "approval") return `Approval ${decisionLabel(action)}`;
  return action === "accept" ? "Confirmation accepted" : "Confirmation declined";
}

function CompactDecisionActions({ item, onOpen }: { item: AttentionItem; onOpen: () => void }) {
  const { mutate } = useTeamsApi();
  const approvals = useApprovalMutations();
  const interactions = useInteractionMutations();
  const [pending, setPending] = useState<CompactDecisionAction | null>(null);
  const actions = collectCompactActions(item);

  const run = async (action: CompactDecisionAction) => {
    setPending(action);
    try {
      if (item.sourceKind === "approval") {
        const id = item.approval?.id ?? item.subject.id;
        if (action === "approve") await approvals.approve(id);
        else await approvals.requestRevision(id);
      } else {
        const interaction = item.interaction!;
        await interactions.accept(interaction.issueId, interaction.id, {});
      }
      void mutate(isAttentionKey);
      notifySuccess(compactDecisionSuccessLabel(item.sourceKind, action));
    } catch (error) {
      notifyApiError(error, `Could not ${decisionLabel(action)}`);
    } finally {
      setPending(null);
    }
  };

  if (actions.length === 0) return null;

  return (
    <div className="flex flex-wrap items-center gap-2" aria-label="Decision actions">
      {actions.map(({ action, id, label, description }) => (
        <button
          key={id}
          type="button"
          className={cn(ACTION_BTN, VARIANT_CLASS[decisionVerbVariant({ label, description })], "inline-flex items-center gap-1")}
          disabled={pending !== null}
          onClick={(event) => {
            event.stopPropagation();
            // A reject needs words (the BFF requires an approval's reason; an
            // interaction's decline takes one): open the row for them.
            if (action === "reject") {
              onOpen();
              return;
            }
            void run(action);
          }}
        >
          {pending === action && <Loader2 size={12} className="animate-spin" aria-hidden="true" />}
          {label}
        </button>
      ))}
    </div>
  );
}

function SnoozeSubmenu({ onSnooze }: { onSnooze: (snoozedUntil: string) => void }) {
  const [customValue, setCustomValue] = useState("");
  const applyCustom = () => {
    if (!customValue) return;
    const ts = new Date(customValue);
    if (Number.isNaN(ts.getTime())) return;
    onSnooze(ts.toISOString());
  };
  return (
    <DropdownMenuSub>
      <DropdownMenuSubTrigger>
        <AlarmClock size={16} aria-hidden="true" />
        Snooze
      </DropdownMenuSubTrigger>
      <DropdownMenuSubContent>
        {SNOOZE_PRESETS.map((preset) => (
          <DropdownMenuItem key={preset.label} onClick={() => onSnooze(preset.resolve())}>
            {preset.label}
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <div className="flex flex-col gap-1.5 px-2 py-1.5" onKeyDown={(e) => e.stopPropagation()} onClick={(e) => e.stopPropagation()}>
          <span className="label-caps text-[var(--fg-mute)]">Custom</span>
          <input
            type="datetime-local"
            aria-label="Snooze until"
            value={customValue}
            onChange={(e) => setCustomValue(e.target.value)}
            className="for-you-form__field"
          />
          <button type="button" className={cn(ACTION_BTN, "editorial-btn--primary")} disabled={!customValue} onClick={applyCustom}>
            Snooze until…
          </button>
        </div>
      </DropdownMenuSubContent>
    </DropdownMenuSub>
  );
}

function decideByProvenance(item: AttentionItem): string | null {
  const attribution = item.decideByAttribution;
  if (!attribution) return null;
  if (attribution.type === "agent") return attribution.agentName ?? "an agent";
  return "you";
}

/** Compact "when does this snooze end" label, e.g. `in 2h`, `in 3d`. */
function reappearLabel(snoozedUntil: string): string {
  const diffMs = new Date(snoozedUntil).getTime() - Date.now();
  if (!Number.isFinite(diffMs) || diffMs <= 0) return "soon";
  const diffMin = Math.round(diffMs / 60000);
  if (diffMin < 60) return `in ${diffMin}m`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `in ${diffHr}h`;
  return `in ${Math.round(diffHr / 24)}d`;
}

function InlineResolver({
  item,
  agentMap,
  currentUserId,
  toggle,
}: {
  item: AttentionItem;
  agentMap?: Map<string, CompanyAgent>;
  currentUserId?: string | null;
  toggle: ReactNode;
}) {
  if (item.sourceKind === "decision") {
    return (
      <>
        <DecisionResolver
          decisionId={item.subject.id}
          originIssue={item.relatedIssue}
          agentMap={agentMap}
          originAgentName={item.originAgentName}
        />
        {toggle && <div className="flex items-center">{toggle}</div>}
      </>
    );
  }
  if (item.sourceKind === "issue_thread_interaction" && item.interaction) {
    return (
      <>
        <InteractionResolver item={item} agentMap={agentMap} currentUserId={currentUserId} />
        {toggle && <div className="flex items-center">{toggle}</div>}
      </>
    );
  }
  if (item.sourceKind === "approval") return <ApprovalResolver item={item} toggle={toggle} />;
  return null;
}

function InteractionResolver({
  item,
  agentMap,
  currentUserId,
}: {
  item: AttentionItem;
  agentMap?: Map<string, CompanyAgent>;
  currentUserId?: string | null;
}) {
  const { mutate } = useTeamsApi();
  const { accept, reject, respond, cancel } = useInteractionMutations();
  const interaction = item.interaction!;
  const refresh = () => void mutate(isAttentionKey);
  return (
    <AttachmentIssueContext.Provider value={interaction.issueId}>
      <IssueThreadInteractionCard
        interaction={interaction}
        agentMap={agentMap}
        currentUserId={currentUserId}
        onAcceptInteraction={async (target, selectedClientKeys, selectedOptionIds) => {
          const body: Record<string, unknown> = {};
          if (selectedClientKeys) body.selected_client_keys = selectedClientKeys;
          if (selectedOptionIds) body.selected_option_ids = selectedOptionIds;
          await accept(target.issueId, target.id, body);
          refresh();
        }}
        onRejectInteraction={async (target, reason) => {
          await reject(target.issueId, target.id, reason);
          refresh();
        }}
        onSubmitInteractionAnswers={async (target, answers) => {
          await respond(target.issueId, target.id, { answers });
          refresh();
        }}
        onCancelInteraction={async (target) => {
          await cancel(target.issueId, target.id);
          refresh();
        }}
      />
    </AttachmentIssueContext.Provider>
  );
}

function ApprovalResolver({ item, toggle }: { item: AttentionItem; toggle: ReactNode }) {
  const { mutate } = useTeamsApi();
  const { approve, reject, requestRevision } = useApprovalMutations();
  const [note, setNote] = useState("");
  const [pending, setPending] = useState<"approve" | "reject" | "revise" | null>(null);
  const id = item.approval?.id ?? item.subject.id;
  const trimmed = note.trim();

  const run = async (which: "approve" | "reject" | "revise") => {
    setPending(which);
    try {
      if (which === "approve") await approve(id, trimmed || undefined);
      else if (which === "reject") await reject(id, trimmed);
      else await requestRevision(id, trimmed || undefined);
      void mutate(isAttentionKey);
    } catch (error) {
      notifyApiError(error, "Couldn't record that decision");
    } finally {
      setPending(null);
    }
  };
  const spinner = (which: typeof pending) =>
    pending === which ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : null;

  // Verb order matches the collapsed row (revise -> reject -> approve).
  return (
    <>
      <textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Optional decision note…"
        aria-label="Decision note"
        rows={2}
        className="for-you-form__field min-h-16"
      />
      <div className="flex flex-wrap items-center justify-between gap-2" data-attention-actions="true">
        {toggle ?? <span />}
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className={cn(ACTION_BTN, "editorial-btn--secondary inline-flex items-center gap-1")}
            onClick={() => void run("revise")} disabled={pending !== null}>
            {spinner("revise")}Request revision
          </button>
          <button type="button" className={cn(ACTION_BTN, "editorial-btn--destructive inline-flex items-center gap-1")}
            onClick={() => void run("reject")} disabled={pending !== null || !trimmed}
            title={trimmed ? undefined : "Add a note to reject"}>
            {spinner("reject")}Reject
          </button>
          <button type="button" className={cn(ACTION_BTN, "editorial-btn--primary inline-flex items-center gap-1")}
            onClick={() => void run("approve")} disabled={pending !== null}>
            {spinner("approve")}Approve
          </button>
        </div>
      </div>
    </>
  );
}
