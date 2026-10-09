"use client";

import { useCallback, useMemo, useState } from "react";
import { notifyApiError, notifyConversionGate, notifySuccess } from "@/lib/notify";
import { ArrowUpDown, Check, Repeat } from "lucide-react";
import {
  EditorialButton,
  EditorialDialog,
  EditorialSelect,
  PanelDataTable,
  PanelEmptyState,
  PanelMasthead,
  PanelToolbar,
  type PanelDataColumn,
} from "@/components/teams/shell";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { AssigneePicker } from "@/components/teams/issues/pickers/AssigneePicker";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useMarketplaceDrawer } from "@/components/marketplace/drawer/MarketplaceDrawerProvider";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { cn } from "@/lib/utils";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import { isAutoPaused } from "@/components/teams/shared/lib/routineHealth";
import { triggerSentence } from "@/components/teams/shared/triggerSentence";
import { SecretField } from "@/components/teams/shared/components/SecretField";
import { ScheduleField } from "@/components/teams/shared/components/ScheduleField";
import {
  ConnectedAppFields,
  ConnectedAppResultDialog,
  StartRungPicker,
  gateMessage,
  useConnectedAppTrigger,
  type StartRung,
} from "@/components/teams/shared/components/ConnectedAppTrigger";
import { withRoutineReviewer } from "@/components/teams/panels/routineExecutionPolicy";
import {
  SIGNING_MODE_OPTIONS,
  REPLAY_WINDOW_SIGNING_MODES,
  DEFAULT_REPLAY_WINDOW_SEC,
} from "@/components/teams/shared/webhookSigning";
import {
  RoutineRunVariablesDialog,
  type RoutineRunDialogSubmitData,
} from "@/components/teams/routines/RoutineRunVariablesDialog";
import type {
  CompanyAgent,
  IssueProject,
  RoutineTriggerSecretMaterial,
  RoutineVariable,
} from "@/components/teams/shared/types";

// Stable empty list so the run dialog's reset-on-open effect doesn't refire.
const EMPTY_VARIABLES: RoutineVariable[] = [];
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

// The /routines BFF is a pure passthrough of upstream Paperclip's
// `RoutineListItem` (paperclip/packages/shared/src/types/routine.ts), so a
// row carries `title` / `assigneeAgentId` / `triggers[]` / `lastTriggeredAt`,
// NOT the flat `name` / `cron` / `agent_id` an earlier scaffold assumed. We
// read the upstream fields first and fall back to the legacy flat names so any
// transitional row (or a test fixture using the old shape) still renders.
interface RoutineTriggerLite {
  id: string;
  kind: string;
  enabled?: boolean;
  cronExpression?: string | null;
  timezone?: string | null;
  nextRunAt?: string | null;
}

interface Routine {
  id: string;
  // Upstream → display title; legacy flat fallback kept for transitional rows.
  title?: string | null;
  name?: string | null;
  // Upstream assignee id; legacy flat fallback.
  assigneeAgentId?: string | null;
  agent_id?: string | null;
  // Upstream status ("active" | "paused" | "draft" | "archived"). The legacy
  // shape carried a bare `enabled` boolean instead.
  status?: string | null;
  enabled?: boolean;
  triggers?: RoutineTriggerLite[];
  // Variable definitions the routine declares (upstream RoutineListItem carries
  // `variables[]`). When non-empty, "Run now" must collect a value per variable
  // before firing — see RoutineRunVariablesDialog. Empty/absent → one-click run.
  variables?: RoutineVariable[];
  // Default project for runs (prefills the run dialog).
  projectId?: string | null;
  // Legacy flat cron (no triggers[] array). Upstream rows carry cron on each
  // trigger; this is read only as a fallback.
  cron?: string | null;
  lastTriggeredAt?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  prompt?: string;
  // Circuit-breaker auto-pause state — see isAutoPaused in shared/lib/routineHealth.
  autoPausedAt?: string | null;
  consecutiveFailureCount?: number | null;
  autoPauseReason?: string | null;
}

/* ── field accessors (tolerant of upstream + legacy shapes) ──────────────── */

