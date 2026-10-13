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
// schedule Add / Edit / Delete + webhook secret Rotate on each trigger. A Run
// button and Run history / Activity / History tabs use the pieces ported from
// stock Paperclip v2026.916.1-isol8.2 under components/teams/routines/
// (run-variables dialog, activity section, revision history with compare +
// restore). All mutations go through useTeamsApi; the
// backend keeps the trial-gate on activation-adjacent actions (a 402 surfaces
// inline as an upgrade prompt rather than a broken button).

"use client";

import { useMemo, useState } from "react";
import { notifyConversionGate, notifySuccess } from "@/lib/notify";
import {
  ChevronRight,
  KeyRound,
  KeySquare,
  Pencil,
  Play,
  Plus,
  Trash2,
} from "lucide-react";
import Link from "next/link";
import {
  EditorialButton,
  EditorialDialog,
  EditorialSelect,
  PanelTabs,
} from "@/components/teams/shell";
import { RoutineActivitySection } from "@/components/teams/routines/RoutineActivitySection";
import { RoutineHistoryTab } from "@/components/teams/routines/RoutineHistoryTab";
import {
  RoutineRunVariablesDialog,
  routineRunNeedsConfiguration,
  type RoutineRunDialogSubmitData,
} from "@/components/teams/routines/RoutineRunVariablesDialog";
import { AssigneePicker } from "@/components/teams/issues/pickers/AssigneePicker";
import { LiveRunDot } from "@/components/teams/shared/components/LiveRunDot";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useApi } from "@/lib/api";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { isActiveStatus, statusColor as runStatusColor } from "@/components/teams/shared/lib/runStatus";
import { isAutoPaused } from "@/components/teams/shared/lib/routineHealth";
import { describeCron } from "@/components/teams/shared/cron-readable";
import { SecretField } from "@/components/teams/shared/components/SecretField";
import { ScheduleField } from "@/components/teams/shared/components/ScheduleField";
import {
  ConnectedAppFields,
  ConnectedAppResultDialog,
  StartRungPicker,
  useConnectedAppTrigger,
  type StartRung,
} from "@/components/teams/shared/components/ConnectedAppTrigger";
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
  IssueProject,
  RestoreRoutineRevisionResponse,
  RoutineDetail,
  RoutineRun,
  RoutineTriggerSecretMaterial,
  RoutineTriggerSummary,
  RoutineVariable,
  RoutineVariableType,
  RoutineVariableValue,
} from "@/components/teams/shared/types";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

// Retheme-mapped onto the editorial palette (mirrors GoalStatusIcon):
//   active   -> --midnight  (running / live)
//   paused   -> --warn    (suspended — attention amber)
//   draft    -> --midnight-muted    (not yet live)
//   archived -> --midnight-muted    (retired — muted)
const routineStatusColor: Record<string, string> = {
  active: "var(--midnight)",
  paused: "var(--warn)",
  draft: "var(--midnight-muted)",
  archived: "var(--midnight-muted)",
};
const routineStatusColorDefault = "var(--midnight-muted)";

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

// What a trigger row is CALLED. Every control on that row is destructive or
// arming (rotate secret, set secret, delete) and a routine routinely carries
// several webhook triggers — one per provider event — so a row that reads only
// "WEBHOOK · paused" is a footgun: three identical rows, and rotating the wrong
// one silently unarms a different event. The seeded label ("Stripe
// invoice.payment_failed") is the identity to show; a schedule falls back to
// its own sentence, and an unlabeled webhook to its public id, which is the
// part of its fire URL that differs per trigger.
function triggerTitle(t: RoutineTriggerSummary): string {
  const label = t.label?.trim();
  if (label) return label;
  if (t.cronExpression) return describeCron(t.cronExpression);
  if (t.kind === "webhook" && t.publicId) return `Webhook ${t.publicId.slice(0, 8)}`;
  return t.kind === "webhook" ? "Webhook" : t.kind;
}

// The identifying detail under the title: a schedule's raw cron (unchanged), or
// a webhook's public id + how it is signed — enough to match a row against the
// endpoint configured on the provider's side.
function triggerDetail(t: RoutineTriggerSummary): string | null {
  if (t.cronExpression) return t.cronExpression;
  if (t.kind !== "webhook") return null;
  const signing = SIGNING_MODE_OPTIONS.find(([value]) => value === t.signingMode)?.[1];
  return [t.publicId, signing].filter(Boolean).join(" · ") || null;
}

