"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { notifyConversionGate } from "@/lib/notify";
import { ArrowUpDown, Blocks, Cable, Check, Clock, Repeat } from "lucide-react";
import type { LucideIcon } from "lucide-react";
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
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { AssigneePicker } from "@/components/teams/issues/pickers/AssigneePicker";
import { AutomationGoogleAccount } from "@/components/teams/panels/AutomationGoogleAccount";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useApi } from "@/lib/api";
import { useConnections } from "@/hooks/useConnections";
import { cn } from "@/lib/utils";
import { handleShellLinkClick, teamsDetailUrl } from "@/lib/shellNav";
import { isAutoPaused } from "@/components/teams/shared/lib/routineHealth";
import { triggerSentence } from "@/components/teams/shared/triggerSentence";
import { SecretField } from "@/components/teams/shared/components/SecretField";
import {
  SIGNING_MODE_OPTIONS,
  REPLAY_WINDOW_SIGNING_MODES,
  DEFAULT_REPLAY_WINDOW_SEC,
} from "@/components/teams/shared/webhookSigning";
import type {
  CompanyAgent,
  RoutineTriggerSecretMaterial,
  RoutineVariable,
  RoutineVariableValue,
} from "@/components/teams/shared/types";

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
  // before firing — see RunRoutineDialog. Empty/absent → one-click run.
  variables?: RoutineVariable[];
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

/** Human copy for a blocked-gate 402 (e.g. routines_require_subscription).
 *
 * The shared API client now promotes a structured `{blocked, code, message}`
 * detail's message to `ApiError.message` too, so for ApiErrors this is
 * equivalent to reading `e.message` — kept because it also covers non-ApiError
 * values via the `String(e)` fallback at the call sites below.
 */
function gateMessage(e: unknown): string {
  const body = (e as { body?: { detail?: { message?: string } } })?.body;
  const msg = body?.detail?.message;
  return typeof msg === "string" && msg ? msg : String(e);
}

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
          "relative inline-flex h-5 w-9 flex-shrink-0 items-center rounded-full border transition-colors",
          "disabled:cursor-not-allowed disabled:opacity-50",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--forest)] focus-visible:ring-offset-1",
        )}
        style={{
          backgroundColor: enabled ? "var(--forest)" : "var(--cream-deeper)",
          borderColor: enabled ? "var(--forest)" : "var(--edge)",
        }}
      >
        <span
          aria-hidden="true"
          className={cn(
            "inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow-sm transition-transform",
            enabled ? "translate-x-[18px]" : "translate-x-0.5",
          )}
        />
      </button>
      <span
        className="text-sm tabular-nums"
        style={{ color: enabled ? "var(--ink)" : "var(--mute)" }}
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
    } finally {
      setBusy(null);
    }
  }

  // Fire the run. `variables` is included only when the routine declares any —
  // upstream `runRoutineSchema` accepts `variables: record<string, value>` and
  // forwards it verbatim; omitting the key entirely keeps the one-click body
  // unchanged for variable-less routines.
  async function fireRun(
    routine: Routine,
    variables?: Record<string, RoutineVariableValue>,
  ) {
    setBusy(routine.id);
    try {
      // `source`'s enum ("manual" | "api") distinguishes UI-fired runs from
      // cron/event-fired runs in upstream run history.
      const body: { source: string; variables?: Record<string, RoutineVariableValue> } = {
        source: "manual",
      };
      if (variables && Object.keys(variables).length > 0) body.variables = variables;
      await post(`/routines/${routine.id}/run`, body);
      mutate();
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
    void fireRun(routine);
  }

  if (error)
    return (
      <div className="editorial-page p-8 text-[var(--danger)]">
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
            <div className="font-medium text-[var(--ink)]">
              {routineName(r)}
            </div>
            <div className="text-xs text-[var(--mute)]">
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
            className="text-sm text-[var(--mute)] hover:text-[var(--danger)] hover:underline"
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
            className="text-sm text-[var(--forest)] hover:underline"
          >
            Open →
          </button>
        ) : (
          <a
            href={teamsDetailUrl("routine", r.id)}
            onClick={(e) =>
              handleShellLinkClick(e, teamsDetailUrl("routine", r.id))
            }
            className="text-sm text-[var(--forest)] hover:underline"
          >
            Open →
          </a>
        ),
    },
  ];

  return (
    <div className="editorial-page flex flex-col gap-6 p-4 sm:p-6 lg:p-8 max-w-[1600px] mx-auto w-full">
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
        <div className="font-editorial-serif text-xl italic text-[var(--mute)]">
          Loading…
        </div>
      ) : filtered.length === 0 ? (
        <PanelEmptyState
          icon={Repeat}
          headline="No automations yet."
          supporting={
            search || agentPick
              ? "No automations match your filters."
              : "Create an automation to define a recurring workflow that fires on a schedule or trigger."
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
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            mutate();
          }}
        />
      )}

      {runVars && (
        <RunRoutineDialog
          routine={runVars}
          submitting={busy === runVars.id}
          onClose={() => setRunVars(null)}
          onRun={async (variables) => {
            await fireRun(runVars, variables);
            setRunVars(null);
          }}
        />
      )}
    </div>
  );
}