function routineName(r: Routine): string {
  return r.title ?? r.name ?? "Untitled automation";
}

function routineAgentId(r: Routine): string | null {
  return r.assigneeAgentId ?? r.agent_id ?? null;
}

// A routine is enabled when its upstream status is "active" (paused/draft/
// archived are not), or — for a legacy flat row — when the bare `enabled`
// boolean is true.
function routineEnabled(r: Routine): boolean {
  if (r.status != null) return r.status === "active";
  return r.enabled === true;
}

// Plain-English "what starts this" subtitle for a routine's list row: picks
// the routine's defining trigger and describes it via triggerSentence. An
// enabled webhook wins over an enabled schedule — Google-seeded automations
// carry a maintenance schedule trigger alongside their webhook, and the row
// should read as "when an app calls this webhook", not as its upkeep cron.
// A legacy flat-cron row (no triggers[] array) synthesizes a schedule
// trigger so it still reads as a sentence, not a raw cron string.
function routineTriggerSentence(r: Routine): string {
  const triggers = r.triggers ?? [];
  const enabled = triggers.filter((t) => t.enabled !== false);
  const picked = enabled.find((t) => t.kind === "webhook") ?? enabled[0] ?? triggers[0];
  if (picked) return triggerSentence(picked);
  if (r.cron) return triggerSentence({ kind: "schedule", cronExpression: r.cron });
  return "No trigger configured";
}

// Earliest upcoming `nextRunAt` across the routine's enabled triggers, as an
// epoch-ms value for sorting. Routines with no upcoming run sort last.
function routineNextRunTs(r: Routine): number {
  let earliest = Number.POSITIVE_INFINITY;
  for (const t of r.triggers ?? []) {
    if (t.enabled === false || !t.nextRunAt) continue;
    const ts = new Date(t.nextRunAt).getTime();
    if (!Number.isNaN(ts) && ts < earliest) earliest = ts;
  }
  return earliest;
}

// A trigger's `nextRunAt` is a FUTURE timestamp, so render it future-relative
// ("in 8h"); an overdue/imminent one falls through to a coarse "soon". Mirrors
// RoutineDetailPanel.nextRunLabel without pulling in the past-only timeAgo.
function nextRunLabel(r: Routine): string | null {
  const ts = routineNextRunTs(r);
  if (!Number.isFinite(ts)) return null;
  const diffSec = Math.round((ts - Date.now()) / 1000);
  if (diffSec < 60) return "soon";
  if (diffSec < 3600) return `in ${Math.floor(diffSec / 60)}m`;
  if (diffSec < 86400) return `in ${Math.floor(diffSec / 3600)}h`;
  return `in ${Math.floor(diffSec / 86400)}d`;
}

/* ── enabled toggle switch ───────────────────────────────────────────────── */

// A clear ON/OFF switch in place of the old ambiguous checkbox-next-to-label
// (a box sitting beside the word "Disabled" reads as both state AND action).
// The KNOB position is the state: pushed right + forest = active, left +
// muted = paused. The label states the CURRENT state ("Active" / "Paused").
// In-theme (cream/forest editorial tokens); no shared Switch primitive exists,
// so it's a styled role="switch" button.
function RoutineToggle({
  enabled,
  disabled,
  onToggle,
  label,
}: {
  enabled: boolean;
  disabled?: boolean;
  onToggle: (next: boolean) => void;
  label: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        role="switch"
        aria-checked={enabled}
        aria-label={`${enabled ? "Pause" : "Activate"} ${label}`}
        disabled={disabled}
        onClick={() => onToggle(!enabled)}
        className={cn(
          "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors",
          "disabled:cursor-not-allowed disabled:opacity-50",
          "",
        )}
        style={{
          backgroundColor: enabled ? "var(--midnight)" : "var(--cream-deep)",
          borderColor: enabled ? "var(--midnight)" : "var(--edge)",
        }}
      >
        <span
          aria-hidden="true"
          className={cn(
            "inline-block h-3.5 w-3.5 transform rounded-full bg-cream shadow-sm transition-transform",
            enabled ? "translate-x-[18px]" : "translate-x-0.5",
          )}
        />
      </button>
      <span
        className="text-sm tabular-nums"
        style={{ color: enabled ? "var(--midnight)" : "var(--midnight-muted)" }}
      >
        {enabled ? "Active" : "Paused"}
      </span>
    </div>
  );
}