const INPUT_CLASS =
  "w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]";

// Inline error banner shared by the edit dialogs so a 402 gate / 5xx surfaces
// ON the dialog the user is looking at, not just the main-column banner it
// overlays. Renders nothing when there is no message.
function DialogError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="text-sm rounded-sm px-3 py-2"
      style={{
        color: "var(--err)",
        border: "1px solid var(--err)",
        background: "var(--cream)",
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
      style={{ color: "var(--midnight-soft)", borderBottom: "1px solid var(--edge)" }}
    >
      <dt className="section-label" style={{ marginTop: 0, marginBottom: 0 }}>
        <b>{label}</b>
      </dt>
      <dd className="min-w-0 truncate">{children}</dd>
    </div>
  );
}

const EMPTY_DASH_STYLE = { color: "var(--midnight-muted)" } as const;

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
  const { data: agentsResp } = read<{ agents: CompanyAgent[] }>("/agents");
  const agents = agentsResp?.agents ?? [];
  const { data: projectsResp } = read<{ projects: IssueProject[] }>("/projects");
  const projects = projectsResp?.projects ?? [];
  const agentLookup = useMemo(
    () => new Map((agentsResp?.agents ?? []).map((a) => [a.id, a])),
    [agentsResp],
  );
  const projectLookup = useMemo(
    () => new Map((projectsResp?.projects ?? []).map((p) => [p.id, p])),
    [projectsResp],
  );
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
  // Stock RoutineDetail sections that ours renders as tabs under Triggers.
  const [section, setSection] = useState<"runs" | "activity" | "history">("runs");
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  // Webhook triggers a revision restore recreated: upstream mints fresh
  // one-time secret material for each, shown once here.
  const [restoredSecrets, setRestoredSecrets] = useState<
    NonNullable<RestoreRoutineRevisionResponse["secretMaterials"]>
  >([]);

  async function refresh(): Promise<void> {
    // Activity is read by its tab alone (it fans out upstream), and SWR
    // revalidates it when that tab mounts.
    await Promise.all([mutate?.(), mutateRuns?.()]);
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
    const kind = routine?.triggers?.find((t) => t.id === triggerId)?.kind;
    if (
      typeof window !== "undefined" &&
      !window.confirm(kind === "webhook" ? "Delete this webhook trigger?" : "Delete this schedule?")
    )
      return;
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

  // Stock RoutineDetail's run: fire directly unless the routine needs the
  // run dialog (variables). `source: "manual"` marks it as UI-fired.
  async function fireRun(data?: RoutineRunDialogSubmitData): Promise<void> {
    const body: Record<string, unknown> = { source: "manual" };
    if (data?.variables && Object.keys(data.variables).length > 0) body.variables = data.variables;
    if (data?.assigneeAgentId) body.assigneeAgentId = data.assigneeAgentId;
    if (data?.projectId) body.projectId = data.projectId;
    const ok = await run(async () => {
      await post(`/routines/${routineId}/run`, body);
      await refresh();
    });
    if (ok) {
      setRunDialogOpen(false);
      notifySuccess(`Running ${routine?.title ?? "automation"}…`, {
        dedupeKey: `routine-run:${routineId}`,
      });
    }
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
          fontFamily: "var(--font-serif), serif",
          color: "var(--midnight-muted)",
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
        style={{ color: "var(--err)" }}
      >
        <p>Failed to load automation.</p>
        {error?.message && (
          <p className="mt-1" style={{ color: "var(--midnight-muted)" }}>
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
          fontFamily: "var(--font-serif), serif",
          color: "var(--midnight-muted)",
        }}
      >
        Automation not found.
      </div>
    );
  }

  const runs = [...(runsData?.runs ?? [])].sort((a, b) => runTime(b) - runTime(a)); // newest first
  const triggers = routine.triggers ?? [];
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
      style={{ background: "var(--cream)", color: "var(--midnight)" }}
    >
      <main className="flex flex-col gap-6 min-w-0">
        <nav
          className="flex items-center gap-1 text-xs"
          style={{ color: "var(--midnight-muted)" }}
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
          <ChevronRight size={12} strokeWidth={1.75} />
          <span className="truncate" style={{ color: "var(--midnight-soft)" }}>
            {routine.title}
          </span>
        </nav>

        <div className="flex items-start justify-between gap-3">
          <div className="flex flex-col gap-2 min-w-0">
            <h1 className="font-editorial-serif text-2xl font-medium leading-tight text-[var(--midnight)]">
              {routine.title}
            </h1>
            {routine.description && (
              <p
                className="text-sm whitespace-pre-wrap pl-3"
                style={{
                  borderLeft: "2px solid var(--edge)",
                  color: "var(--midnight-soft)",
                }}
              >
                {routine.description}
              </p>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <EditorialButton
              variant="secondary"
              size="sm"
              disabled={busy}
              onClick={() => {
                setActionError(null);
                if (routineRunNeedsConfiguration({ variables: routine.variables ?? [] })) {
                  setRunDialogOpen(true);
                } else {
                  void fireRun();
                }
              }}
              aria-label="Run automation now"
            >
              <Play size={16} aria-hidden="true" />
              Run
            </EditorialButton>
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
              <Pencil size={16} aria-hidden="true" />
              Edit
            </EditorialButton>
          </div>
        </div>

        {/* Circuit-breaker auto-pause banner: the routine stopped firing on
            its own after repeated failed runs. Distinct from a hand-pause
            (no banner there) — see isAutoPaused. */}
        {isAutoPaused(routine) && (
          <div
            role="status"
            className="rounded-lg border px-4 py-3 text-sm flex items-center justify-between gap-3"
            style={{
              borderColor: "var(--warn)",
              backgroundColor: "var(--cream)",
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
        {actionError && !editing && !triggerDialog && !secretDialogTrigger && !runDialogOpen && (
          <DialogError message={actionError} />
        )}

        <section className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-2">
            {/* "Triggers", not "Schedule": this section holds webhooks too,
                and a routine can have no schedule at all. */}
            <h2 className="section-label">
              <b>Triggers</b>
            </h2>
            <EditorialButton
              variant="ghost"
              size="sm"
              disabled={busy}
              onClick={() => {
                setActionError(null);
                setTriggerDialog({ mode: "add" });
              }}
              aria-label="Add trigger"
            >
              <Plus size={16} aria-hidden="true" />
              Add
            </EditorialButton>
          </div>
          {triggers.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--midnight-muted)" }}>
              No triggers.
            </p>
          ) : (
            <ul className="flex flex-col gap-1">
              {triggers.map((t) => (
                <li key={t.id} className="flex flex-col gap-1.5 text-sm">
                  <div className="flex items-center gap-2">
                    <span
                      className="label-caps font-mono px-1.5 py-0.5 rounded-sm shrink-0"
                      style={{ background: "var(--edge)", color: "var(--midnight)" }}
                    >
                      {t.kind}
                    </span>
                    <div className="flex min-w-0 flex-col gap-0.5">
                      <span className="truncate text-xs text-[var(--midnight)]">
                        {triggerTitle(t)}
                      </span>
                      {triggerDetail(t) && (
                        <span className="truncate font-mono text-xs text-[var(--midnight-muted)]">
                          {triggerDetail(t)}
                        </span>
                      )}
                    </div>
                    {t.timezone && (
                      <span className="text-xs text-[var(--midnight-muted)]">{t.timezone}</span>
                    )}
                    {t.nextRunAt && (
                      <span className="text-xs text-[var(--midnight-muted)]">
                        · next {nextRunLabel(t.nextRunAt)}
                      </span>
                    )}
                    {t.enabled === false && (
                      <span className="text-xs text-[var(--midnight-muted)]">· paused</span>
                    )}
                    <span className="flex-1" />
                    {t.kind === "schedule" && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => {
                              setActionError(null);
                              setTriggerDialog({ mode: "edit", trigger: t });
                            }}
                            className="text-[var(--midnight-muted)] hover:text-[var(--midnight)] disabled:opacity-50"
                            aria-label={`Edit ${triggerTitle(t)}`}
                          >
                            <Pencil size={16} aria-hidden="true" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>Edit</TooltipContent>
                      </Tooltip>
                    )}
                    {triggerHasSecret(t) && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => rotateSecret(t.id)}
                            className="text-[var(--midnight-muted)] hover:text-[var(--midnight)] disabled:opacity-50"
                            aria-label={`Rotate to a new random secret for ${triggerTitle(t)}`}
                          >
                            <KeyRound size={16} aria-hidden="true" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>Rotate to a new random secret</TooltipContent>
                      </Tooltip>
                    )}
                    {triggerHasSecret(t) && t.publicId && (
                      <Tooltip>
                        <TooltipTrigger asChild>
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => {
                              setActionError(null);
                              setSecretDialogTrigger(t);
                            }}
                            className="text-[var(--midnight-muted)] hover:text-[var(--midnight)] disabled:opacity-50"
                            aria-label={`Set signing secret for ${triggerTitle(t)}`}
                          >
                            <KeySquare size={16} aria-hidden="true" />
                          </button>
                        </TooltipTrigger>
                        <TooltipContent>Set signing secret</TooltipContent>
                      </Tooltip>
                    )}
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => deleteTrigger(t.id)}
                          className="text-[var(--midnight-muted)] hover:text-[var(--err)] disabled:opacity-50"
                          aria-label={`Delete ${triggerTitle(t)}`}
                        >
                          <Trash2 size={16} aria-hidden="true" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent>Delete</TooltipContent>
                    </Tooltip>
                  </div>
                  {/* Arming a webhook trigger means pasting THIS url into the
                      provider's dashboard. The guided create-flow shows it once,
                      for the routine it just made; without it here an existing
                      trigger (e.g. the Stripe triggers on a hand-written routine)
                      can only be armed through the API. Server-projected — the
                      shape is server-owned and a subtly wrong URL fails silently
                      at delivery time, so it is never rebuilt client-side. */}
                  {t.fireUrl && (
                    <SecretField
                      label="Fire URL"
                      value={t.fireUrl}
                      copyLabel={`fire URL for ${triggerTitle(t)}`}
                    />
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="flex flex-col gap-3">
          <PanelTabs
            ariaLabel="Automation sections"
            value={section}
            onValueChange={(next) => setSection(next as typeof section)}
            tabs={[
              { value: "runs", label: "Run history" },
              { value: "activity", label: "Activity" },
              { value: "history", label: "History" },
            ]}
          />
          {section === "activity" ? (
            <RoutineActivitySection routineId={routineId} />
          ) : section === "history" ? (
            <RoutineHistoryTab
              routine={routine}
              agents={agentLookup}
              projects={projectLookup}
              onRestored={(data) => {
                setRestoredSecrets(data.secretMaterials ?? []);
                void refresh();
              }}
            />
          ) : runsError ? (
            <p role="alert" className="text-sm" style={{ color: "var(--err)" }}>
              Failed to load run history.
            </p>
          ) : runs.length === 0 ? (
            <p className="text-sm italic" style={{ color: "var(--midnight-muted)" }}>
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
                        className="label-caps px-1.5 py-0.5 rounded-sm shrink-0"
                        style={{ background: "var(--edge)", color: "var(--midnight-muted)" }}
                      >
                        {r.source}
                      </span>
                      {isActiveStatus(r.status) && (
                        // Decorative — the status text right after it already
                        // reads "running" / "scheduled retry", so a retry row
                        // isn't announced twice. No per-run silence signal
                        // exists at this list granularity (unlike the live-run
                        // feed), so this always renders the default "running"
                        // tone rather than guessing at "stalled".
                        <LiveRunDot decorative testId={`routine-run-live-dot-${r.id}`} />
                      )}
                      <span
                        className="text-xs capitalize shrink-0"
                        style={{ color: runStatusColor(r.status) }}
                      >
                        {r.status.replace(/_/g, " ")}
                      </span>
                      {r.trigger && (
                        <span className="text-xs text-[var(--midnight-muted)] truncate">
                          {r.trigger.label ?? r.trigger.kind}
                        </span>
                      )}
                      {r.linkedIssue && linkedIssueUrl && (
                        <a
                          href={linkedIssueUrl}
                          onClick={(e) => handleShellLinkClick(e, linkedIssueUrl)}
                          className="text-xs hover:underline"
                          style={{ color: "var(--midnight)" }}
                        >
                          {r.linkedIssue.identifier ?? r.linkedIssue.id.slice(0, 8)}
                        </a>
                      )}
                      <span className="flex-1" />
                      {when && (
                        <span className="text-xs text-[var(--midnight-muted)] shrink-0">
                          {timeAgo(when)}
                        </span>
                      )}
                    </div>
                    {r.failureReason && (
                      <div className="text-xs pl-1" style={{ color: "var(--err)" }}>
                        {r.failureReason}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </section>

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
                style={{ color: "var(--midnight)" }}
              >
                {assigneeAgent.name}
              </Link>
            ) : assigneeName ? (
              <span style={{ color: "var(--midnight)" }}>{assigneeName}</span>
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
                style={{ color: "var(--midnight)" }}
              >
                {reviewerAgent.name}
              </Link>
            ) : reviewerAgentId ? (
              <span style={{ color: "var(--midnight)" }}>{reviewerAgentId}</span>
            ) : (
              <span data-empty="true" style={EMPTY_DASH_STYLE}>
                —
              </span>
            )}
          </PropertyRow>
          <PropertyRow label="Created">
            {routine.createdAt ? (
              <time dateTime={routine.createdAt} style={{ color: "var(--midnight-muted)" }}>
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
              <time dateTime={routine.updatedAt} style={{ color: "var(--midnight-muted)" }}>
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
          routineId={routineId}
          agents={agents}
          onArmed={() => {
            setTriggerDialog(null);
            void refresh();
          }}
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

      {restoredSecrets.length > 0 && (
        <EditorialDialog
          open
          onOpenChange={(o) => !o && setRestoredSecrets([])}
          title="Webhook triggers recreated"
          description="Restoring the revision recreated these webhook triggers with new URLs and secrets. Copy them now — each secret is shown only once."
          footer={
            <EditorialButton variant="primary" size="sm" onClick={() => setRestoredSecrets([])}>
              Done
            </EditorialButton>
          }
        >
          <div className="flex flex-col gap-4">
            {restoredSecrets.map((material) => (
              <div key={material.triggerId} className="flex flex-col gap-3">
                <SecretField label="Webhook URL" value={material.webhookUrl} />
                <SecretField label="Webhook secret" value={material.webhookSecret} />
              </div>
            ))}
          </div>
        </EditorialDialog>
      )}

      <RoutineRunVariablesDialog
        open={runDialogOpen}
        onOpenChange={setRunDialogOpen}
        routineName={routine.title}
        agents={agents}
        projects={projects}
        defaultProjectId={routine.projectId ?? null}
        defaultAssigneeAgentId={routine.assigneeAgentId ?? null}
        variables={routine.variables ?? []}
        isPending={busy}
        error={runDialogOpen ? actionError : null}
        onSubmit={(data) => void fireRun(data)}
      />

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

// Placeholder matcher mirroring upstream Paperclip's (`routine-variables.ts`):
// inner whitespace and markdown-escaped underscores (`{{pr\_url}}`, how WYSIWYG
// editors serialize `_`) both count as a reference.
const VAR_PLACEHOLDER_RE = /\{\{\s*([A-Za-z](?:\\_|[A-Za-z0-9_])*)\s*\}\}/g;

// Names referenced as `{{NAME}}` across the routine's title + description.
function referencedVariableNames(title: string, description: string): Set<string> {
  const names = new Set<string>();
  for (const source of [title, description]) {
    for (const m of source.matchAll(VAR_PLACEHOLDER_RE)) {
      names.add(m[1].replace(/\\_/g, "_"));
    }
  }
  return names;
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
  // Paperclip re-derives the variable set from the template on EVERY update
  // (`syncRoutineVariablesWithTemplate`), keeping only definitions the title or
  // description references as `{{NAME}}` — including on a save that doesn't send
  // `variables` at all. An unreferenced definition therefore vanishes behind a
  // 200, so block Save here rather than let it round-trip into silence.
  const referenced = referencedVariableNames(title, description);
  const unreferenced = namedVars.filter((v) => !referenced.has(v.name.trim()));
  const canSave = title.trim() !== "" && unreferenced.length === 0;
  const reviewerAgents = agents.filter(
    (agent) =>
      agent.id === reviewerAgentId ||
      (agent.id !== assigneeAgentId && agent.status !== "terminated"),
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
          <span className="text-sm text-[var(--midnight-soft)]">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className={INPUT_CLASS}
          />
        </label>
        <div className="block">
          <span className="text-sm text-[var(--midnight-soft)]">Assignee</span>
          <div className="mt-1">
            <AssigneePicker
              agents={agents}
              selectedAgentId={assigneeAgentId}
              onChange={selectAssignee}
              variant="inline"
              className="border border-[var(--edge)] rounded-sm px-3 py-2 bg-[var(--cream)]"
            />
          </div>
        </div>
        <div className="block">
          <span className="text-sm text-[var(--midnight-soft)]">Reviewer</span>
          <div className="mt-1">
            <AssigneePicker
              agents={reviewerAgents}
              selectedAgentId={reviewerAgentId}
              onChange={setReviewerAgentId}
              variant="inline"
              noun="reviewer"
              className="border border-[var(--edge)] rounded-sm px-3 py-2 bg-[var(--cream)]"
            />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="text-sm text-[var(--midnight-soft)]">Status</span>
            <div className="mt-1">
              <EditorialSelect
                options={STATUS_OPTIONS.map(([value, label]) => ({ value, label }))}
                value={status}
                onValueChange={setStatus}
              />
            </div>
          </label>
          <label className="block">
            <span className="text-sm text-[var(--midnight-soft)]">Priority</span>
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
          <span className="text-sm text-[var(--midnight-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            className={INPUT_CLASS}
          />
        </label>

        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="text-sm text-[var(--midnight-soft)]">Variables</span>
            <EditorialButton variant="ghost" size="sm" onClick={addVar}>
              <Plus size={16} aria-hidden="true" />
              Add variable
            </EditorialButton>
          </div>
          {unreferenced.length > 0 && (
            <p role="alert" className="text-xs" style={{ color: "var(--err)" }}>
              {unreferenced.map((v) => `{{${v.name.trim()}}}`).join(", ")}{" "}
              {unreferenced.length === 1 ? "is" : "are"} not used in the title or
              description. Add the placeholder there, or remove the variable — an
              automation only keeps variables its own text references.
            </p>
          )}
          {vars.length === 0 ? (
            <p className="text-xs italic" style={{ color: "var(--midnight-muted)" }}>
              No variables. Add one — and reference it as {"{{name}}"} in the title
              or description — to prompt for input at dispatch time.
            </p>
          ) : (
            <ul className="flex flex-col gap-3">
              {vars.map((v, i) => (
                <li
                  key={i}
                  className="flex flex-col gap-2 rounded-sm border border-[var(--edge)] p-2"
                >
                  <div className="grid grid-cols-[1fr_auto_auto] gap-2 items-center">
                    <input aria-label="Variable name"
                      value={v.name}
                      onChange={(e) => setVar(i, { name: e.target.value })}
                      placeholder="name"
                      className="border border-[var(--edge)] bg-[var(--cream)] rounded-md px-2 py-1 text-sm text-[var(--midnight)]"
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
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button
                          type="button"
                          onClick={() => removeVar(i)}
                          className="text-[var(--midnight-muted)] hover:text-[var(--err)]"
                          aria-label={`Remove variable ${v.name || i + 1}`}
                        >
                          <Trash2 size={16} aria-hidden="true" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent>Remove</TooltipContent>
                    </Tooltip>
                  </div>
                  <input aria-label="Variable label"
                    value={v.label}
                    onChange={(e) => setVar(i, { label: e.target.value })}
                    placeholder="label (optional)"
                    className="border border-[var(--edge)] bg-[var(--cream)] rounded-md px-2 py-1 text-sm text-[var(--midnight)]"
                  />
                  {v.type === "select" && (
                    <input aria-label="Variable options"
                      value={v.options}
                      onChange={(e) => setVar(i, { options: e.target.value })}
                      placeholder="options, comma-separated"
                      className="border border-[var(--edge)] bg-[var(--cream)] rounded-md px-2 py-1 text-sm text-[var(--midnight)]"
                    />
                  )}
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-[var(--midnight-muted)] w-14">Default</span>
                    <VarDefaultInput
                      v={v}
                      onChange={(next) => setVar(i, { defaultValue: next })}
                    />
                  </div>
                  <label className="flex items-center gap-2 text-xs text-[var(--midnight-soft)]">
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
    <input aria-label="Default value"
      type={inputType}
      value={v.defaultValue}
      onChange={(e) => onChange(e.target.value)}
      className="flex-1 border border-[var(--edge)] bg-[var(--cream)] rounded-md px-2 py-1 text-sm text-[var(--midnight)]"
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
  routineId,
  agents,
  submitting,
  error,
  onClose,
  onSubmit,
  onArmed,
}: {
  mode: "add" | "edit";
  trigger?: RoutineTriggerSummary;
  /** The automation being armed — the connected-app branch attaches to it. */
  routineId: string;
  agents: CompanyAgent[];
  submitting: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (body: TriggerSubmitBody) => void | Promise<void>;
  /** The connected-app branch talks to /automations directly, so it reports
   * completion here instead of through onSubmit. */
  onArmed: () => void;
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
  // What starts this — the SAME three-way choice the New-automation dialog
  // offers, because an automation that already exists can take any of them:
  // a routine is instructions + an agent, and its triggers are a set, not a
  // property of the door you came in through. Only choosable in "add" mode;
  // edit only reaches this dialog for an existing schedule trigger (the
  // Pencil affordance is gated to `t.kind === "schedule"`), so kind is fixed
  // there.
  const [rung, setRung] = useState<StartRung>("schedule");
  const kind =
    mode === "add" ? (rung === "webhook" ? "webhook" : "schedule") : (trigger?.kind ?? "schedule");
  const isConnectedApp = mode === "add" && rung === "connected_app";
  // Arming a connected app attaches to THIS routine — same call the create
  // dialog makes, same provider-side wiring, only with a target.
  const conn = useConnectedAppTrigger({ routineId });
  const [signingMode, setSigningMode] = useState("bearer");
  const [replayWindowSec, setReplayWindowSec] = useState(String(DEFAULT_REPLAY_WINDOW_SEC));
  const showReplayWindow = REPLAY_WINDOW_SIGNING_MODES.has(signingMode);
  const replayWindowNum = Number(replayWindowSec);
  const replayWindowValid =
    !showReplayWindow || (Number.isFinite(replayWindowNum) && replayWindowNum >= 30 && replayWindowNum <= 86400);

  const canSubmit = isConnectedApp
    ? conn.canSubmit
    : kind === "schedule"
      ? cron.trim() !== "" && timezone.trim() !== ""
      : replayWindowValid;

  async function submit() {
    if (!canSubmit) return;
    if (isConnectedApp) {
      await conn.submit();
      return;
    }
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

  if (conn.result) {
    return <ConnectedAppResultDialog ctl={conn} onDone={onArmed} />;
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={mode === "edit" ? "Edit schedule" : "Add trigger"}
      footer={
        <>
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton
            variant="primary"
            size="sm"
            onClick={submit}
            disabled={submitting || conn.submitting || !canSubmit}
          >
            {submitting || conn.submitting ? "Saving…" : mode === "add" ? "Add" : "Save"}
          </EditorialButton>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <DialogError message={isConnectedApp ? conn.err : error} />
        {mode === "add" && <StartRungPicker value={rung} onChange={setRung} />}
        {isConnectedApp ? (
          <ConnectedAppFields ctl={conn} agents={agents} />
        ) : kind === "schedule" ? (
          <>
            <ScheduleField value={cron} onChange={setCron} />
            <label className="block">
              <span className="text-sm text-[var(--midnight-soft)]">Timezone</span>
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
              <span className="text-sm text-[var(--midnight-soft)]">Signing mode</span>
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
              <span className="text-xs text-[var(--midnight-muted)]">
                The external app signs each request with this secret; Lightbulb
                verifies it.
              </span>
            </label>
            {showReplayWindow && (
              <label className="block">
                <span className="text-sm text-[var(--midnight-soft)]">
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
                <span className="text-xs text-[var(--midnight-muted)]">
                  How long a signed delivery stays valid (30–86400s).
                </span>
              </label>
            )}
          </>
        )}
        {/* Label + Enabled are fields of a trigger this dialog creates; the
            connected-app branch names and enables its own server-side. */}
        {!isConnectedApp && (
          <>
            <label className="block">
              <span className="text-sm text-[var(--midnight-soft)]">Label (optional)</span>
              <input
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                className={INPUT_CLASS}
              />
            </label>
            <label className="flex items-center gap-2 text-sm text-[var(--midnight-soft)]">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(e) => setEnabled(e.target.checked)}
              />
              Enabled
            </label>
          </>
        )}
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
          <span className="text-sm text-[var(--midnight-soft)]">Signing secret</span>
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
