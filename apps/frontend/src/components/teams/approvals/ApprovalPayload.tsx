// apps/frontend/src/components/teams/approvals/ApprovalPayload.tsx

// Ported from upstream Paperclip's ApprovalPayload
// (paperclip/ui/src/components/ApprovalPayload.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). The four typed payload renderers and the
// renderer switch are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - typeLabel / approvalSubject / approvalLabel -> already ported in
//                                       inbox/lib/approvalLabel (one copy, which
//                                       also labels our own cards)
//   - MarkdownBody                    -> shared MarkdownView
//   - formatCents                     -> local (the lib helper is not in this app)
//   - amber palette / CSS-var utilities -> brand warn token, label-caps, text-caption
//   - the switch gains ONE branch ahead of upstream's: Lightbulb's own cards
//     (Stripe, provisioning consent / connect / information, payment setup —
//     marked by `payload.isol8_action`) render through the inbox ApprovalRow,
//     which owns their hand-off flows. Paperclip files those as
//     request_board_approval, so without the branch they would fall into
//     BoardApprovalPayload and lose every action.

import { UserPlus, Lightbulb, ShieldAlert, ShieldCheck } from "lucide-react";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { ApprovalRow } from "@/components/teams/inbox/ApprovalRow";
import type { Approval } from "@/components/teams/shared/types";

export { approvalLabel, approvalSubject } from "@/components/teams/inbox/lib/approvalLabel";

function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

function MarkdownBody({ className, children }: { className?: string; children: string }) {
  return (
    <div className={className}>
      <MarkdownView>{children}</MarkdownView>
    </div>
  );
}

function firstNonEmptyString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim().length > 0) {
      return value.trim();
    }
  }
  return null;
}

export const typeIcon: Record<string, typeof UserPlus> = {
  hire_agent: UserPlus,
  approve_ceo_strategy: Lightbulb,
  budget_override_required: ShieldAlert,
  request_board_approval: ShieldCheck,
};

export const defaultTypeIcon = ShieldCheck;

/** True for a card Lightbulb files itself (see the header). */
export function isLightbulbActionApproval(payload?: Record<string, unknown> | null): boolean {
  return typeof payload?.isol8_action === "string";
}

function PayloadField({ label, value }: { label: string; value: unknown }) {
  if (!value) return null;
  return (
    <div className="flex items-center gap-2">
      <span className="text-muted-foreground w-20 sm:w-24 shrink-0 text-caption">{label}</span>
      <span>{String(value)}</span>
    </div>
  );
}

function SkillList({ values }: { values: unknown }) {
  if (!Array.isArray(values)) return null;
  const items = values
    .filter((value): value is string => typeof value === "string")
    .map((value) => value.trim())
    .filter(Boolean);
  if (items.length === 0) return null;

  return (
    <div className="flex items-start gap-2">
      <span className="text-muted-foreground w-20 sm:w-24 shrink-0 text-caption pt-0.5">Skills</span>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item) => (
          <span
            key={item}
            className="rounded bg-muted px-1.5 py-0.5 font-mono text-caption text-muted-foreground"
          >
            {item}
          </span>
        ))}
      </div>
    </div>
  );
}

export function HireAgentPayload({ payload }: { payload: Record<string, unknown> }) {
  return (
    <div className="mt-3 space-y-1.5 text-ui">
      <div className="flex items-center gap-2">
        <span className="text-muted-foreground w-20 sm:w-24 shrink-0 text-caption">Name</span>
        <span className="font-medium">{String(payload.name ?? "—")}</span>
      </div>
      <PayloadField label="Role" value={payload.role} />
      <PayloadField label="Title" value={payload.title} />
      <PayloadField label="Icon" value={payload.icon} />
      {!!payload.capabilities && (
        <div className="flex items-start gap-2">
          <span className="text-muted-foreground w-20 sm:w-24 shrink-0 text-caption pt-0.5">Capabilities</span>
          <span className="text-muted-foreground">{String(payload.capabilities)}</span>
        </div>
      )}
      {!!payload.adapterType && (
        <div className="flex items-center gap-2">
          <span className="text-muted-foreground w-20 sm:w-24 shrink-0 text-caption">Adapter</span>
          <span className="font-mono text-caption bg-muted px-1.5 py-0.5 rounded">
            {String(payload.adapterType)}
          </span>
        </div>
      )}
      <SkillList values={payload.desiredSkills} />
    </div>
  );
}

export function CeoStrategyPayload({ payload }: { payload: Record<string, unknown> }) {
  const plan = payload.plan ?? payload.description ?? payload.strategy ?? payload.text;
  return (
    <div className="mt-3 space-y-1.5 text-ui">
      <PayloadField label="Title" value={payload.title} />
      {!!plan && (
        <div className="mt-2 rounded-md bg-muted/40 px-3 py-2 text-ui text-muted-foreground whitespace-pre-wrap font-mono text-caption max-h-48 overflow-y-auto">
          {String(plan)}
        </div>
      )}
      {!plan && (
        <pre className="mt-2 rounded-md bg-muted/40 px-3 py-2 text-caption text-muted-foreground overflow-x-auto max-h-48">
          {JSON.stringify(payload, null, 2)}
        </pre>
      )}
    </div>
  );
}