/* ── sort ────────────────────────────────────────────────────────────────── */

type RoutineSortField = "name" | "nextRun" | "agent" | "enabled";
type RoutineSortDir = "asc" | "desc";

const SORT_OPTIONS: ReadonlyArray<readonly [RoutineSortField, string]> = [
  ["name", "Name"],
  ["nextRun", "Next run"],
  ["agent", "Agent"],
  ["enabled", "Enabled"],
];

function sortRoutines(
  routines: Routine[],
  field: RoutineSortField,
  dir: RoutineSortDir,
  agentName: (id: string | null) => string,
): Routine[] {
  const mul = dir === "asc" ? 1 : -1;
  return [...routines].sort((a, b) => {
    let result = 0;
    switch (field) {
      case "name":
        result = routineName(a).localeCompare(routineName(b));
        break;
      case "nextRun":
        result = routineNextRunTs(a) - routineNextRunTs(b);
        break;
      case "agent":
        result = agentName(routineAgentId(a)).localeCompare(
          agentName(routineAgentId(b)),
        );
        break;
      case "enabled":
        // Enabled before disabled when ascending.
        result = Number(routineEnabled(b)) - Number(routineEnabled(a));
        break;
    }
    // Stable tiebreak on name so equal keys keep a deterministic order.
    if (result === 0) result = routineName(a).localeCompare(routineName(b));
    return mul * result;
  });
}

/* ── component ───────────────────────────────────────────────────────────── */

