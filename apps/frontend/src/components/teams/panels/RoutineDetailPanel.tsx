// apps/frontend/src/components/teams/panels/RoutineDetailPanel.tsx

// Restructured onto the editorial detail shell, mirroring
// goals/GoalDetailPage.tsx + projects/ProjectDetailPage.tsx: an
// `editorial-page` container with a [1fr 320px] grid — a main column
// (font-editorial-serif header + Schedule + Run-history sections) and a
// sticky non-scrollable Properties aside (Status / Assignee / Created /
// Updated). Status reads as a real swatch (RoutineStatusIcon, modeled on
// GoalStatusIcon) rather than an --edge fill.
//
// A2 (editable routines): the read-only shell above is now the DEFAULT view;
// edit affordances layer on top of it — an Edit dialog (title / description /
// assignee / priority / status / typed variables incl. the `date` type), a
// schedule Add / Edit / Delete + webhook secret Rotate on each trigger, and a
// Revisions view with Restore. All mutations go through useTeamsApi; the
// backend keeps the trial-gate on activation-adjacent actions (a 402 surfaces
// inline as an upgrade prompt rather than a broken button).

"use client";

import { useState } from "react";
import { notifyConversionGate } from "@/lib/notify";
import {
  ChevronRight,
  History,
  KeyRound,
  KeySquare,
  Pencil,
  Plus,
  RotateCcw,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import {
  EditorialButton,
  EditorialDialog,
  EditorialSelect,
} from "@/components/teams/shell";
import { AssigneePicker } from "@/components/teams/issues/pickers/AssigneePicker";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useApi } from "@/lib/api";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { isAutoPaused } from "@/components/teams/shared/lib/routineHealth";
import { describeCron } from "@/components/teams/shared/cron-readable";
import { SecretField } from "@/components/teams/shared/components/SecretField";
import {
  routineReviewerAgentId,
  withRoutineReviewer,
} from "@/components/teams/panels/routineExecutionPolicy";
import {
  SIGNING_MODE_OPTIONS,
  REPLAY_WINDOW_SIGNING_MODES,
  DEFAULT_REPLAY_WINDOW_SEC,
} from "@/components/teams/shared/webhookSigning";
import { cn } from "@/lib/utils";
import {
  handleShellLinkClick,
  teamsDetailUrl,
  teamsPanelUrl,
} from "@/lib/shellNav";
import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";
import type {
  CompanyAgent,
  RoutineDetail,
  RoutineRevision,
  RoutineRun,
  RoutineTriggerSecretMaterial,
  RoutineTriggerSummary,
  RoutineVariable,
  RoutineVariableType,
  RoutineVariableValue,
} from "@/components/teams/shared/types";

function runStatusColor(status: string): string {
  const s = status.toLowerCase();
  if (s === "failed" || s === "error" || s === "cancelled") return "var(--danger)";
  if (s === "succeeded" || s === "completed" || s === "done") return "var(--forest)";
  return "var(--mute)";
}

// Retheme-mapped onto the editorial palette (mirrors GoalStatusIcon):
//   active   -> --forest  (running / live)
//   paused   -> --warn    (suspended — attention amber)
//   draft    -> --mute    (not yet live)
//   archived -> --mute    (retired — muted)
const routineStatusColor: Record<string, string> = {
  active: "var(--forest)",
  paused: "var(--warn)",
  draft: "var(--mute)",
  archived: "var(--mute)",
};
const routineStatusColorDefault = "var(--mute)";

const STATUS_OPTIONS: ReadonlyArray<readonly [string, string]> = [
  ["active", "Active"],
  ["paused", "Paused"],
  ["draft", "Draft"],
  ["archived", "Archived"],
];
const PRIORITY_OPTIONS: ReadonlyArray<readonly [string, string]> = [
  ["critical", "Critical"],
  ["high", "High"],
  ["medium", "Medium"],
  ["low", "Low"],
];
const VAR_TYPE_OPTIONS: ReadonlyArray<readonly [RoutineVariableType, string]> = [
  ["text", "Text"],
  ["textarea", "Long text"],
  ["number", "Number"],
  ["boolean", "Boolean"],
  ["select", "Select"],
  ["date", "Date"],
];

function statusLabel(status: string): string {
  return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Human copy for a blocked-gate 402 (routines_require_subscription): the shared
// API client only promotes a STRING `detail` to `ApiError.message`, so a
// structured `{blocked, code, message}` body would surface as a stringified
// exception. It keeps the parsed body, which we read here — the difference
// between "Error: API 402" and an actual upgrade prompt. (Mirrors RoutinesPanel.)
function gateMessage(e: unknown): string {
  const body = (e as { body?: { detail?: { message?: string } } })?.body;
  const msg = body?.detail?.message;
  return typeof msg === "string" && msg ? msg : e instanceof Error ? e.message : String(e);
}

function browserTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

// The backend's schedule-trigger default when `timezone` is omitted is UTC — not
// the browser zone (see CreateRoutineTriggerBody.timezone default + upstream's
// schedule default). Used as the seed + dirty-compare baseline when editing an
// existing trigger that stored no timezone.
const SCHEDULE_DEFAULT_TZ = "UTC";

// A signed webhook trigger is the only kind that owns a rotatable secret.
// An unsigned webhook (signingMode "none") still has a publicId so it can be
// fired externally, but there is no secret to rotate — showing the button just
// calls the rotate endpoint and surfaces an avoidable error.
function triggerHasSecret(t: RoutineTriggerSummary): boolean {
  if (t.kind !== "webhook") return false;
  if (t.signingMode === "none") return false;
  // Known signing mode (bearer/hmac) → rotatable. If signingMode wasn't
  // projected, fall back to publicId so a signed webhook is never hidden.
  return t.signingMode != null || t.publicId != null;
}

const INPUT_CLASS =
  "w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]";

// Inline error banner shared by the edit dialogs so a 402 gate / 5xx surfaces
// ON the dialog the user is looking at, not just the main-column banner it
// overlays. Renders nothing when there is no message.
function DialogError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="text-sm rounded px-3 py-2"
      style={{
        color: "var(--danger)",
        border: "1px solid color-mix(in srgb, var(--danger) 20%, transparent)",
        background: "color-mix(in srgb, var(--danger) 6%, transparent)",
      }}
    >
      {message}
    </div>
  );
}

