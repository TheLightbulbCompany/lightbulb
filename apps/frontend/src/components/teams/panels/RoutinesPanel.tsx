"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import { notifyConversionGate } from "@/lib/notify";
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
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { AssigneePicker } from "@/components/teams/issues/pickers/AssigneePicker";
import { useInlineTeamsNav } from "@/components/chat/InlineTeamsNav";
import { useTeamsApi } from "@/hooks/useTeamsApi";
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
// the first enabled trigger (or any trigger) and describes it via
// triggerSentence. A legacy flat-cron row (no triggers[] array) synthesizes a
// schedule trigger so it still reads as a sentence, not a raw cron string.
function routineTriggerSentence(r: Routine): string {
  const triggers = r.triggers ?? [];
  const picked = triggers.find((t) => t.enabled !== false) ?? triggers[0];
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

/* ── create dialog: "what should start this?" five-rung picker ──────────── */

// The five ways a routine can be kicked off (spec Phase 1d). Only "schedule"
// and "webhook" are wired to a real trigger today — the rest are visible,
// selectable, and explain themselves as not-yet-built rather than being
// hidden (requestable-not-hidden; delivered in Phase 2/4).
type StartRung = "connected_app" | "webhook" | "email" | "schedule" | "agent_watch";

const START_RUNGS: ReadonlyArray<{
  value: StartRung;
  label: string;
  comingSoon?: boolean;
}> = [
  { value: "connected_app", label: "A connected app", comingSoon: true },
  { value: "webhook", label: "Any other app" },
  { value: "email", label: "An email arrives", comingSoon: true },
  { value: "schedule", label: "On a schedule" },
  { value: "agent_watch", label: "My agent watches it", comingSoon: true },
];

const COMING_SOON_COPY: Partial<Record<StartRung, string>> = {
  connected_app:
    "Coming soon — for now use “Any other app” to trigger this automation.",
  email: "Coming soon — for now forward the email to your agent.",
  agent_watch:
    "Coming soon — for now use “Any other app” to trigger this automation.",
};

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

  const comingSoon = COMING_SOON_COPY[rung] != null;
  const showReplayWindow = REPLAY_WINDOW_SIGNING_MODES.has(signingMode);
  const replayWindowNum = Number(replayWindowSec);
  const replayWindowValid =
    !showReplayWindow ||
    (Number.isFinite(replayWindowNum) &&
      replayWindowNum >= 30 &&
      replayWindowNum <= 86400);
  const canSubmit =
    title.trim() !== "" &&
    !comingSoon &&
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

  async function submit() {
    if (!canSubmit) return;
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

  return (
    <EditorialDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New automation"
    >
      <div className="flex flex-col gap-3">
        <div className="block">
          <span className="text-sm text-[var(--ink-soft)]">
            What should start this?
          </span>
          <div
            role="radiogroup"
            aria-label="What should start this?"
            className="mt-1 grid grid-cols-2 gap-1.5 sm:grid-cols-5"
            onKeyDown={onRungKeyDown}
          >
            {START_RUNGS.map((r, i) => (
              <button
                key={r.value}
                ref={(el) => {
                  rungRefs.current[i] = el;
                }}
                type="button"
                role="radio"
                aria-checked={rung === r.value}
                tabIndex={rung === r.value ? 0 : -1}
                onClick={() => setRung(r.value)}
                className={cn(
                  "rounded border px-2 py-2 text-left text-xs transition-colors",
                  rung === r.value
                    ? "border-[var(--forest)] text-[var(--ink)]"
                    : "border-[var(--edge)] text-[var(--ink-soft)] hover:bg-[var(--cream-deeper)]",
                )}
                style={
                  rung === r.value
                    ? {
                        background:
                          "color-mix(in srgb, var(--forest) 8%, transparent)",
                      }
                    : undefined
                }
              >
                {r.label}
                {r.comingSoon && (
                  <span
                    className="ml-1 text-[10px] uppercase tracking-wide"
                    style={{ color: "var(--mute)" }}
                  >
                    soon
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Title</span>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
          />
        </label>
        {/* Agent picker replaces the old raw "Agent ID" text input — the same
            editorial AssigneePicker the issues surface uses. Assignee is
            optional upstream (a routine can be created unassigned). */}
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
                How Isol8 signs the callback — the caller verifies with the
                secret shown once this automation is created.
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

        {comingSoon && (
          <div
            role="note"
            className="rounded border px-3 py-2 text-sm"
            style={{
              borderColor: "var(--edge)",
              background: "var(--cream-deeper)",
              color: "var(--ink-soft)",
            }}
          >
            {COMING_SOON_COPY[rung]}
          </div>
        )}

        <label className="block">
          <span className="text-sm text-[var(--ink-soft)]">Description</span>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            className="w-full border border-[var(--edge)] bg-[var(--cream)] rounded px-3 py-2 mt-1 text-[var(--ink)]"
            rows={3}
          />
        </label>
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