export function RoutinesPanel({ agentId }: { agentId?: string } = {}) {
  const { read, post, patch, del } = useTeamsApi();
  // In-shell (provider present) → open the detail view inline; standalone
  // under /teams (no provider) → fall back to <a>.
  const nav = useInlineTeamsNav();
  const { open: openMarketplace } = useMarketplaceDrawer();
  const { data, isLoading, error, mutate } = read<{ routines: Routine[] }>(
    "/routines",
  );
  // Resolve assignee ids → display names (the same id→name idiom the issues
  // panels use). The roster is best-effort: a missing read just falls back to
  // the raw id, so a slow /agents fetch never blanks the list.
  const { data: agentsData } = read<{ agents: CompanyAgent[] }>("/agents");
  const agents = agentsData?.agents ?? [];

  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  // When a routine that DEFINES variables is run, hold it here so the variables
  // editor opens; the one-click path (no variables) never sets this.
  const [runVars, setRunVars] = useState<Routine | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const { data: projectsData } = read<{ projects: IssueProject[] }>("/projects");
  const [search, setSearch] = useState("");
  // Standalone-only agent filter (hidden when the intrinsic `agentId` prop
  // scopes the panel — see filtering note below).
  const [agentPick, setAgentPick] = useState<string | null>(null);
  const [sortField, setSortField] = useState<RoutineSortField>("name");
  const [sortDir, setSortDir] = useState<RoutineSortDir>("asc");

  const agentName = useCallback(
    (id: string | null): string => {
      if (!id) return "Unassigned";
      return agents.find((a) => a.id === id)?.name ?? id;
    },
    [agents],
  );

  const routines = data?.routines;
  // `agentId` (when set by the per-agent inspector) is an INTRINSIC scope: the
  // panel is mounted under a single agent, so we filter to its routines with no
  // dismissible chip. Standalone under /teams the prop is absent → the toolbar
  // agent picker (`agentPick`) drives an OPTIONAL, user-dismissible filter.
  // The free-text `search` filter applies on top in all modes.
  const filtered = useMemo(() => {
    const list = routines ?? [];
    const q = search.trim().toLowerCase();
    const scoped = agentId ?? agentPick;
    const matched = list.filter(
      (r) =>
        (!scoped || routineAgentId(r) === scoped) &&
        (!q || routineName(r).toLowerCase().includes(q)),
    );
    return sortRoutines(matched, sortField, sortDir, agentName);
  }, [routines, agentId, agentPick, search, sortField, sortDir, agentName]);

  async function toggle(r: Routine, next: boolean) {
    setBusy(r.id);
    try {
      // Upstream has no `enabled` field on the routine row — enable/disable
      // is a status flip (`active` | `paused`), mirroring upstream's
      // `nextRoutineStatus` (paperclip/ui/src/components/RoutineList.tsx).
      await patch(`/routines/${r.id}`, { status: next ? "active" : "paused" });
      mutate();
    } catch (err) {
      notifyApiError(err, "Couldn't update the automation");
    } finally {
      setBusy(null);
    }
  }

  async function remove(id: string) {
    if (!confirm("Delete this automation?")) return;
    setBusy(id);
    try {
      await del(`/routines/${id}`);
      mutate();
    } catch (err) {
      notifyApiError(err, "Couldn't delete the automation");
    } finally {
      setBusy(null);
    }
  }

  // Fire the run. `variables` is included only when the routine declares any —
  // upstream `runRoutineSchema` accepts `variables: record<string, value>` and
  // forwards it verbatim; omitting the key entirely keeps the one-click body
  // unchanged for variable-less routines.
  async function fireRun(routine: Routine, data?: RoutineRunDialogSubmitData) {
    setBusy(routine.id);
    try {
      // `source`'s enum ("manual" | "api") distinguishes UI-fired runs from
      // cron/event-fired runs in upstream run history.
      const body: Record<string, unknown> = { source: "manual" };
      const variables = data?.variables;
      if (variables && Object.keys(variables).length > 0) body.variables = variables;
      if (data?.assigneeAgentId) body.assigneeAgentId = data.assigneeAgentId;
      if (data?.projectId) body.projectId = data.projectId;
      await post(`/routines/${routine.id}/run`, body);
      mutate();
      // The run is dispatched server-side and nothing on this row changes when
      // it starts — no status flip, no new column — so a successful Run now was
      // indistinguishable from a dead button. The toast is the acknowledgement,
      // and its action opens the automation, where Run history is.
      notifySuccess(`Running ${routineName(routine)}…`, {
        dedupeKey: `routine-run:${routine.id}`,
        ...(nav
          ? {
              action: {
                label: "View runs",
                onClick: () => nav.openDetail("routine-detail", routine.id),
              },
            }
          : {}),
      });
    } finally {
      setBusy(null);
    }
  }

  function runNow(routine: Routine) {
    // A routine that DEFINES variables can't be fired blind — open the editor
    // to collect a value per variable. With none defined, run in one click.
    if ((routine.variables?.length ?? 0) > 0) {
      setRunVars(routine);
      return;
    }
    // The one-click path has no dialog to render a failure into (the variables
    // path keeps its inline error — see RoutineRunVariablesDialog's onSubmit), so it reports
    // through the toast surface instead of failing silently.
    void fireRun(routine).catch((e) => {
      if (notifyConversionGate(e) === null) {
        notifyApiError(e, "Couldn't start that run.");
      }
    });
  }

  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--err)]">
        Error: {String(error)}
      </div>
    );

  const columns: PanelDataColumn<Routine>[] = [
    {
      key: "name",
      header: "Name",
      cell: (r) => {
        const next = nextRunLabel(r);
        return (
          <div>
            <div className="font-medium text-[var(--midnight)]">
              {routineName(r)}
            </div>
            <div className="text-xs text-[var(--midnight-muted)]">
              {routineTriggerSentence(r)}
              {" · "}
              {agentName(routineAgentId(r))}
              {next ? ` · next ${next}` : ""}
            </div>
          </div>
        );
      },
    },
    {
      key: "enabled",
      header: "Status",
      cell: (r) => (
        <div className="flex items-center gap-2">
          <RoutineToggle
            enabled={routineEnabled(r)}
            disabled={busy === r.id}
            onToggle={(next) => toggle(r, next)}
            label={routineName(r)}
          />
          {isAutoPaused(r) && (
            <span
              className="inline-flex items-center gap-1 text-xs"
              style={{ color: "var(--warn)" }}
              title="Auto-paused after repeated failures"
            >
              <span
                aria-hidden="true"
                className="h-1.5 w-1.5 rounded-full"
                style={{ backgroundColor: "var(--warn)" }}
              />
              auto-paused
            </span>
          )}
        </div>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (r) => (
        <div className="flex items-center justify-end gap-2">
          <EditorialButton
            variant="ghost"
            size="sm"
            disabled={busy === r.id}
            aria-label={`Run ${routineName(r)} now`}
            onClick={() => runNow(r)}
          >
            Run now
          </EditorialButton>
          <button
            onClick={() => remove(r.id)}
            disabled={busy === r.id}
            className="text-sm text-[var(--midnight-muted)] hover:text-[var(--err)] hover:underline"
          >
            Delete
          </button>
        </div>
      ),
    },
    {
      key: "open",
      header: "",
      align: "right",
      cell: (r) =>
        nav ? (
          <button
            type="button"
            onClick={() => nav.openDetail("routine-detail", r.id)}
            className="text-sm text-[var(--midnight)] hover:underline"
          >
            Open →
          </button>
        ) : (
          <a
            href={teamsDetailUrl("routine", r.id)}
            onClick={(e) =>
              handleShellLinkClick(e, teamsDetailUrl("routine", r.id))
            }
            className="text-sm text-[var(--midnight)] hover:underline"
          >
            Open →
          </a>
        ),
    },
  ];

  return (
    <div className="editorial-page flex flex-col gap-4 p-4 sm:p-5 lg:p-6 max-w-[1600px] mx-auto w-full">
      <PanelMasthead
        overline="Automations"
        title="Automations"
        actions={
          <EditorialButton variant="primary" onClick={() => setCreating(true)}>
            New automation
          </EditorialButton>
        }
      />
      <PanelToolbar
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder="Find automation..."
        searchAriaLabel="Search automations"
        filters={
          <>
            {/* Agent filter — standalone-only. The inspector mount already
                scopes to one agent via the intrinsic `agentId` prop, so the
                picker would be redundant there. */}
            {!agentId && (
              <AssigneePicker
                agents={agents}
                selectedAgentId={agentPick}
                onChange={setAgentPick}
                variant="inline"
                className="border border-[var(--edge)]"
              />
            )}
            <SortMenu
              field={sortField}
              dir={sortDir}
              onSelect={(f) => {
                if (f === sortField) {
                  setSortDir((d) => (d === "asc" ? "desc" : "asc"));
                } else {
                  setSortField(f);
                  setSortDir("asc");
                }
              }}
            />
          </>
        }
      />
      {/*
        Cross-routine "Recent runs" view — DEFERRED.

        Upstream's Routines page (paperclip/ui/src/pages/Routines.tsx) has a
        list-level "Recent Runs" tab, but it is NOT a routine-runs aggregation:
        it reads `GET /issues?originKind=routine_execution` and renders the
        result through the shared IssuesList. Our /issues BFF whitelists its
        query params (issues.py:31-37) and does NOT forward `originKind`, and
        there is no global routine-runs endpoint — `list_routine_runs` is
        strictly per-routine (`GET /routines/{id}/runs`). Porting the tab
        would require a NEW BFF param/endpoint that the slim Teams surface
        doesn't expose today. The per-routine Run history in RoutineDetailPanel
        already covers the "what ran, when, did it fail" need, so we defer the
        cross-routine view rather than invent backend surface for it.
      */}
      {isLoading ? (
        <div className="font-editorial-serif text-xl italic text-[var(--midnight-muted)]">
          Loading…
        </div>
      ) : filtered.length === 0 ? (
        <PanelEmptyState
          icon={Repeat}
          headline="No automations yet."
          supporting={
            search || agentPick
              ? "No automations match your filters."
              : "Build one here, or hire an agent from the marketplace — any automations it was published with arrive paused, ready for you to switch on."
          }
          // Canned automation lives in the marketplace (published agents carry
          // their routines, with provenance), not in a second catalog here.
          action={search || agentPick ? undefined : { label: "Build one", onClick: () => setCreating(true) }}
          secondaryAction={
            search || agentPick ? undefined : { label: "Hire an agent", onClick: () => openMarketplace({ view: "browse" }) }
          }
        />
      ) : (
        <PanelDataTable<Routine>
          rows={filtered}
          columns={columns}
          rowKey={(r) => r.id}
        />
      )}

      {creating && (
        <CreateRoutineDialog
          agents={agents}
          routines={routines ?? []}
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            mutate();
          }}
        />
      )}

      <RoutineRunVariablesDialog
        open={runVars !== null}
        onOpenChange={(next) => {
          if (!next) {
            setRunVars(null);
            setRunError(null);
          }
        }}
        routineName={runVars ? routineName(runVars) : null}
        agents={agents}
        projects={projectsData?.projects ?? []}
        defaultProjectId={runVars?.projectId ?? null}
        defaultAssigneeAgentId={runVars ? routineAgentId(runVars) : null}
        variables={runVars?.variables ?? EMPTY_VARIABLES}
        isPending={runVars !== null && busy === runVars.id}
        error={runError}
        onSubmit={async (data) => {
          if (!runVars) return;
          setRunError(null);
          try {
            await fireRun(runVars, data);
            setRunVars(null);
          } catch (e) {
            // A trial-gate 402 renders the upgrade toast instead of dialog text.
            if (notifyConversionGate(e) === null) {
              setRunError(e instanceof Error ? e.message : String(e));
            }
          }
        }}
      />
    </div>
  );
}