// A status swatch + label, modeled on goals/GoalStatusIcon. `autoPaused`
// renders the same paused (amber) swatch but FILLED — like the active dot —
// plus a distinct title, so a breaker auto-pause reads differently from a
// hand-pause (hollow ring, plain "Paused" label) at a glance.
function RoutineStatusIcon({
  status,
  autoPaused,
  showLabel,
  className,
}: {
  status: string;
  autoPaused?: boolean;
  showLabel?: boolean;
  className?: string;
}) {
  const swatchColor = routineStatusColor[status] ?? routineStatusColorDefault;
  const isActive = status === "active";
  const filled = isActive || autoPaused;
  const label = autoPaused ? "Auto-paused after repeated failures" : statusLabel(status);
  const circle = (
    <span
      className={cn(
        "relative inline-flex h-4 w-4 rounded-full border-2 shrink-0",
        className,
      )}
      style={{ color: swatchColor, borderColor: swatchColor }}
      aria-label={label}
      title={label}
    >
      {filled && (
        <span className="absolute inset-0 m-auto h-2 w-2 rounded-full bg-current" />
      )}
    </span>
  );
  if (!showLabel) return circle;
  return (
    <span className="inline-flex items-center gap-1.5">
      {circle}
      <span className="text-sm">{autoPaused ? "Auto-paused" : statusLabel(status)}</span>
    </span>
  );
}

function runTime(r: RoutineRun): number {
  const v = r.triggeredAt ?? r.startedAt ?? r.createdAt ?? null;
  const t = v ? new Date(v).getTime() : 0;
  return Number.isNaN(t) ? 0 : t;
}