export function BudgetOverridePayload({ payload }: { payload: Record<string, unknown> }) {
  const budgetAmount = typeof payload.budgetAmount === "number" ? payload.budgetAmount : null;
  const observedAmount = typeof payload.observedAmount === "number" ? payload.observedAmount : null;
  return (
    <div className="mt-3 space-y-1.5 text-ui">
      <PayloadField label="Scope" value={payload.scopeName ?? payload.scopeType} />
      <PayloadField label="Window" value={payload.windowKind} />
      <PayloadField label="Metric" value={payload.metric} />
      {(budgetAmount !== null || observedAmount !== null) ? (
        <div className="rounded-md bg-muted/40 px-3 py-2 text-caption text-muted-foreground">
          Limit {budgetAmount !== null ? formatCents(budgetAmount) : "—"} · Observed {observedAmount !== null ? formatCents(observedAmount) : "—"}
        </div>
      ) : null}
      {!!payload.guidance && (
        <p className="text-muted-foreground">{String(payload.guidance)}</p>
      )}
    </div>
  );
}

export function BoardApprovalPayload({
  payload,
  hideTitle = false,
}: {
  payload: Record<string, unknown>;
  hideTitle?: boolean;
}) {
  const nextPayload = hideTitle ? { ...payload, title: undefined } : payload;
  return (
    <BoardApprovalPayloadContent payload={nextPayload} />
  );
}

/**
 * Risks render inside a custom bullet row, so a leading markdown list marker
 * would nest a second bullet inside the first. Strip one leading marker.
 */
function stripLeadingListMarker(value: string): string {
  return value.replace(/^(?:[-*•]|\d+[.)])\s+/, "");
}

function BoardApprovalPayloadContent({ payload }: { payload: Record<string, unknown> }) {
  const risks = Array.isArray(payload.risks)
    ? payload.risks
        .filter((value): value is string => typeof value === "string")
        .map((value) => stripLeadingListMarker(value.trim()))
        .filter(Boolean)
    : [];
  const title = firstNonEmptyString(payload.title);
  const summary = firstNonEmptyString(payload.summary);
  const recommendedAction = firstNonEmptyString(payload.recommendedAction);
  const nextActionOnApproval = firstNonEmptyString(payload.nextActionOnApproval);
  const proposedComment = firstNonEmptyString(payload.proposedComment);

  return (
    <div className="mt-4 space-y-3.5 text-ui">
      {title && (
        <div className="space-y-1">
          <p className="label-caps text-muted-foreground">Title</p>
          <p className="font-medium leading-6 text-foreground">{title}</p>
        </div>
      )}
      {summary && (
        <div className="space-y-1">
          <p className="label-caps text-muted-foreground">Summary</p>
          <MarkdownBody className="leading-6 text-foreground/90">{summary}</MarkdownBody>
        </div>
      )}
      {recommendedAction && (
        <div className="rounded-lg border border-warn/20 bg-warn/10 px-3.5 py-3">
          <p className="label-caps text-warn">
            Recommended action
          </p>
          <MarkdownBody className="mt-1 leading-6 text-foreground">{recommendedAction}</MarkdownBody>
        </div>
      )}
      {nextActionOnApproval && (
        <div className="rounded-lg border border-border/60 bg-background/60 px-3.5 py-3">
          <p className="label-caps text-muted-foreground">On approval</p>
          <MarkdownBody className="mt-1 leading-6 text-foreground">{nextActionOnApproval}</MarkdownBody>
        </div>
      )}
      {risks.length > 0 && (
        <div className="space-y-1.5">
          <p className="label-caps text-muted-foreground">Risks</p>
          <ul className="space-y-1 text-ui text-muted-foreground">
            {risks.map((risk, index) => (
              <li key={`${index}-${risk}`} className="flex items-start gap-2">
                <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-muted-foreground/60" />
                <MarkdownBody className="leading-6">{risk}</MarkdownBody>
              </li>
            ))}
          </ul>
        </div>
      )}
      {proposedComment && (
        <div className="space-y-1.5">
          <p className="label-caps text-muted-foreground">
            Proposed comment
          </p>
          <pre className="max-h-48 overflow-auto rounded-lg border border-border/60 bg-muted/50 px-3.5 py-3 font-mono text-caption leading-5 text-muted-foreground whitespace-pre-wrap">
            {proposedComment}
          </pre>
        </div>
      )}
    </div>
  );
}

export function ApprovalPayloadRenderer({
  type,
  payload,
  hidePrimaryTitle = false,
  approval,
}: {
  type: string;
  payload: Record<string, unknown>;
  hidePrimaryTitle?: boolean;
  /** Lightbulb's cards need the whole row (id + status), not just the payload. */
  approval?: Approval;
}) {
  if (approval && isLightbulbActionApproval(payload)) {
    return (
      <ul className="mt-3 -mx-3">
        <ApprovalRow approval={approval} showDetailLink={false} />
      </ul>
    );
  }
  if (type === "hire_agent") return <HireAgentPayload payload={payload} />;
  if (type === "budget_override_required") return <BudgetOverridePayload payload={payload} />;
  if (type === "request_board_approval") {
    return <BoardApprovalPayload payload={payload} hideTitle={hidePrimaryTitle} />;
  }
  return <CeoStrategyPayload payload={payload} />;
}
