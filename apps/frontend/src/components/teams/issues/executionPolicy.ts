// Ported from upstream Paperclip's lib/issue-execution-policy.ts
// (paperclip/ui/src/lib/issue-execution-policy.ts) (MIT, (c) 2025 Paperclip AI).
//
// An issue's executionPolicy bundles three independent sub-fields — `monitor`,
// the reviewer `stages`, and the approver `stages` — but the PATCH wire has
// REPLACE semantics: Paperclip overwrites the whole executionPolicy with what
// the BFF forwards. Patching one sub-field in isolation (e.g. sending just
// `{ monitor }`) would therefore drop the others. Every edit must rebuild the
// FULL desired policy from the issue's current one, preserving the sub-fields
// it is not touching. These helpers are the single place that read-modify-write
// happens, so the merge invariant lives in exactly one spot.

import type {
  IssueExecutionPolicy,
  IssueExecutionStage,
  IssueExecutionStageParticipant,
  IssueMonitorPolicy,
} from "@/components/teams/shared/types";

/** A stage participant encoded as a single selection string for the
 * multi-select picker. Agents are `agent:<id>`, humans are `user:<id>` — the
 * same encoding the AssigneePicker option ids use, so the two surfaces speak
 * the same language. */
export type ParticipantValue = string;

export type StageType = "review" | "approval";

function newId(): string {
  // crypto.randomUUID is available in every browser the app targets and in the
  // jsdom test runner; the fallback only guards exotic/SSR contexts.
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `stage-${Math.random().toString(36).slice(2)}`;
}

function participantKey(participant: IssueExecutionStageParticipant): string {
  return participant.type === "agent"
    ? `agent:${participant.agentId}`
    : `user:${participant.userId}`;
}

/** Decode a `agent:<id>` / `user:<id>` selection string into a participant.
 * Emits `null` for the unused id field so the shape satisfies upstream's zod
 * superRefine (agent participants must not carry a userId and vice versa). */
export function participantFromValue(
  value: ParticipantValue,
): IssueExecutionStageParticipant | null {
  if (value.startsWith("agent:")) {
    return { type: "agent", agentId: value.slice("agent:".length), userId: null };
  }
  if (value.startsWith("user:")) {
    return { type: "user", userId: value.slice("user:".length), agentId: null };
  }
  return null;
}

export function valueFromParticipant(
  participant: IssueExecutionStageParticipant,
): ParticipantValue {
  return participantKey(participant);
}

/** The current selection values for a stage type, read off the issue's policy.
 * Drives both the picker's checked state and the read-only display chips. */
export function stageParticipantValues(
  policy: IssueExecutionPolicy | null | undefined,
  stageType: StageType,
): ParticipantValue[] {
  const stage = policy?.stages?.find((candidate) => candidate.type === stageType);
  return stage?.participants.map(valueFromParticipant) ?? [];
}

/** Reconcile a desired set of selection values against the stage's existing
 * participants, preserving each participant's stable `id` when it survives the
 * edit (so re-saving an unchanged reviewer keeps its identity rather than
 * minting a new one). */
function mergeParticipants(
  existing: IssueExecutionStageParticipant[] | undefined,
  values: ParticipantValue[],
): IssueExecutionStageParticipant[] {
  const existingByKey = new Map(
    (existing ?? []).map((participant) => [participantKey(participant), participant]),
  );
  const participants: IssueExecutionStageParticipant[] = [];
  for (const value of values) {
    const principal = participantFromValue(value);
    if (!principal) continue;
    const key = participantKey(principal);
    const previous = existingByKey.get(key);
    participants.push({
      id: previous?.id ?? newId(),
      type: principal.type,
      agentId: principal.type === "agent" ? principal.agentId ?? null : null,
      userId: principal.type === "user" ? principal.userId ?? null : null,
    });
  }
  return participants;
}

/** Build a stage of one type from the existing policy + the desired values.
 * Returns `null` when the desired set is empty — an empty stage is never
 * emitted (upstream treats "no reviewers" as "no review stage at all"). */
function buildStage(
  existingPolicy: IssueExecutionPolicy | null | undefined,
  stageType: StageType,
  values: ParticipantValue[],
): IssueExecutionStage | null {
  const existingStage = existingPolicy?.stages?.find((stage) => stage.type === stageType);
  const participants = mergeParticipants(existingStage?.participants, values);
  if (participants.length === 0) return null;
  return {
    id: existingStage?.id ?? newId(),
    type: stageType,
    approvalsNeeded: 1,
    participants,
  };
}

/** The monitor subset the rail editor commits (matches MonitorRow's payload). */
type MonitorEdit = Pick<IssueMonitorPolicy, "nextCheckAt" | "notes" | "serviceName">;

/** A single sub-field edit to apply to the existing policy. Exactly one of
 * `monitor` / `reviewers` / `approvers` is set per call — the host wires one
 * handler per rail row. `monitor: null` clears the monitor; the other sub-fields
 * are preserved untouched. */
type ExecutionPolicyEdit =
  | { monitor: MonitorEdit | null }
  | { reviewers: ParticipantValue[] }
  | { approvers: ParticipantValue[] };

/** Read-modify-write the issue's executionPolicy: apply ONE sub-field edit
 * while preserving the others, then return the full merged policy ready to
 * PATCH. Returns `null` when the result carries no monitor and no stages, so
 * the caller sends `executionPolicy: null` and clears the row entirely (rather
 * than shipping an inert empty object).
 *
 * This is the ONE place monitor + reviewer + approver edits are reconciled, so
 * editing any one of them can never wipe the others. */
export function patchExecutionPolicy(
  existingPolicy: IssueExecutionPolicy | null | undefined,
  edit: ExecutionPolicyEdit,
): IssueExecutionPolicy | null {
  const mode = existingPolicy?.mode ?? "normal";

  // Carry forward whichever sub-fields this edit does not touch. A monitor
  // edit only carries the three rail-editable fields (nextCheckAt / notes /
  // serviceName), so merge it onto the existing monitor rather than replacing
  // it — otherwise upstream-managed fields (kind / externalRef / scheduledBy /
  // timeoutAt / maxAttempts / recoveryPolicy) would be reset to their schema
  // defaults on every reschedule.
  const monitor =
    "monitor" in edit
      ? edit.monitor === null
        ? null
        : { ...existingPolicy?.monitor, ...edit.monitor }
      : existingPolicy?.monitor ?? null;

  const reviewerValues =
    "reviewers" in edit
      ? edit.reviewers
      : stageParticipantValues(existingPolicy, "review");
  const approverValues =
    "approvers" in edit
      ? edit.approvers
      : stageParticipantValues(existingPolicy, "approval");

  const stages: IssueExecutionStage[] = [];
  const reviewStage = buildStage(existingPolicy, "review", reviewerValues);
  if (reviewStage) stages.push(reviewStage);
  const approvalStage = buildStage(existingPolicy, "approval", approverValues);
  if (approvalStage) stages.push(approvalStage);

  // The rail edits only monitor + the two stages, but upstream's
  // executionPolicy carries other top-level fields (commentRequired,
  // reviewPreset, authorizationPolicy). Strip the three we manage and carry the
  // rest forward unchanged so a rail edit never resets them to schema defaults.
  const { monitor: _m, stages: _s, mode: _md, ...preserved } =
    existingPolicy ?? {};
  void _m;
  void _s;
  void _md;
  const hasPreserved = Object.keys(preserved).length > 0;

  if (!monitor && stages.length === 0 && !hasPreserved) return null;

  return {
    ...preserved,
    mode,
    stages,
    ...(monitor ? { monitor } : {}),
  };
}