// A trigger's `nextRunAt` is a FUTURE timestamp, so the past-only `timeAgo`
// would render every upcoming run as "just now". Format future-relative
// ("in 8h") and only fall back to `timeAgo` for a past/overdue next-run.
function nextRunLabel(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const diffSec = Math.round((then - Date.now()) / 1000);
  if (diffSec < 60) return timeAgo(iso); // overdue / imminent → "X ago" / "just now"
  if (diffSec < 3600) return `in ${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `in ${Math.floor(diffSec / 3600)}h`;
  return `in ${Math.floor(diffSec / 86400)}d`;
}

function PropertyRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className="grid grid-cols-[100px_1fr] gap-3 items-baseline py-2 text-sm"
      style={{ color: "var(--ink-soft)", borderBottom: "1px solid var(--edge)" }}
    >
      <dt className="section-label" style={{ marginTop: 0, marginBottom: 0 }}>
        <b>{label}</b>
      </dt>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

const EMPTY_DASH_STYLE = { color: "var(--mute)" } as const;

export function RoutineDetailPanel({ routineId }: { routineId: string }) {
  const { read, post, patch, del } = useTeamsApi();
  // The webhook-secret endpoint (routers/automations.py) is mounted under
  // /api/v1, not the Teams BFF `post`/`patch` above proxy through — mirrors
  // RoutinesPanel's saveWebhookSecret.
  const automationsApi = useApi();
  const {
    data: routine,
    isLoading,
    error,
    mutate,
  } = read<RoutineDetail>(`/routines/${routineId}`);
  const {
    data: runsData,
    error: runsError,
    mutate: mutateRuns,
  } = read<{ runs: RoutineRun[] }>(`/routines/${routineId}/runs`);
  const { data: revisionsData, mutate: mutateRevisions } = read<{
    revisions: RoutineRevision[];
  }>(`/routines/${routineId}/revisions`);
  const { data: agentsResp } = read<{ agents: CompanyAgent[] }>("/agents");
  const agents = agentsResp?.agents ?? [];
  const isError = !!error;

  // Edit state. Kept OUT of render-time reads so the read-only view (and its
  // tests) are untouched until the user reaches for an affordance.
  const [editing, setEditing] = useState(false);
  const [triggerDialog, setTriggerDialog] = useState<
    { mode: "add" } | { mode: "edit"; trigger: RoutineTriggerSummary } | null
  >(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [rotatedSecret, setRotatedSecret] = useState<RoutineTriggerSecretMaterial | null>(null);
  const [createdWebhook, setCreatedWebhook] = useState<RoutineTriggerSecretMaterial | null>(null);
  const [secretDialogTrigger, setSecretDialogTrigger] = useState<RoutineTriggerSummary | null>(
    null,
  );

  async function refresh(): Promise<void> {
    await Promise.all([mutate?.(), mutateRuns?.(), mutateRevisions?.()]);
  }

  // One shared runner for every mutation: clears the last error, flips busy,
  // surfaces a 402/5xx inline (via gateMessage) instead of an unhandled reject.
  // A trial-gate 402 renders the upgrade toast instead of the inline line.
  async function run(fn: () => Promise<void>): Promise<boolean> {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      return true;
    } catch (e) {
      if (notifyConversionGate(e) === null) {
        setActionError(gateMessage(e));
      }
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function deleteTrigger(triggerId: string): Promise<void> {
    if (typeof window !== "undefined" && !window.confirm("Delete this schedule?")) return;
    await run(async () => {
      await del(`/routines/${routineId}/triggers/${triggerId}`);
      await refresh();
    });
  }

  async function rotateSecret(triggerId: string): Promise<void> {
    // Rotating mints a NEW RANDOM secret. That's right when YOU control the
    // sender (you copy the new secret into it), but it silently breaks a
    // provider-imposed secret: a Stripe endpoint keeps signing with its own
    // whsec_, so once this trigger holds a random secret every event 401s —
    // and Stripe can't be pointed at a random secret. Warn before the swap.
    if (
      typeof window !== "undefined" &&
      !window.confirm(
        "Generate a new random signing secret? The current one is invalidated " +
          "immediately, and any sender still signing with it — including a Stripe " +
          "webhook (which signs with its own secret) — will stop verifying until updated.",
      )
    )
      return;
    await run(async () => {
      // The wire shape is an OBJECT (RoutineTriggerSecretMaterial), not a bare
      // string — mirrors the create-trigger response below. Rendering it
      // directly as a JSX child (rather than through SecretField) is what
      // used to crash React 19 with "Objects are not valid as a React child".
      const res = await post<{ secretMaterial?: RoutineTriggerSecretMaterial | null }>(
        `/routines/${routineId}/triggers/${triggerId}/rotate-secret`,
        {},
      );
      if (res?.secretMaterial) setRotatedSecret(res.secretMaterial);
      await refresh();
    });
  }

  async function restoreRevision(revisionId: string): Promise<void> {
    if (
      typeof window !== "undefined" &&
      !window.confirm("Restore the automation to this revision?")
    )
      return;
    await run(async () => {
      await post(`/routines/${routineId}/revisions/${revisionId}/restore`, {});
      await refresh();
    });
  }

  // Same status-mutation path RoutinesPanel's enable/disable toggle uses
  // (PATCH { status }) — the Resume affordance on the auto-pause banner is
  // just that toggle fired from the detail view.
  async function patchStatus(status: string): Promise<void> {
    await run(async () => {
      await patch(`/routines/${routineId}`, { status });
      await refresh();
    });
  }

  if (isLoading && !routine) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-lora-serif), serif",
          color: "var(--mute)",
        }}
      >
        Loading…
      </div>
    );
  }

  if (isError && !routine) {
    return (
      <div
        role="alert"
        className="flex h-full flex-col items-center justify-center p-8 text-sm"
        style={{ color: "var(--danger)" }}
      >
        <p>Failed to load automation.</p>
        {error?.message && (
          <p className="mt-1" style={{ color: "var(--mute)" }}>
            {error.message}
          </p>
        )}
      </div>
    );
  }

  if (!routine) {
    return (
      <div
        className="flex h-full items-center justify-center p-8 text-sm italic"
        style={{
          fontFamily: "var(--font-lora-serif), serif",
          color: "var(--mute)",
        }}
      >
        Automation not found.
      </div>
    );
  }

  const runs = [...(runsData?.runs ?? [])].sort((a, b) => runTime(b) - runTime(a)); // newest first
  const triggers = routine.triggers ?? [];
  const revisions = revisionsData?.revisions ?? [];
  const assigneeAgent = routine.assigneeAgentId
    ? agents.find((a) => a.id === routine.assigneeAgentId)
    : null;
  const assigneeName =
    routine.assignee?.name ?? assigneeAgent?.name ?? routine.assigneeAgentId ?? null;
  const reviewerAgentId = routineReviewerAgentId(routine.executionPolicy);
  const reviewerAgent = reviewerAgentId
    ? agents.find((agent) => agent.id === reviewerAgentId)
    : null;

  return (
    <div
      className="editorial-page grid grid-cols-1 @3xl:grid-cols-[minmax(0,1fr)_320px] gap-6 p-4 sm:p-6"
      style={{ background: "var(--cream)", color: "var(--ink)" }}
    >
      <main className="flex flex-col gap-6 min-w-0">
        <nav
          className="flex items-center gap-1 text-xs"
          style={{ color: "var(--mute)" }}
          aria-label="Breadcrumb"
        >
          <Link
            href={teamsPanelUrl("routines")}
            prefetch={false}
            onClick={(e) => handleShellLinkClick(e, teamsPanelUrl("routines"))}
            className="hover:underline"
          >
            Automations
          </Link>
          <ChevronRight className="h-3 w-3" />
          <span className="truncate" style={{ color: "var(--ink-soft)" }}>
            {routine.title}
          </span>
        </nav>

        <div className="flex items-start justify-between gap-3">
          <div className="flex flex-col gap-2 min-w-0">
            <h1 className="font-editorial-serif text-2xl font-medium leading-tight text-[var(--ink)]">
              {routine.title}
            </h1>
            {routine.description && (
              <p
                className="text-sm whitespace-pre-wrap pl-3"
                style={{
                  borderLeft: "2px solid var(--edge)",
                  color: "var(--ink-soft)",
                }}
              >
                {routine.description}
              </p>
            )}
          </div>
          <EditorialButton
            variant="secondary"
            size="sm"
            disabled={busy}
            onClick={() => {
              setActionError(null);
              setEditing(true);
            }}
            aria-label="Edit automation"
          >
            <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
            Edit
          </EditorialButton>
        </div>

        {/* Circuit-breaker auto-pause banner: the routine stopped firing on
            its own after repeated failed runs. Distinct from a hand-pause
            (no banner there) — see isAutoPaused. */}
        {isAutoPaused(routine) && (
          <div
            role="status"
            className="rounded-lg border px-4 py-3 text-sm flex items-center justify-between gap-3"
            style={{
              borderColor: "color-mix(in srgb, var(--warn) 40%, transparent)",
              backgroundColor: "color-mix(in srgb, var(--warn) 12%, transparent)",
              color: "var(--warn)",
            }}
          >
            <span>
              Auto-paused after {routine.consecutiveFailureCount ?? "repeated"} failed runs
              {routine.autoPauseReason ? ` (${routine.autoPauseReason})` : ""}. Fix the
              cause, then resume.
            </span>
            <EditorialButton
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => {
                setActionError(null);
                void patchStatus("active");
              }}
            >
              Resume
            </EditorialButton>
          </div>
        )}

        {/* Main-column banner for errors from the inline actions (delete /
            rotate / restore). When an edit dialog is open it renders the error
            itself, so suppress the banner to avoid a duplicate message. */}
        {actionError && !editing && !triggerDialog && !secretDialogTrigger && (
          <DialogError message={actionError} />
        )}

        <section className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="section-label">
              <b>Schedule</b>
            </h2>
            <EditorialButton
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => {
                setActionError(null);
                setTriggerDialog({ mode: "add" });
              }}
              aria-label="Add schedule"
            >
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              Add
            </EditorialButton>
          </div>
          {triggers.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--mute)" }}>
              No triggers.
            </p>
          ) : (
            <ul className="flex flex-col gap-1">
              {triggers.map((t) => (
                <li key={t.id} className="flex items-center gap-2 text-sm">
                  <span
                    className="text-[10px] uppercase tracking-wide font-mono px-1.5 py-0.5 rounded flex-shrink-0"
                    style={{ background: "var(--edge)", color: "var(--ink)" }}
                  >
                    {t.kind}
                  </span>
                  {t.cronExpression && (
                    <div className="flex flex-col gap-0.5">
                      <span className="text-xs text-[var(--ink)]">
                        {describeCron(t.cronExpression)}
                      </span>
                      <span className="font-mono text-xs text-[var(--mute)]">
                        {t.cronExpression}
                      </span>
                    </div>
                  )}
                  {t.timezone && (
                    <span className="text-xs text-[var(--mute)]">{t.timezone}</span>
                  )}
                  {t.nextRunAt && (
                    <span className="text-xs text-[var(--mute)]">
                      · next {nextRunLabel(t.nextRunAt)}
                    </span>
                  )}
                  {t.enabled === false && (
                    <span className="text-xs text-[var(--mute)]">· paused</span>
                  )}
                  <span className="flex-1" />
                  {t.kind === "schedule" && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setActionError(null);
                        setTriggerDialog({ mode: "edit", trigger: t });
                      }}
                      className="text-[var(--mute)] hover:text-[var(--ink)] disabled:opacity-50"
                      aria-label={`Edit ${t.label ?? "schedule"}`}
                      title="Edit"
                    >
                      <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                    </button>
                  )}
                  {triggerHasSecret(t) && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => rotateSecret(t.id)}
                      className="text-[var(--mute)] hover:text-[var(--ink)] disabled:opacity-50"
                      aria-label={`Rotate to a new random secret for ${t.label ?? "webhook"}`}
                      title="Rotate to a new random secret"
                    >
                      <KeyRound className="h-3.5 w-3.5" aria-hidden="true" />
                    </button>
                  )}
                  {triggerHasSecret(t) && t.publicId && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        setActionError(null);
                        setSecretDialogTrigger(t);
                      }}
                      className="text-[var(--mute)] hover:text-[var(--ink)] disabled:opacity-50"
                      aria-label={`Set signing secret for ${t.label ?? "webhook"}`}
                      title="Set signing secret"
                    >
                      <KeySquare className="h-3.5 w-3.5" aria-hidden="true" />
                    </button>
                  )}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => deleteTrigger(t.id)}
                    className="text-[var(--mute)] hover:text-[var(--danger)] disabled:opacity-50"
                    aria-label={`Delete ${t.label ?? "schedule"}`}
                    title="Delete"
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="flex flex-col gap-3">
          <h2 className="section-label">
            <b>Run history</b>
          </h2>
          {runsError ? (
            <p role="alert" className="text-sm" style={{ color: "var(--danger)" }}>
              Failed to load run history.
            </p>
          ) : runs.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--mute)" }}>
              No runs yet.
            </p>
          ) : (
            <ul className="flex flex-col">
              {runs.map((r) => {
                const when = r.triggeredAt ?? r.startedAt ?? r.createdAt;
                const linkedIssueUrl = r.linkedIssue
                  ? teamsDetailUrl("issue", r.linkedIssue.id)
                  : null;
                return (
                  <li
                    key={r.id}
                    className="flex flex-col gap-0.5 px-3 py-2"
                    style={{ borderBottom: "1px solid var(--edge)" }}
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded flex-shrink-0"
                        style={{ background: "var(--edge)", color: "var(--mute)" }}
                      >
                        {r.source}
                      </span>
                      <span
                        className="text-xs capitalize flex-shrink-0"
                        style={{ color: runStatusColor(r.status) }}
                      >
                        {r.status.replace(/_/g, " ")}
                      </span>
                      {r.trigger && (
                        <span className="text-xs text-[var(--mute)] truncate">
                          {r.trigger.label ?? r.trigger.kind}
                        </span>
                      )}
                      {r.linkedIssue && linkedIssueUrl && (
                        <a
                          href={linkedIssueUrl}
                          onClick={(e) => handleShellLinkClick(e, linkedIssueUrl)}
                          className="text-xs hover:underline"
                          style={{ color: "var(--forest)" }}
                        >
                          {r.linkedIssue.identifier ?? r.linkedIssue.id.slice(0, 8)}
                        </a>
                      )}
                      <span className="flex-1" />
                      {when && (
                        <span className="text-xs text-[var(--mute)] flex-shrink-0">
                          {timeAgo(when)}
                        </span>
                      )}
                    </div>
                    {r.failureReason && (
                      <div className="text-xs pl-1" style={{ color: "var(--danger)" }}>
                        {r.failureReason}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        {revisions.length > 0 && (
          <section className="flex flex-col gap-3">
            <h2 className="section-label inline-flex items-center gap-1.5">
              <History className="h-3.5 w-3.5" aria-hidden="true" />
              <b>Revisions</b>
            </h2>
            <ul className="flex flex-col">
              {revisions.map((rev) => (
                <li
                  key={rev.id}
                  className="flex items-center gap-2 px-3 py-2 text-sm"
                  style={{ borderBottom: "1px solid var(--edge)" }}
                >
                  {rev.revisionNumber != null && (
                    <span className="font-mono text-xs text-[var(--mute)] flex-shrink-0">
                      v{rev.revisionNumber}
                    </span>
                  )}
                  <span className="truncate text-[var(--ink-soft)]">
                    {rev.changeSummary ?? "Revision"}
                  </span>
                  <span className="flex-1" />
                  {rev.createdAt && (
                    <span className="text-xs text-[var(--mute)] flex-shrink-0">
                      {timeAgo(rev.createdAt)}
                    </span>
                  )}
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => restoreRevision(rev.id)}
                    className="inline-flex items-center gap-1 text-xs text-[var(--forest)] hover:underline disabled:opacity-50 flex-shrink-0"
                    aria-label={`Restore revision ${rev.revisionNumber ?? rev.id}`}
                  >
                    <RotateCcw className="h-3 w-3" aria-hidden="true" />
                    Restore
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </main>

      <aside className="@3xl:sticky @3xl:top-4 @3xl:self-start">
        <dl className="flex flex-col">
          <PropertyRow label="Status">
            {routine.status ? (
              <RoutineStatusIcon
                status={routine.status}
                autoPaused={isAutoPaused(routine)}
                showLabel
              />
            ) : (
              <span data-empty="true" style={EMPTY_DASH_STYLE}>
                —
              </span>
            )}
          </PropertyRow>
          <PropertyRow label="Assignee">
            {assigneeAgent ? (
              <Link
                href={agentInspectorUrl(assigneeAgent.id)}
                prefetch={false}
                onClick={(e) =>
                  handleShellLinkClick(e, agentInspectorUrl(assigneeAgent.id))
                }
                className="hover:underline"
                style={{ color: "var(--ink)" }}
              >
                {assigneeAgent.name}
              </Link>
            ) : assigneeName ? (
              <span style={{ color: "var(--ink)" }}>{assigneeName}</span>
            ) : (
              <span data-empty="true" style={EMPTY_DASH_STYLE}>
                —
              </span>
            )}
          </PropertyRow>
          <PropertyRow label="Reviewer">
            {reviewerAgent ? (
              <Link
                href={agentInspectorUrl(reviewerAgent.id)}
                prefetch={false}
                onClick={(e) =>
                  handleShellLinkClick(e, agentInspectorUrl(reviewerAgent.id))
                }
                className="hover:underline"
                style={{ color: "var(--ink)" }}
              >
                {reviewerAgent.name}
              </Link>
            ) : reviewerAgentId ? (
              <span style={{ color: "var(--ink)" }}>{reviewerAgentId}</span>
            ) : (
              <span data-empty="true" style={EMPTY_DASH_STYLE}>
                —
              </span>
            )}
          </PropertyRow>
          <PropertyRow label="Created">
            {routine.createdAt ? (
              <time dateTime={routine.createdAt} style={{ color: "var(--mute)" }}>
                {timeAgo(routine.createdAt)}
              </time>
            ) : (
              <span data-empty="true" style={EMPTY_DASH_STYLE}>
                —
              </span>
            )}
          </PropertyRow>
          <PropertyRow label="Updated">
            {routine.updatedAt ? (
              <time dateTime={routine.updatedAt} style={{ color: "var(--mute)" }}>
                {timeAgo(routine.updatedAt)}
              </time>
            ) : (
              <span data-empty="true" style={EMPTY_DASH_STYLE}>
                —
              </span>
            )}
          </PropertyRow>
        </dl>
      </aside>

      {editing && (
        <EditRoutineDialog
          routine={routine}
          agents={agents}
          submitting={busy}
          error={actionError}
          onClose={() => setEditing(false)}
          onSave={async (body) => {
            const ok = await run(async () => {
              await patch(`/routines/${routineId}`, body);
              await refresh();
            });
            if (ok) setEditing(false);
          }}
        />
      )}

      {triggerDialog && (
        <TriggerDialog
          mode={triggerDialog.mode}
          trigger={triggerDialog.mode === "edit" ? triggerDialog.trigger : undefined}
          submitting={busy}
          error={actionError}
          onClose={() => setTriggerDialog(null)}
          onSubmit={async (body) => {
            const ok = await run(async () => {
              if (triggerDialog.mode === "add") {
                const res = await post<{ secretMaterial?: RoutineTriggerSecretMaterial | null }>(
                  `/routines/${routineId}/triggers`,
                  body,
                );
                // A webhook create returns the URL + secret exactly once — a
                // schedule trigger has none, so `secretMaterial` is absent/null.
                if (res?.secretMaterial) setCreatedWebhook(res.secretMaterial);
              } else {
                await patch(
                  `/routines/${routineId}/triggers/${triggerDialog.trigger.id}`,
                  body,
                );
              }
              await refresh();
            });
            if (ok) setTriggerDialog(null);
          }}
        />
      )}

      {createdWebhook != null && (
        <EditorialDialog
          open
          onOpenChange={(o) => !o && setCreatedWebhook(null)}
          title="Webhook created"
          description="Copy the URL and secret now — the secret is shown only once."
          footer={
            <EditorialButton
              variant="primary"
              size="sm"
              onClick={() => setCreatedWebhook(null)}
            >
              Done
            </EditorialButton>
          }
        >
          <div className="flex flex-col gap-3">
            <SecretField label="Webhook URL" value={createdWebhook.webhookUrl} />
            <SecretField label="Webhook secret" value={createdWebhook.webhookSecret} />
          </div>
        </EditorialDialog>
      )}

      {rotatedSecret != null && (
        <EditorialDialog
          open
          onOpenChange={(o) => !o && setRotatedSecret(null)}
          title="Signing secret rotated"
          description="Copy this secret now — it is shown only once. Update any webhook caller with the new value."
          footer={
            <EditorialButton
              variant="primary"
              size="sm"
              onClick={() => setRotatedSecret(null)}
            >
              Done
            </EditorialButton>
          }
        >
          <SecretField label="Webhook secret" value={rotatedSecret.webhookSecret} />
        </EditorialDialog>
      )}

      {secretDialogTrigger && (
        <SetSigningSecretDialog
          submitting={busy}
          error={actionError}
          onClose={() => setSecretDialogTrigger(null)}
          onSave={async (value) => {
            const ok = await run(async () => {
              // Arms/updates the trigger's secret with a SPECIFIC
              // provider-issued value (e.g. a Stripe endpoint's whsec_) —
              // distinct from rotateSecret above, which mints a random one.
              // Same wire endpoint RoutinesPanel's guided create-flow uses,
              // but reached under /api/v1 via automationsApi, not the Teams BFF.
              await automationsApi.put(
                `/automations/${secretDialogTrigger.publicId}/webhook-secret`,
                { webhook_secret: value.trim() },
              );
              await refresh();
            });
            if (ok) setSecretDialogTrigger(null);
          }}
        />
      )}
    </div>
  );
}

/* ── edit routine dialog (with typed-variable editor) ─────────────────────── */

interface RoutineEditBody {
  title?: string;
  description?: string | null;
  assignee_agent_id?: string | null;
  priority?: string;
  status?: string;
  variables?: RoutineVariable[];
  execution_policy?: RoutineDetail["executionPolicy"];
}

// A local, editable copy of a routine variable — every field is a string in the
// form (booleans/dates are strings too) and coerced back to its declared type
// on save, mirroring RoutinesPanel's run-now editor.
interface VarDraft {
  name: string;
  type: RoutineVariableType;
  label: string;
  required: boolean;
  defaultValue: string;
  options: string; // comma-separated, select-only
}

function toDraft(v: RoutineVariable): VarDraft {
  return {
    name: v.name,
    type: v.type ?? "text",
    label: v.label ?? "",
    required: v.required !== false,
    defaultValue: v.defaultValue == null ? "" : String(v.defaultValue),
    options: (v.options ?? []).join(", "),
  };
}

function fromDraft(d: VarDraft): RoutineVariable {
  const out: RoutineVariable = { name: d.name.trim(), type: d.type, required: d.required };
  if (d.label.trim()) out.label = d.label.trim();
  if (d.type === "select") {
    out.options = d.options
      .split(",")
      .map((o) => o.trim())
      .filter(Boolean);
  }
  const raw = d.defaultValue.trim();
  if (raw !== "") {
    let dv: RoutineVariableValue = raw;
    if (d.type === "boolean") dv = raw === "true";
    else if (d.type === "number") {
      const n = Number(raw);
      if (Number.isFinite(n)) dv = n;
    }
    out.defaultValue = dv;
  }
  return out;
}

// Structural, order-sensitive equality for two typed-variable arrays. Both sides
// are built by the SAME `fromDraft`, so a field-by-field compare on the canonical
// output cleanly answers "did the user actually edit the variables?" — used to
// dirty-diff the array and avoid re-sending (and thereby clobbering) definitions
// the loaded detail may have captured before a concurrent edit.
function variablesEqual(a: RoutineVariable[], b: RoutineVariable[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    const x = a[i];
    const y = b[i];
    if (
      x.name !== y.name ||
      (x.type ?? "text") !== (y.type ?? "text") ||
      (x.required !== false) !== (y.required !== false) ||
      (x.label ?? "") !== (y.label ?? "") ||
      x.defaultValue !== y.defaultValue ||
      (x.options ?? []).length !== (y.options ?? []).length ||
      (x.options ?? []).some((opt, k) => opt !== (y.options ?? [])[k])
    ) {
      return false;
    }
  }
  return true;
}

function EditRoutineDialog({
  routine,
  agents,
  submitting,
  error,
  onClose,
  onSave,
}: {
  routine: RoutineDetail;
  agents: CompanyAgent[];
  submitting: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (body: RoutineEditBody) => void | Promise<void>;
}) {
  const [title, setTitle] = useState(routine.title ?? "");
  const [description, setDescription] = useState(routine.description ?? "");
  const [assigneeAgentId, setAssigneeAgentId] = useState<string | null>(
    routine.assigneeAgentId ?? null,
  );
  const [loadedExecutionPolicy] = useState(() => routine.executionPolicy ?? null);
  const [reviewerAgentId, setReviewerAgentId] = useState<string | null>(() =>
    routineReviewerAgentId(loadedExecutionPolicy),
  );
  const [priority, setPriority] = useState<string>(routine.priority ?? "medium");
  const [status, setStatus] = useState<string>(routine.status ?? "active");
  const [vars, setVars] = useState<VarDraft[]>(() =>
    (routine.variables ?? []).map(toDraft),
  );
  // Immutable snapshot of the variables as they loaded when the dialog opened.
  // Comparing the draft against the LIVE routine.variables prop is unsafe: SWR can
  // revalidate that prop (window focus / reconnect) to a concurrent edit while this
  // dialog stays open, which would mark an untouched list dirty and clobber the edit.
  const [loadedVarsSnapshot] = useState<Array<ReturnType<typeof fromDraft>>>(() =>
    (routine.variables ?? [])
      .map(toDraft)
      .filter((v) => v.name.trim() !== "")
      .map(fromDraft),
  );

  function setVar(i: number, patch: Partial<VarDraft>) {
    setVars((prev) => prev.map((v, idx) => (idx === i ? { ...v, ...patch } : v)));
  }
  function addVar() {
    setVars((prev) => [
      ...prev,
      { name: "", type: "text", label: "", required: true, defaultValue: "", options: "" },
    ]);
  }
  function removeVar(i: number) {
    setVars((prev) => prev.filter((_, idx) => idx !== i));
  }

  const namedVars = vars.filter((v) => v.name.trim() !== "");
  const canSave = title.trim() !== "";
  const reviewerAgents = agents.filter(
    (agent) => agent.id !== assigneeAgentId && agent.status !== "terminated",
  );

  function selectAssignee(nextAgentId: string | null) {
    setAssigneeAgentId(nextAgentId);
    if (nextAgentId === reviewerAgentId) setReviewerAgentId(null);
  }

  // Send only fields the user actually changed. The backend gates any body that
  // (re-)asserts status:"active" as a fireability-increasing edit, so a benign
  // rename / reassign by a trial owner must NOT carry an unchanged status.
  // Variables are likewise dirty-diffed: a PATCH replaces the whole definition
  // set, so re-sending an untouched array would clobber newer definitions if the
  // loaded detail was stale (a concurrent edit, or a GET that omitted them). Only
  // include `variables` when the current array differs from what loaded.
  async function submit() {
    if (!canSave) return;
    const body: RoutineEditBody = {};
    const nextTitle = title.trim();
    if (nextTitle !== (routine.title ?? "").trim()) body.title = nextTitle;
    const nextDescription = description.trim() || null;
    if (nextDescription !== ((routine.description ?? "").trim() || null))
      body.description = nextDescription;
    const nextAssignee = assigneeAgentId ?? null;
    if (nextAssignee !== (routine.assigneeAgentId ?? null))
      body.assignee_agent_id = nextAssignee;
    if (priority !== (routine.priority ?? "medium")) body.priority = priority;
    if (status !== (routine.status ?? "active")) body.status = status;
    const nextVars = namedVars.map(fromDraft);
    if (!variablesEqual(nextVars, loadedVarsSnapshot)) body.variables = nextVars;
    if (reviewerAgentId !== routineReviewerAgentId(loadedExecutionPolicy)) {
      body.execution_policy = withRoutineReviewer(
        loadedExecutionPolicy,
        reviewerAgentId,
      );
    }
    await onSave(body);
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Edit automation"
      footer={
        <>
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton
            variant="primary"
            size="sm"
            onClick={submit}
            disabled={submitting || !canSave}
          >
            {submitting ? "Saving…" : "Save"}
          </EditorialButton>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <DialogError message={error} />
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
        <div className="block">
          <span className="text-sm text-[var(--ink-soft)]">Assignee</span>
          <div className="mt-1">
            <AssigneePicker
              agents={agents}
              selectedAgentId={assigneeAgentId}
              onChange={selectAssignee}
              variant="inline"
              className="border border-[var(--edge)] rounded px-3 py-2 bg-[var(--cream)]"
            />
          </div>
        </div>
        <div className="block">
          <span className="text-sm text-[var(--ink-soft)]">Reviewer</span>
          <div className="mt-1">
            <AssigneePicker
              agents={reviewerAgents}
              selectedAgentId={reviewerAgentId}
              onChange={setReviewerAgentId}
              variant="inline"
              noun="reviewer"
              className="border border-[var(--edge)] rounded px-3 py-2 bg-[var(--cream)]"
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="text-sm text-[var(--ink-soft)]">Status</span>
            <div className="mt-1">
              <EditorialSelect
                options={STATUS_OPTIONS.map(([value, label]) => ({ value, label }))}
                value={status}
                onValueChange={setStatus}
              />
            </div>
          </label>
          <label className="block">
            <span className="text-sm text-[var(--ink-soft)]">Priority</span>
            <div className="mt-1">
              <EditorialSelect
                options={PRIORITY_OPTIONS.map(([value, label]) => ({ value, label }))}
                value={priority}
                onValueChange={setPriority}
              />
            </div>
          </label>
        </div>
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            className={INPUT_CLASS}
          />
        </label>

        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="text-sm text-[var(--ink-soft)]">Variables</span>
            <EditorialButton variant="ghost" size="sm" onClick={addVar}>
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              Add variable
            </EditorialButton>
          </div>
          {vars.length === 0 ? (
            <p className="text-xs italic" style={{ color: "var(--mute)" }}>
              No variables. Add one to prompt for input at dispatch time.
            </p>
          ) : (
            <ul className="flex flex-col gap-3">
              {vars.map((v, i) => (
                <li
                  key={i}
                  className="flex flex-col gap-2 rounded border border-[var(--edge)] p-2"
                >
                  <div className="grid grid-cols-[1fr_auto_auto] gap-2 items-center">
                    <input
                      value={v.name}
                      onChange={(e) => setVar(i, { name: e.target.value })}
                      placeholder="name"
                      className="border border-[var(--edge)] bg-[var(--cream)] rounded px-2 py-1 text-sm text-[var(--ink)]"
                    />
                    <div className="w-32">
                      <EditorialSelect
                        options={VAR_TYPE_OPTIONS.map(([value, label]) => ({
                          value,
                          label,
                        }))}
                        value={v.type}
                        onValueChange={(next) =>
                          setVar(i, { type: next as RoutineVariableType })
                        }
                      />
                    </div>
                    <button
                      type="button"
                      onClick={() => removeVar(i)}
                      className="text-[var(--mute)] hover:text-[var(--danger)]"
                      aria-label={`Remove variable ${v.name || i + 1}`}
                      title="Remove"
                    >
                      <Trash2 className="h-4 w-4" aria-hidden="true" />
                    </button>
                  </div>
                  <input
                    value={v.label}
                    onChange={(e) => setVar(i, { label: e.target.value })}
                    placeholder="label (optional)"
                    className="border border-[var(--edge)] bg-[var(--cream)] rounded px-2 py-1 text-sm text-[var(--ink)]"
                  />
                  {v.type === "select" && (
                    <input
                      value={v.options}
                      onChange={(e) => setVar(i, { options: e.target.value })}
                      placeholder="options, comma-separated"
                      className="border border-[var(--edge)] bg-[var(--cream)] rounded px-2 py-1 text-sm text-[var(--ink)]"
                    />
                  )}
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-[var(--mute)] w-14">Default</span>
                    <VarDefaultInput
                      v={v}
                      onChange={(next) => setVar(i, { defaultValue: next })}
                    />
                  </div>
                  <label className="flex items-center gap-2 text-xs text-[var(--ink-soft)]">
                    <input
                      type="checkbox"
                      checked={v.required}
                      onChange={(e) => setVar(i, { required: e.target.checked })}
                    />
                    Required
                  </label>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </EditorialDialog>
  );
}

// Default-value input adapts to the variable's declared type: a date picker for
// `date` (upstream #8655), a true/false select for `boolean`, a number input
// for `number`, and plain text otherwise.
function VarDefaultInput({
  v,
  onChange,
}: {
  v: VarDraft;
  onChange: (next: string) => void;
}) {
  if (v.type === "boolean") {
    return (
      <div className="flex-1">
        <EditorialSelect
          options={[
            { value: "true", label: "True" },
            { value: "false", label: "False" },
          ]}
          value={v.defaultValue || "false"}
          onValueChange={onChange}
        />
      </div>
    );
  }
  const inputType = v.type === "date" ? "date" : v.type === "number" ? "number" : "text";
  return (
    <input
      type={inputType}
      value={v.defaultValue}
      onChange={(e) => onChange(e.target.value)}
      className="flex-1 border border-[var(--edge)] bg-[var(--cream)] rounded px-2 py-1 text-sm text-[var(--ink)]"
    />
  );
}

/* ── add / edit trigger dialog (schedule or webhook) ──────────────────────── */

interface TriggerSubmitBody {
  kind?: string;
  cron_expression?: string;
  timezone?: string;
  // `null` is the explicit CLEAR signal (blanking a previously-set label).
  // `undefined` is dropped by JSON.stringify, so a clear must send `null` to
  // survive serialization and reach the backend.
  label?: string | null;
  enabled?: boolean;
  signing_mode?: string;
  replay_window_sec?: number;
}

function TriggerDialog({
  mode,
  trigger,
  submitting,
  error,
  onClose,
  onSubmit,
}: {
  mode: "add" | "edit";
  trigger?: RoutineTriggerSummary;
  submitting: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (body: TriggerSubmitBody) => void | Promise<void>;
}) {
  // For a NEW trigger we suggest the user's browser zone; for an EXISTING trigger
  // that stored no timezone we seed AND dirty-compare against the backend's real
  // default (UTC), so the field shows what actually fires. Seeding the browser zone
  // in edit mode would let a user outside UTC edit only the cron, have the field
  // silently match the browser-tz baseline, and omit `timezone` from the patch —
  // leaving the schedule on UTC while the UI implied local time. Using `initialTz`
  // for both the seed and the comparison makes the dirty-check honest by construction.
  const initialTz = trigger?.timezone ?? (mode === "add" ? browserTz() : SCHEDULE_DEFAULT_TZ);
  const [cron, setCron] = useState(trigger?.cronExpression ?? "0 9 * * *");
  const [timezone, setTimezone] = useState(initialTz);
  const [label, setLabel] = useState(trigger?.label ?? "");
  const [enabled, setEnabled] = useState(trigger?.enabled !== false);
  // Trigger kind — only choosable in "add" mode (a Plus button). Edit only
  // reaches this dialog for an existing schedule trigger (the Pencil affordance
  // is gated to `t.kind === "schedule"`), so kind is fixed there.
  const [kind, setKind] = useState<string>(mode === "add" ? "schedule" : trigger?.kind ?? "schedule");
  const [signingMode, setSigningMode] = useState("bearer");
  const [replayWindowSec, setReplayWindowSec] = useState(String(DEFAULT_REPLAY_WINDOW_SEC));
  const showReplayWindow = REPLAY_WINDOW_SIGNING_MODES.has(signingMode);
  const replayWindowNum = Number(replayWindowSec);
  const replayWindowValid =
    !showReplayWindow || (Number.isFinite(replayWindowNum) && replayWindowNum >= 30 && replayWindowNum <= 86400);

  const canSubmit =
    kind === "schedule" ? cron.trim() !== "" && timezone.trim() !== "" : replayWindowValid;

  async function submit() {
    if (!canSubmit) return;
    if (mode === "add") {
      if (kind === "webhook") {
        await onSubmit({
          kind: "webhook",
          signing_mode: signingMode,
          replay_window_sec: showReplayWindow ? replayWindowNum : undefined,
          label: label.trim() || undefined,
          enabled,
        });
        return;
      }
      await onSubmit({
        kind: "schedule",
        cron_expression: cron.trim(),
        timezone: timezone.trim(),
        label: label.trim() || undefined,
        enabled,
      });
      return;
    }
    // Edit: send only fields the user actually changed, so a benign relabel or a
    // pure disable (unchecking Enabled) doesn't re-assert cron/timezone and trip
    // the backend fireability gate. A pure disable sends just { enabled: false }.
    const body: TriggerSubmitBody = {};
    const nextCron = cron.trim();
    if (nextCron !== (trigger?.cronExpression ?? "").trim()) body.cron_expression = nextCron;
    const nextTz = timezone.trim();
    if (nextTz !== initialTz.trim()) body.timezone = nextTz;
    // Label: send the new text on a real change; send an explicit `null` when a
    // previously-set label is BLANKED (undefined would be dropped by JSON.stringify,
    // silently losing the clear so the old label persisted after refresh); omit it
    // when unchanged.
    const prevLabel = (trigger?.label ?? "").trim();
    const nextLabel = label.trim();
    if (nextLabel !== prevLabel) body.label = nextLabel === "" ? null : nextLabel;
    if (enabled !== (trigger?.enabled !== false)) body.enabled = enabled;
    await onSubmit(body);
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={
        mode === "edit" ? "Edit schedule" : kind === "webhook" ? "Add webhook" : "Add schedule"
      }
      footer={
        <>
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton
            variant="primary"
            size="sm"
            onClick={submit}
            disabled={submitting || !canSubmit}
          >
            {submitting ? "Saving…" : mode === "add" ? "Add" : "Save"}
          </EditorialButton>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <DialogError message={error} />
        {mode === "add" && (
          <label className="block">
            <span className="text-sm text-[var(--ink-soft)]">Trigger type</span>
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              className={INPUT_CLASS}
            >
              <option value="schedule">Schedule</option>
              <option value="webhook">Webhook</option>
            </select>
          </label>
        )}
        {kind === "schedule" ? (
          <>
            <label className="block">
              <span className="text-sm text-[var(--ink-soft)]">Schedule</span>
              <input
                value={cron}
                onChange={(e) => setCron(e.target.value)}
                placeholder="0 9 * * *"
                className={cn(INPUT_CLASS, "font-mono")}
              />
              <span className="text-xs text-[var(--mute)]">
                Cron expression — when the automation fires.
              </span>
            </label>
            <label className="block">
              <span className="text-sm text-[var(--ink-soft)]">Timezone</span>
              <input
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                placeholder="UTC"
                className={INPUT_CLASS}
              />
            </label>
          </>
        ) : (
          <>
            <label className="block">
              <span className="text-sm text-[var(--ink-soft)]">Signing mode</span>
              <select
                value={signingMode}
                onChange={(e) => setSigningMode(e.target.value)}
                className={INPUT_CLASS}
              >
                {SIGNING_MODE_OPTIONS.map(([value, optLabel]) => (
                  <option key={value} value={value}>
                    {optLabel}
                  </option>
                ))}
              </select>
              <span className="text-xs text-[var(--mute)]">
                The external app signs each request with this secret; Isol8
                verifies it.
              </span>
            </label>
            {showReplayWindow && (
              <label className="block">
                <span className="text-sm text-[var(--ink-soft)]">
                  Replay window (seconds)
                </span>
                <input
                  type="number"
                  min={30}
                  max={86400}
                  value={replayWindowSec}
                  onChange={(e) => setReplayWindowSec(e.target.value)}
                  className={INPUT_CLASS}
                />
                <span className="text-xs text-[var(--mute)]">
                  How long a signed delivery stays valid (30–86400s).
                </span>
              </label>
            )}
          </>
        )}
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Label (optional)</span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
        <label className="flex items-center gap-2 text-sm text-[var(--ink-soft)]">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Enabled
        </label>
      </div>
    </EditorialDialog>
  );
}

/* ── set signing secret dialog ─────────────────────────────────────────── */

// The post-creation path to arm/update a webhook trigger's secret with a
// SPECIFIC provider-issued value (e.g. a Stripe endpoint's whsec_) — additive
// to rotateSecret above, which mints a random one instead. A provider that
// signs with its own secret can never be pointed at a random one, so this is
// the only way to (re-)arm a trigger whose sender is out of our control.
function SetSigningSecretDialog({
  submitting,
  error,
  onClose,
  onSave,
}: {
  submitting: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (value: string) => void | Promise<void>;
}) {
  const [value, setValue] = useState("");
  const canSave = value.trim() !== "";

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Set signing secret"
      description="Paste the signing secret your provider issued (e.g. a Stripe endpoint's whsec_). Paperclip verifies incoming webhooks against it."
      footer={
        <>
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton
            variant="primary"
            size="sm"
            onClick={() => onSave(value)}
            disabled={submitting || !canSave}
          >
            {submitting ? "Saving…" : "Save"}
          </EditorialButton>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <DialogError message={error} />
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Signing secret</span>
          <input
            type="password"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="whsec_..."
            className={cn(INPUT_CLASS, "font-mono")}
          />
        </label>
      </div>
    </EditorialDialog>
  );
}