/* ── run-now variables editor ────────────────────────────────────────────── */

// The default value to seed a variable input with, coerced to the string the
// controlled inputs hold. Booleans use a "true"/"false" select, numbers reuse
// the text input (validated on submit), so everything is editable as a string
// and converted back to its declared type only at run time.
function initialVarString(v: RoutineVariable): string {
  if (v.defaultValue == null) return v.type === "boolean" ? "false" : "";
  return String(v.defaultValue);
}

// Convert a collected string back to the variable's declared type for the run
// body. Upstream `routineVariableValueSchema` is string | number | boolean.
function coerceVarValue(
  v: RoutineVariable,
  raw: string,
): RoutineVariableValue {
  if (v.type === "boolean") return raw === "true";
  if (v.type === "number") {
    const n = Number(raw);
    return Number.isFinite(n) ? n : raw;
  }
  return raw;
}

function variableLabel(v: RoutineVariable): string {
  return v.label?.trim() || v.name;
}

// Collects a value per defined variable, then fires the run. Required text /
// number / select fields must be non-empty; booleans always have a value.
function RunRoutineDialog({
  routine,
  submitting,
  onClose,
  onRun,
}: {
  routine: Routine;
  submitting: boolean;
  onClose: () => void;
  onRun: (variables: Record<string, RoutineVariableValue>) => void | Promise<void>;
}) {
  const vars = routine.variables ?? [];
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(vars.map((v) => [v.name, initialVarString(v)])),
  );
  const [err, setErr] = useState<string | null>(null);

  function setVar(name: string, next: string) {
    setValues((prev) => ({ ...prev, [name]: next }));
  }

  // A required variable is "missing" when its trimmed string is empty. Booleans
  // are never missing (they always hold "true"/"false").
  const missing = vars.filter(
    (v) =>
      v.required !== false &&
      v.type !== "boolean" &&
      (values[v.name] ?? "").trim() === "",
  );
  const canRun = missing.length === 0;

  async function submit() {
    if (!canRun) return;
    const collected: Record<string, RoutineVariableValue> = {};
    for (const v of vars) {
      const raw = values[v.name] ?? "";
      // Omit a blank optional variable rather than coercing "" → 0 (number) or
      // "" (text). Required vars can't be blank here — canRun gates them.
      if (v.required === false && v.type !== "boolean" && raw.trim() === "") {
        continue;
      }
      collected[v.name] = coerceVarValue(v, raw);
    }
    setErr(null);
    try {
      await onRun(collected);
    } catch (e) {
      // Surface the failure in the dialog rather than leaving the user with a
      // button that just flips back to "Run" — mirrors CreateRoutineDialog.
      // A trial-gate 402 renders the upgrade toast instead of dialog text.
      if (notifyConversionGate(e) === null) {
        setErr(e instanceof Error ? e.message : String(e));
      }
    }
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Run ${routineName(routine)}`}
      description="Provide values for this automation's variables, then run it now."
      footer={
        <>
          <EditorialButton variant="secondary" size="sm" onClick={onClose}>
            Cancel
          </EditorialButton>
          <EditorialButton
            variant="primary"
            size="sm"
            onClick={submit}
            disabled={submitting || !canRun}
          >
            {submitting ? "Running…" : "Run"}
          </EditorialButton>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {err && (
          <div
            className="text-[var(--danger)] text-sm rounded px-3 py-2"
            style={{
              border:
                "1px solid color-mix(in srgb, var(--danger) 20%, transparent)",
              background: "color-mix(in srgb, var(--danger) 6%, transparent)",
            }}
          >
            {err}
          </div>
        )}
        {vars.map((v) => {
          const required = v.required !== false;
          const label = `${variableLabel(v)}${required ? " *" : ""}`;
          if (v.type === "select") {
            return (
              <label key={v.name} className="block">
                <span className="text-sm text-[var(--ink-soft)]">{label}</span>
                <div className="mt-1">
                  <EditorialSelect
                    options={(v.options ?? []).map((o) => ({
                      value: o,
                      label: o,
                    }))}
                    value={values[v.name] || undefined}
                    onValueChange={(next) => setVar(v.name, next)}
                    placeholder="Select…"
                  />
                </div>
              </label>
            );
          }
          if (v.type === "boolean") {
            return (
              <label key={v.name} className="block">
                <span className="text-sm text-[var(--ink-soft)]">
                  {variableLabel(v)}
                </span>
                <div className="mt-1">
                  <EditorialSelect
                    options={[
                      { value: "true", label: "True" },
                      { value: "false", label: "False" },
                    ]}
                    value={values[v.name] || "false"}
                    onValueChange={(next) => setVar(v.name, next)}
                  />
                </div>
              </label>
            );
          }
          if (v.type === "textarea") {
            return (
              <label key={v.name} className="block">
                <span className="text-sm text-[var(--ink-soft)]">{label}</span>
                <textarea
                  value={values[v.name] ?? ""}
                  onChange={(e) => setVar(v.name, e.target.value)}
                  rows={3}
                  className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
                />
              </label>
            );
          }
          // text + number share the text input; number is coerced on submit.
          return (
            <label key={v.name} className="block">
              <span className="text-sm text-[var(--ink-soft)]">{label}</span>
              <input
                type={v.type === "number" ? "number" : "text"}
                value={values[v.name] ?? ""}
                onChange={(e) => setVar(v.name, e.target.value)}
                className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
              />
            </label>
          );
        })}
      </div>
    </EditorialDialog>
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
      <PopoverTrigger asChild>
        <button
          type="button"
          title="Sort"
          aria-label="Sort automations"
          className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-[var(--edge)] text-[var(--mute)] transition-colors hover:bg-[var(--cream-deeper)] hover:text-[var(--ink)]"
        >
          <ArrowUpDown className="h-3.5 w-3.5" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-44 p-1">
        {SORT_OPTIONS.map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => onSelect(value)}
            className={cn(
              "flex w-full items-center justify-between rounded-sm px-2 py-1.5 text-sm transition-colors",
              value === field
                ? "bg-[var(--cream-deeper)] text-[var(--ink)]"
                : "text-[var(--ink-soft)] hover:bg-[var(--cream-deeper)]",
            )}
          >
            <span>{label}</span>
            {value === field ? (
              <span className="text-xs text-[var(--mute)]">
                {dir === "asc" ? "↑" : "↓"}
              </span>
            ) : (
              <Check className="h-3.5 w-3.5 opacity-0" aria-hidden="true" />
            )}
          </button>
        ))}
      </PopoverContent>
    </Popover>
  );
}

/* ── create dialog: "what should start this?" trigger-type picker ───────── */

// The three ways a routine can be kicked off today. (Email intake and
// agent-watch triggers are on the roadmap but deliberately not surfaced here —
// a picker padded with disabled "coming soon" options is clutter, not a
// feature; they'll appear when they actually work.)
type StartRung = "connected_app" | "webhook" | "schedule";

const START_RUNGS: ReadonlyArray<{
  value: StartRung;
  label: string;
  hint: string;
  icon: LucideIcon;
}> = [
  {
    value: "connected_app",
    label: "A connected app",
    hint: "When something happens in a connected app like Stripe.",
    icon: Blocks,
  },
  {
    value: "webhook",
    label: "Any other app",
    hint: "Give any service a signed URL to call when its event fires.",
    icon: Cable,
  },
  {
    value: "schedule",
    label: "On a schedule",
    hint: "Run automatically on a repeating cadence — hourly, daily, weekly.",
    icon: Clock,
  },
];

// What the auto-registering shapes' third dropdown picks — the provider-side
// resource the backend hooks ("owner/repo" for GitHub, a project for Vercel,
// a connected Google account email for Google watch channels).
// The label heads the dropdown; the plural feeds its placeholder copy.
// Keyed by registration shape, with an optional "<shape>:<provider>" override:
// auto_meta is the one shape two providers share, and its resource is a Page
// for Meta Ads but an account for Instagram.
const AUTO_RESOURCE_NOUNS: Record<string, { label: string; plural: string }> = {
  auto_github: { label: "Repository", plural: "repositories" },
  auto_vercel: { label: "Project", plural: "projects" },
  auto_google_watch: { label: "Google account", plural: "accounts" },
  "auto_meta:meta_ads": { label: "Page", plural: "Pages" },
  "auto_meta:instagram": { label: "Instagram account", plural: "accounts" },
};

// POST /automations/from-integration's response (core/services/
// inbound_triggers.py::seed_trigger_routine). `guided` -> the owner must
// paste the proxy URL into the provider's own dashboard and then paste back
// the signing secret it hands them. `secret_hint` names the provider's term
// for that secret ("whsec_…" for Stripe, "Client Secret" for Sentry) and
// becomes the paste input's placeholder. `webhook_secret` -> the REVERSE-
// guided variant (PostHog): the secret flows the other way — the owner sets
// it as the provider-side Authorization header, shown once here with no
// paste-back step, so Done is never gated on it. `resource` echoes the
// auto-registered target ("owner/repo" for GitHub, the project id for
// Vercel) so the success copy can name it. `poll` -> a schedule-backed
// automation (gmail.poll): no webhook exists, so trigger_public_id/proxy_url
// are null. Non-guided results may still carry `instructions`
// (gmail.message's one-time Pub/Sub setup) — informational only, Done is
// never gated on them.
interface FromIntegrationResult {
  routine_id: string;
  trigger_public_id: string | null;
  proxy_url: string | null;
  guided: boolean;
  instructions?: string | null;
  secret_hint?: string | null;
  webhook_secret?: string | null;
  resource?: string | null;
  poll?: boolean;
}

// Searchable single-select for the event catalog. Providers now carry their
// FULL event lists (Stripe ~265), so the plain dropdown stopped scaling. With
// no filter and a big catalog: a "Popular" group (the first PINNED_EVENT_COUNT
// registry entries — providers.yaml orders the curated picks first) then the
// rest alphabetized. Typing collapses to one flat list matched
// case-insensitively against label AND id. Small catalogs (≤8) skip the
// grouping and list in registry order.
const PINNED_EVENT_COUNT = 3;

function EventPicker({
  events,
  value,
  onSelect,
}: {
  events: { id: string; label: string }[];
  value: string | null;
  onSelect: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  // Roving focus over the options (same idiom as the rung radiogroup): Arrow
  // keys move focus, Enter/Space activate the focused option button natively.
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const q = query.trim().toLowerCase();
  const filtered = q
    ? events.filter(
        (e) =>
          e.label.toLowerCase().includes(q) || e.id.toLowerCase().includes(q),
      )
    : null;
  const grouped = filtered === null && events.length > 8;
  const popular = grouped ? events.slice(0, PINNED_EVENT_COUNT) : [];
  const rest = grouped
    ? [...events.slice(PINNED_EVENT_COUNT)].sort((a, b) =>
        a.id.localeCompare(b.id),
      )
    : (filtered ?? events);
  const visible = [...popular, ...rest];
  const selectedIndex = visible.findIndex((e) => e.id === value);

  function onListKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const i = optionRefs.current.findIndex(
      (el) => el === document.activeElement,
    );
    const next =
      e.key === "ArrowDown"
        ? Math.min(i + 1, visible.length - 1)
        : Math.max(i - 1, 0);
    optionRefs.current[next]?.focus();
  }

  const groupHeading = (text: string) => (
    <div className="px-3 pt-2 pb-1 text-xs font-medium uppercase tracking-wide text-[var(--mute)]">
      {text}
    </div>
  );

  return (
    <div>
      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search events…"
        aria-label="Search events"
        className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 text-[var(--ink)]"
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            e.preventDefault();
            optionRefs.current[0]?.focus();
          }
        }}
      />
      <div
        role="listbox"
        aria-label="Event"
        onKeyDown={onListKeyDown}
        className="mt-1 max-h-[260px] overflow-y-auto rounded border border-[var(--edge)] bg-[var(--cream)] pb-1"
      >
        {visible.length === 0 && (
          <p className="px-3 py-2 text-sm text-[var(--mute)]">
            No events match &ldquo;{query.trim()}&rdquo;.
          </p>
        )}
        {visible.map((e, i) => (
          <div key={e.id}>
            {grouped && i === 0 && groupHeading("Popular")}
            {grouped && i === popular.length && groupHeading("All events")}
            <button
              ref={(el) => {
                optionRefs.current[i] = el;
              }}
              type="button"
              role="option"
              aria-selected={value === e.id}
              tabIndex={i === (selectedIndex === -1 ? 0 : selectedIndex) ? 0 : -1}
              onClick={() => onSelect(e.id)}
              className={cn(
                "flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-sm transition-colors",
                value === e.id
                  ? "bg-[var(--forest-tint)] text-[var(--ink)]"
                  : "text-[var(--ink-soft)] hover:bg-[var(--cream-deeper)]",
              )}
            >
              <span className="truncate">{e.label}</span>
              {value === e.id && (
                <Check
                  size={14}
                  strokeWidth={2}
                  className="shrink-0 text-[var(--forest)]"
                />
              )}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

function CreateRoutineDialog({
  agents,
  onClose,
  onCreated,
}: {
  agents: CompanyAgent[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const { post } = useTeamsApi();
  // The connected-app rung talks straight to the app API — automations
  // endpoints (routers/automations.py) are mounted under /api/v1, not the
  // Teams BFF that `post` above proxies through.
  const automationsApi = useApi();
  const { connections } = useConnections();
  const [rung, setRung] = useState<StartRung>("schedule");
  // Roving tabindex for the rung radiogroup (WAI-ARIA APG pattern): only the
  // checked radio is a tab stop; Arrow keys move both the selection and DOM
  // focus between rungs, wrapping at the ends.
  const rungRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [title, setTitle] = useState("");
  const [cron, setCron] = useState("0 9 * * *");
  const [signingMode, setSigningMode] = useState("bearer");
  const [replayWindowSec, setReplayWindowSec] = useState(
    String(DEFAULT_REPLAY_WINDOW_SEC),
  );
  const [agentId, setAgentId] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Set once the webhook branch's create call returns — the one-time URL +
  // secret then replace the form (mirrors RoutineDetailPanel's TriggerDialog
  // flow, which this rung reuses).
  const [createdWebhook, setCreatedWebhook] =
    useState<RoutineTriggerSecretMaterial | null>(null);

  // "A connected app" rung state — a provider + one of its events, submitted
  // straight to POST /automations/from-integration (no title/assignee/
  // description: the registry template supplies all of that server-side).
  const [connProvider, setConnProvider] = useState<string | null>(null);
  const [connEventId, setConnEventId] = useState<string | null>(null);
  const [connResult, setConnResult] = useState<FromIntegrationResult | null>(
    null,
  );
  const [whsecInput, setWhsecInput] = useState("");
  const [whsecSubmitting, setWhsecSubmitting] = useState(false);
  const [whsecSaved, setWhsecSaved] = useState(false);
  // Auto-register state — the provider-side resource (repo/project) to hook,
  // and the picker feed fetched lazily once such a provider is selected.
  // resourceOptions null = not loaded yet (also what re-arms the fetch effect).
  const [connResource, setConnResource] = useState<string | null>(null);
  const [resourceOptions, setResourceOptions] = useState<
    { id: string; label: string }[] | null
  >(null);
  const [resourceError, setResourceError] = useState<string | null>(null);
  // owner_setup state (gmail.message) — the owner's own Pub/Sub topic.
  const [connTopic, setConnTopic] = useState("");

  // Trigger-capable = connected AND the registry declares inbound events for
  // it (mirrors the Integrations card's `can_trigger` gate — Task 5).
  const triggerableConnections = connections.filter(
    (c) => c.status === "connected" && (c.inbound?.events.length ?? 0) > 0,
  );
  const selectedConnection =
    triggerableConnections.find((c) => c.provider === connProvider) ?? null;
  const connEvents = selectedConnection?.inbound?.events ?? [];
  const isAutoRegister =
    selectedConnection?.inbound?.webhook_registration?.startsWith("auto_") ??
    false;
  // The selected EVENT's registration override — "owner_setup" adds the
  // Pub/Sub topic input below (the account picker still comes from the
  // block-level auto_google_watch shape).
  const selectedEvent = connEvents.find((e) => e.id === connEventId) ?? null;
  const isOwnerSetup = selectedEvent?.registration === "owner_setup";
  const registrationShape =
    selectedConnection?.inbound?.webhook_registration ?? "";
  const resourceNoun = AUTO_RESOURCE_NOUNS[
    `${registrationShape}:${connProvider}`
  ] ??
    AUTO_RESOURCE_NOUNS[registrationShape] ?? {
      label: "Resource",
      plural: "resources",
    };

  useEffect(() => {
    if (
      !isAutoRegister ||
      !connProvider ||
      resourceOptions !== null ||
      resourceError !== null
    )
      return;
    let cancelled = false;
    (async () => {
      try {
        const resources = (await automationsApi.get(
          `/automations/providers/${connProvider}/resources`,
        )) as { id: string; label: string }[];
        if (!cancelled) setResourceOptions(resources);
      } catch (e) {
        if (!cancelled) setResourceError(gateMessage(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isAutoRegister, connProvider, resourceOptions, resourceError, automationsApi]);

  const showReplayWindow = REPLAY_WINDOW_SIGNING_MODES.has(signingMode);
  const replayWindowNum = Number(replayWindowSec);
  const replayWindowValid =
    !showReplayWindow ||
    (Number.isFinite(replayWindowNum) &&
      replayWindowNum >= 30 &&
      replayWindowNum <= 86400);
  const canSubmit =
    rung === "connected_app"
      ? connProvider != null &&
        connEventId != null &&
        (!isAutoRegister || connResource != null) &&
        (!isOwnerSetup || connTopic.trim() !== "")
      : title.trim() !== "" &&
        (rung === "schedule" ? cron.trim() !== "" : replayWindowValid);

  function moveRung(delta: 1 | -1) {
    const i = START_RUNGS.findIndex((r) => r.value === rung);
    const next = (i + delta + START_RUNGS.length) % START_RUNGS.length;
    setRung(START_RUNGS[next].value);
    rungRefs.current[next]?.focus();
  }

  function onRungKeyDown(e: React.KeyboardEvent<HTMLDivElement>) {
    if (e.key === "ArrowRight" || e.key === "ArrowDown") {
      e.preventDefault();
      moveRung(1);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
      e.preventDefault();
      moveRung(-1);
    }
  }

  // "A connected app" — one call seeds BOTH the routine and its trigger from
  // the registry template (core/services/inbound_triggers.py), so none of
  // the title/assignee/description fields the other two rungs use apply here.
  async function submitConnectedApp() {
    if (!connProvider || !connEventId) return;
    setSubmitting(true);
    setErr(null);
    try {
      const body = {
        provider: connProvider,
        event_id: connEventId,
        // Only the auto-register shapes carry a resource; guided providers'
        // bodies stay exactly as before.
        ...(connResource ? { resource: connResource } : {}),
        // Only the owner_setup shape (gmail.message) carries a topic.
        ...(isOwnerSetup && connTopic.trim() ? { topic: connTopic.trim() } : {}),
      };
      const result = (await automationsApi.post(
        "/automations/from-integration",
        body,
      )) as FromIntegrationResult;
      setConnResult(result);
    } catch (e) {
      if (notifyConversionGate(e) === null) {
        setErr(gateMessage(e));
      }
    } finally {
      setSubmitting(false);
    }
  }

  async function saveWebhookSecret() {
    if (!connResult || whsecInput.trim() === "") return;
    setWhsecSubmitting(true);
    setErr(null);
    try {
      await automationsApi.put(
        `/automations/${connResult.trigger_public_id}/webhook-secret`,
        { webhook_secret: whsecInput.trim() },
      );
      setWhsecSaved(true);
    } catch (e) {
      if (notifyConversionGate(e) === null) {
        setErr(gateMessage(e));
      }
    } finally {
      setWhsecSubmitting(false);
    }
  }

  async function submit() {
    if (!canSubmit) return;
    if (rung === "connected_app") {
      await submitConnectedApp();
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

  if (connResult) {
    const providerName =
      selectedConnection?.display_name ?? connProvider ?? "the app";
    // Prefer the picker's human label for the registered resource — the
    // backend echoes the id it hooked (a GitHub "owner/repo" reads fine, but
    // a Vercel "prj_…" id doesn't), and the picker already knows the name.
    const resourceLabel = connResult.resource
      ? (resourceOptions?.find((r) => r.id === connResult.resource)?.label ??
        connResult.resource)
      : null;
    // Guided (Stripe) results aren't armed until the owner pastes the whsec
    // back via saveWebhookSecret — closing before that leaves an active,
    // unsigned automation with no UI left to finish it (the detail panel
    // treats a "none"-signing trigger as non-rotatable). Auto (GitHub/
    // Vercel) results are already fully armed by the time this dialog shows.
    // Reverse-guided (webhook_secret present) results have no paste-back —
    // the trigger is born armed with its own secret and the owner wires the
    // provider side by hand — so Done is never gated for them either.
    const isReverseGuided = !!connResult.webhook_secret;
    const mustSaveSecretFirst =
      connResult.guided && !isReverseGuided && !whsecSaved;
    // Poll results (gmail.poll) are schedule-backed: nothing to wire, no URL.
    const isPoll = connResult.poll === true;
    // Non-guided results carrying instructions (gmail.message) still need a
    // one-time owner-side setup — informational only, Done is never gated.
    const needsOwnerSetup = !connResult.guided && !isPoll && !!connResult.instructions;
    return (
      <EditorialDialog
        open
        onOpenChange={(o) => !o && !mustSaveSecretFirst && onCreated()}
        title={
          connResult.guided || needsOwnerSetup
            ? "Finish setup"
            : "Automation registered"
        }
        description={
          connResult.guided
            ? isReverseGuided
              ? `Copy this URL and Authorization header value into ${providerName} — the secret is shown only once.`
              : `Paste this URL into ${providerName}'s webhook settings, then paste the signing secret it gives you back here.`
            : isPoll
              ? `Checks ${resourceLabel ?? "the account"}'s inbox every 10 minutes — this automation is live.`
              : needsOwnerSetup
                ? "One-time setup in your Google Cloud project points push notifications at this URL — follow the steps below."
                : `${providerName} registered the webhook${resourceLabel ? ` on ${resourceLabel}` : " automatically"} — this automation is live.`
        }
        footer={
          <EditorialButton
            variant="primary"
            size="sm"
            onClick={onCreated}
            disabled={mustSaveSecretFirst}
            title={
              mustSaveSecretFirst
                ? "Save the signing secret first"
                : undefined
            }
          >
            Done
          </EditorialButton>
        }
      >
        <div className="flex flex-col gap-3">
          {/* Poll results have no webhook — nothing to copy. */}
          {connResult.proxy_url && (
            <SecretField label="Webhook URL" value={connResult.proxy_url} />
          )}
          {/* Reverse-guided: the FULL header value ("Bearer <secret>"), not
              the bare secret — copying it whole eliminates the classic
              forgot-the-Bearer-prefix failure. */}
          {connResult.webhook_secret && (
            <SecretField
              label="Authorization header value"
              value={`Bearer ${connResult.webhook_secret}`}
            />
          )}
          {connResult.instructions && (
            <p className="text-xs text-[var(--mute)]">
              {connResult.instructions}
            </p>
          )}
          {connResult.guided && !isReverseGuided ? (
            <>
              <label className="block">
                <span className="text-sm text-[var(--ink-soft)]">
                  Paste your {providerName} signing secret
                </span>
                <input
                  type="password"
                  value={whsecInput}
                  onChange={(e) => setWhsecInput(e.target.value)}
                  placeholder={connResult.secret_hint ?? "Signing secret"}
                  className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 font-mono text-[var(--ink)]"
                />
              </label>
              {whsecSaved ? (
                <p className="text-sm" style={{ color: "var(--forest)" }}>
                  Secret saved.
                </p>
              ) : (
                <EditorialButton
                  variant="secondary"
                  size="sm"
                  disabled={whsecSubmitting || whsecInput.trim() === ""}
                  onClick={saveWebhookSecret}
                >
                  {whsecSubmitting ? "Saving…" : "Save secret"}
                </EditorialButton>
              )}
            </>
          ) : (
            // Reverse-guided is guided-but-unwired: "Registered." would claim
            // a liveness the owner's PostHog-side setup hasn't earned yet.
            !connResult.guided && (
              <p className="text-sm" style={{ color: "var(--forest)" }}>
                Registered.
              </p>
            )
          )}
          {err && (
            <div
              className="text-[var(--danger)] text-sm rounded px-3 py-2"
              style={{
                border:
                  "1px solid color-mix(in srgb, var(--danger) 20%, transparent)",
                background:
                  "color-mix(in srgb, var(--danger) 6%, transparent)",
              }}
            >
              {err}
            </div>
          )}
        </div>
      </EditorialDialog>
    );
  }

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New automation"
    >
      <div className="flex flex-col gap-4">
        <div className="block">
          <span className="text-xs font-medium uppercase tracking-wide text-[var(--mute)]">
            What starts this?
          </span>
          <div
            role="radiogroup"
            aria-label="What starts this automation?"
            className="mt-2 flex flex-col gap-1.5"
            onKeyDown={onRungKeyDown}
          >
            {START_RUNGS.map((r, i) => {
              const Icon = r.icon;
              const active = rung === r.value;
              return (
                <button
                  key={r.value}
                  ref={(el) => {
                    rungRefs.current[i] = el;
                  }}
                  type="button"
                  role="radio"
                  aria-checked={active}
                  tabIndex={active ? 0 : -1}
                  onClick={() => setRung(r.value)}
                  className={cn(
                    "flex items-start gap-3 rounded-[var(--radius)] border px-3 py-2.5 text-left transition-colors",
                    active
                      ? "border-[var(--forest)] bg-[var(--forest-tint)]"
                      : "border-[var(--edge)] hover:bg-[var(--cream-deeper)]",
                  )}
                >
                  <span
                    className="mt-px flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
                    style={{
                      background: active
                        ? "color-mix(in srgb, var(--forest) 12%, transparent)"
                        : "var(--cream-deeper)",
                      color: active ? "var(--forest)" : "var(--mute)",
                    }}
                  >
                    <Icon size={15} strokeWidth={1.75} />
                  </span>
                  <span className="min-w-0 flex-1">
                    <span
                      className={cn(
                        "block text-sm font-medium",
                        active ? "text-[var(--ink)]" : "text-[var(--ink-soft)]",
                      )}
                    >
                      {r.label}
                    </span>
                    <span className="block text-xs leading-snug text-[var(--mute)]">
                      {r.hint}
                    </span>
                  </span>
                  {active && (
                    <Check
                      size={16}
                      strokeWidth={2}
                      className="mt-px shrink-0 text-[var(--forest)]"
                    />
                  )}
                </button>
              );
            })}
          </div>
        </div>
        {/* Title/Assignee only apply to the schedule/webhook rungs — the
            connected-app rung's registry template supplies both server-side
            (POST /automations/from-integration). */}
        {rung !== "connected_app" && (
          <>
            <label className="block">
              <span className="text-sm text-[var(--ink-soft)]">Title</span>
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
              />
            </label>
            {/* Agent picker replaces the old raw "Agent ID" text input — the
                same editorial AssigneePicker the issues surface uses.
                Assignee is optional upstream (a routine can be created
                unassigned). */}
            <div className="block">
              <span className="text-sm text-[var(--ink-soft)]">Assignee</span>
              <div className="mt-1">
                <AssigneePicker
                  agents={agents}
                  selectedAgentId={agentId}
                  onChange={setAgentId}
                  variant="inline"
                  className="border border-[var(--edge)] rounded px-3 py-2 bg-[var(--cream)]"
                />
              </div>
            </div>
          </>
        )}

        {rung === "connected_app" && (
          <>
            <label className="block">
              <span className="text-sm text-[var(--ink-soft)]">
                Connected app
              </span>
              <div className="mt-1">
                <EditorialSelect
                  options={triggerableConnections.map((c) => ({
                    value: c.provider,
                    label: c.display_name,
                  }))}
                  value={connProvider ?? undefined}
                  onValueChange={(next) => {
                    setConnProvider(next);
                    setConnEventId(null);
                    setConnResource(null);
                    setConnTopic("");
                    // Resource feeds are per-provider — clearing re-arms the
                    // lazy fetch for the newly selected one.
                    setResourceOptions(null);
                    setResourceError(null);
                  }}
                  placeholder={
                    triggerableConnections.length === 0
                      ? "No connected apps can trigger automations yet"
                      : "Select an app…"
                  }
                  disabled={triggerableConnections.length === 0}
                />
              </div>
            </label>
            {selectedConnection && (
              <div className="block">
                <span className="text-sm text-[var(--ink-soft)]">Event</span>
                <div className="mt-1">
                  {/* Keyed by provider so switching apps resets the filter. */}
                  <EventPicker
                    key={connProvider}
                    events={connEvents}
                    value={connEventId}
                    onSelect={setConnEventId}
                  />
                </div>
              </div>
            )}
            {/* The auto-register shapes' third step — which repo/project to
                hook. The backend registers the webhook there itself, so
                unlike guided providers there is no paste step after Create. */}
            {isAutoRegister && connEventId != null && (
              <div className="flex flex-col gap-1.5">
              <label className="block">
                <span className="text-sm text-[var(--ink-soft)]">
                  {resourceNoun.label}
                </span>
                <div className="mt-1">
                  <EditorialSelect
                    options={(resourceOptions ?? []).map((r) => ({
                      value: r.id,
                      label: r.label,
                    }))}
                    value={connResource ?? undefined}
                    onValueChange={setConnResource}
                    placeholder={
                      resourceError
                        ? `Couldn't load ${resourceNoun.plural}`
                        : resourceOptions === null
                          ? `Loading ${resourceNoun.plural}…`
                          : resourceOptions.length === 0
                            ? `No ${resourceNoun.plural} found`
                            : `Select a ${resourceNoun.label.toLowerCase()}…`
                    }
                    disabled={
                      resourceOptions === null || resourceOptions.length === 0
                    }
                  />
                </div>
                {resourceError && (
                  <span className="text-xs text-[var(--danger)]">
                    {resourceError}
                  </span>
                )}
              </label>
                {/* Google's "resource" is an identity, not a repo — which
                    agent reads whose mailbox. That deserves a stated
                    consequence and a reconciliation with the per-agent
                    assignment, neither of which a bare dropdown carries. A
                    sibling of the <label>, not a child: it holds its own
                    buttons, which a label would hijack. */}
                {connProvider === "google" && (
                  <AutomationGoogleAccount
                    agents={agents}
                    account={connResource}
                    accountOptions={resourceOptions}
                    onSelectAccount={setConnResource}
                  />
                )}
              </div>
            )}
            {/* owner_setup (gmail.message) — the owner's own Pub/Sub topic.
                The one-time topic/subscription wiring instructions come back
                in the finish dialog after Create. */}
            {isOwnerSetup && (
              <label className="block">
                <span className="text-sm text-[var(--ink-soft)]">
                  Pub/Sub topic
                </span>
                <input
                  value={connTopic}
                  onChange={(e) => setConnTopic(e.target.value)}
                  placeholder="projects/my-project/topics/isol8-gmail"
                  className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 font-mono text-[var(--ink)]"
                />
                <span className="text-xs text-[var(--mute)]">
                  A topic in your own Google Cloud project — you&apos;ll wire
                  it up after Create.
                </span>
              </label>
            )}
          </>
        )}

        {rung === "schedule" && (
          <label className="block">
            <span className="text-sm text-[var(--ink-soft)]">Schedule</span>
            <input
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              placeholder="0 9 * * *"
              className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 font-mono text-[var(--ink)]"
            />
            <span className="text-xs text-[var(--mute)]">
              Cron expression — when the automation fires.
            </span>
          </label>
        )}

        {rung === "webhook" && (
          <>
            <label className="block">
              <span className="text-sm text-[var(--ink-soft)]">
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
                  className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
                />
              </label>
            )}
          </>
        )}

        {rung !== "connected_app" && (
          <label className="block">
            <span className="text-sm text-[var(--ink-soft)]">
              Description
            </span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
              rows={3}
            />
          </label>
        )}
        {err && (
          <div
            className="text-[var(--danger)] text-sm rounded px-3 py-2"
            style={{
              border:
                "1px solid color-mix(in srgb, var(--danger) 20%, transparent)",
              background:
                "color-mix(in srgb, var(--danger) 6%, transparent)",
            }}
          >
            {err}
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