/* ── sort menu (Tasks-list idiom) ────────────────────────────────────────── */

function SortMenu({
  field,
  dir,
  onSelect,
}: {
  field: RoutineSortField;
  dir: RoutineSortDir;
  onSelect: (field: RoutineSortField) => void;
}) {
  return (
    <Popover>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label="Sort automations"
              className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-[var(--edge)] text-[var(--midnight-muted)] transition-colors hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
            >
              <ArrowUpDown size={16} />
            </button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>Sort</TooltipContent>
      </Tooltip>
      <PopoverContent align="end" className="w-44 p-1">
        {SORT_OPTIONS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => onSelect(value)}
            className={cn(
              "flex w-full items-center justify-between rounded-md px-2 py-1.5 text-ui transition-colors",
              value === field
                ? "bg-[var(--cream-deeper)] text-[var(--midnight)]"
                : "text-[var(--midnight-soft)] hover:bg-[var(--cream-deep)]",
            )}
          >
            <span>{label}</span>
            {value === field ? (
              <span className="text-xs text-[var(--midnight-muted)]">
                {dir === "asc" ? "↑" : "↓"}
              </span>
            ) : (
              <Check size={16} className="opacity-0" aria-hidden="true" />
            )}
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
}

// The target-picker's "create a new one" option. A sentinel rather than an
// empty string because EditorialSelect (Radix) treats "" as no-value, which
// would render the placeholder instead of this deliberate default.
const NEW_ROUTINE = "__new__";

function CreateRoutineDialog({
  agents,
  routines,
  onClose,
  onCreated,
}: {
  agents: CompanyAgent[];
  // The panel's already-fetched list, reused for the connected-app rung's
  // target picker — attaching a trigger needs no second read.
  routines: Routine[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  const [rung, setRung] = useState<StartRung>("schedule");
  const [title, setTitle] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [signingMode, setSigningMode] = useState("bearer");
  const [replayWindowSec, setReplayWindowSec] = useState(
    String(DEFAULT_REPLAY_WINDOW_SEC),
  );
  const [agentId, setAgentId] = useState<string | null>(null);
  const [reviewerAgentId, setReviewerAgentId] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Set once the webhook branch's create call returns — the one-time URL +
  // secret then replace the form (mirrors RoutineDetailPanel's TriggerDialog
  // flow, which this rung reuses).
  const [createdWebhook, setCreatedWebhook] =
    useState<RoutineTriggerSecretMaterial | null>(null);

  // Which automation the connected-app trigger lands on: NEW_ROUTINE (the
  // default, and the unchanged 1-click path) or an existing routine's id,
  // which attaches the trigger to it and leaves its own work alone.
  const [connRoutineId, setConnRoutineId] = useState<string>(NEW_ROUTINE);
  const attachRoutineId = connRoutineId === NEW_ROUTINE ? null : connRoutineId;
  // Everything else about "a connected app fires this" — the app/event
  // picker, the provider-side wiring and the finish dialog — is shared with
  // RoutineDetailPanel's Add-trigger door.
  const conn = useConnectedAppTrigger({
    routineId: attachRoutineId,
    // A different app may not even list the same automations as sensible
    // targets — and a stale one silently attaching is worse than re-picking,
    // so switching apps drops back to "create a new automation".
    onProviderChange: () => setConnRoutineId(NEW_ROUTINE),
  });

  const showReplayWindow = REPLAY_WINDOW_SIGNING_MODES.has(signingMode);
  const replayWindowNum = Number(replayWindowSec);
  const replayWindowValid =
    !showReplayWindow ||
    (Number.isFinite(replayWindowNum) &&
      replayWindowNum >= 30 &&
      replayWindowNum <= 86400);
  const canSubmit =
    rung === "connected_app"
      ? conn.canSubmit
      : title.trim() !== "" &&
        (rung === "schedule" ? cron.trim() !== "" : replayWindowValid);
  // The connected-app rung owns its own error state (different API); the
  // other two share the dialog's.
  const shownErr = rung === "connected_app" ? conn.err : err;
  const reviewerAgents = agents.filter(
    (agent) => agent.id !== agentId && agent.status !== "terminated",
  );

  function selectAssignee(nextAgentId: string | null) {
    setAgentId(nextAgentId);
    if (nextAgentId === reviewerAgentId) setReviewerAgentId(null);
  }

  async function submit() {
    if (!canSubmit) return;
    if (rung === "connected_app") {
      await conn.submit();
      return;
    }
    setSubmitting(true);
    setErr(null);
    try {
      // Two-step create, mirroring upstream (paperclip/ui/src/pages/
      // Routines.tsx + RoutineDetail.tsx): the routine row is a titled,
      // assignable unit; the trigger is a SEPARATE resource attached after.
      // 1. Create the routine (no trigger fields here — those 422 upstream on
      //    the routine endpoint). The old "prompt" maps to `description`.
      const routine = await post<{ id: string }>("/routines", {
        title: title.trim(),
        assignee_agent_id: agentId ?? undefined,
        ...(reviewerAgentId
          ? { execution_policy: withRoutineReviewer(null, reviewerAgentId) }
          : {}),
        description: description.trim() || undefined,
        status: "active",
      });
      if (rung === "schedule") {
        // 2. Attach the schedule trigger that carries the cron expression.
        //    Timezone matches upstream's `getLocalTimezone()` (browser TZ).
        await post(`/routines/${routine.id}/triggers`, {
          kind: "schedule",
          cron_expression: cron.trim(),
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        });
        onCreated();
        return;
      }
      // "Any other app" — the webhook branch, reusing the same
      // signing-mode/replay-window fields and create call as
      // RoutineDetailPanel's TriggerDialog.
      const res = await post<{
        secretMaterial?: RoutineTriggerSecretMaterial | null;
      }>(`/routines/${routine.id}/triggers`, {
        kind: "webhook",
        signing_mode: signingMode,
        replay_window_sec: showReplayWindow ? replayWindowNum : undefined,
      });
      if (res?.secretMaterial) {
        setCreatedWebhook(res.secretMaterial);
        setSubmitting(false);
      } else {
        onCreated();
      }
    } catch (e) {
      if (notifyConversionGate(e) === null) {
        setErr(gateMessage(e));
      }
      setSubmitting(false);
    }
  }

  if (createdWebhook) {
    return (
      <EditorialDialog
        open
        onOpenChange={(o) => !o && onCreated()}
        title="Webhook created"
        description="Copy the URL and secret now — the secret is shown only once."
        footer={
          <EditorialButton variant="primary" size="sm" onClick={onCreated}>
            Done
          </EditorialButton>
        }
      >
        <div className="flex flex-col gap-3">
          <SecretField label="Webhook URL" value={createdWebhook.webhookUrl} />
          <SecretField
            label="Webhook secret"
            value={createdWebhook.webhookSecret}
          />
        </div>
      </EditorialDialog>
    );
  }

  if (conn.result) {
    return <ConnectedAppResultDialog ctl={conn} onDone={onCreated} />;
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New automation"
    >
      <div className="flex flex-col gap-4">
        <StartRungPicker value={rung} onChange={setRung} />
        {/* Title/Assignee/Description belong to a routine this dialog CREATES.
            The connected-app rung has its own pair (pre-filled from the
            registry template, and editable) inside ConnectedAppFields, plus a
            target picker that can send the trigger to an automation that
            already has all three. */}
        {rung !== "connected_app" && (
          <>
            <label className="block">
              <span className="text-sm text-[var(--midnight-soft)]">Title</span>
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
              />
            </label>
            {/* Agent picker replaces the old raw "Agent ID" text input — the
                same editorial AssigneePicker the issues surface uses.
                Assignee is optional upstream (a routine can be created
                unassigned). */}
            <div className="block">
              <span className="text-sm text-[var(--midnight-soft)]">Assignee</span>
              <div className="mt-1">
                <AssigneePicker
                  agents={agents}
                  selectedAgentId={agentId}
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
          </>
        )}

        {rung === "connected_app" && (
          <ConnectedAppFields ctl={conn} agents={agents}>
            {/* Where the trigger lands. "Create a new automation" is the
                default, so the existing one-click path is unchanged; picking
                an automation the owner already wrote ADDS the trigger to it
                and leaves their instructions, assignee, reviewers, variables
                and schedule as written (the shapes that need maintenance text
                append a clearly-marked block, disclosed below). */}
            {conn.eventIds.length > 0 && routines.length > 0 && (
              <label className="block">
                <span className="text-sm text-[var(--midnight-soft)]">Automation</span>
                <div className="mt-1">
                  <EditorialSelect
                    options={[
                      { value: NEW_ROUTINE, label: "Create a new automation" },
                      ...routines.map((r) => ({
                        value: r.id,
                        label: routineName(r),
                      })),
                    ]}
                    value={connRoutineId}
                    onValueChange={setConnRoutineId}
                  />
                </div>
                <span className="text-xs text-[var(--midnight-muted)]">
                  {!attachRoutineId
                    ? "Writes a new automation from this event's template."
                    : conn.attachDisclosure
                      ? "Adds this trigger to that automation. Its assignee, reviewers and schedule stay as they are."
                      : "Adds this trigger to that automation. Its instructions, assignee and schedule stay as they are."}
                </span>
              </label>
            )}
          </ConnectedAppFields>
        )}

        {rung === "schedule" && <ScheduleField value={cron} onChange={setCron} />}

        {rung === "webhook" && (
          <>
            <label className="block">
              <span className="text-sm text-[var(--midnight-soft)]">
                Signing mode
              </span>
              <div className="mt-1">
                <EditorialSelect
                  options={SIGNING_MODE_OPTIONS.map(([value, optLabel]) => ({
                    value,
                    label: optLabel,
                  }))}
                  value={signingMode}
                  onValueChange={setSigningMode}
                />
              </div>
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
                  className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
                />
              </label>
            )}
          </>
        )}

        {rung !== "connected_app" && (
          <label className="block">
            <span className="text-sm text-[var(--midnight-soft)]">
              Description
            </span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded-sm px-3 py-2 mt-1 text-[var(--midnight)]"
              rows={3}
            />
          </label>
        )}
        {shownErr && (
          <div
            className="text-[var(--err)] text-sm rounded-sm px-3 py-2"
            style={{
              border:
                "1px solid var(--err)",
              background:
                "var(--cream)",
            }}
          >
            {shownErr}
          </div>
        )}
        <div className="flex justify-end gap-2 mt-2">
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton
            variant="primary"
            size="sm"
            onClick={submit}
            disabled={submitting || !canSubmit}
          >
            {submitting ? "Creating…" : "Create"}
          </EditorialButton>
        </div>
      </div>
    </EditorialDialog>
  );
}
