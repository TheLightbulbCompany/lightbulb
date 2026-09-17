"use client";

import { useState } from "react";
import { Loader2, RefreshCw, Moon, Sun, Sparkles } from "lucide-react";
import { useHiddenOnAgentCore } from "@lightbulb/shared";
import { useRuntimeRpc, useRuntimeRpcMutation } from "@/hooks/useRuntimeRpc";
import { useConfigPatch } from "@/hooks/useConfigPatch";
import { type Agent, agentDisplayName, useAgents } from "@/hooks/useAgents";
import { Button } from "@/components/ui/button";
import {
  type MemoryStatusResponse,
  type DreamDiaryResponse,
  type DreamActionResponse,
  type DreamPhaseKey,
} from "./dreams-types";

type SubTab = "scene" | "diary" | "advanced";

const PHASE_ORDER: DreamPhaseKey[] = ["light", "rem", "deep"];
const PHASE_LABEL: Record<DreamPhaseKey, string> = { light: "Light", rem: "REM", deep: "Deep" };

export function DreamsPanel() {
  const [tab, setTab] = useState<SubTab>("scene");
  const [agentId, setAgentId] = useState<string | undefined>(undefined);

  // M6 task 10-D, row 18: Dreams (memory-core's nightly sweep/diary/phases) has no
  // Hermes equivalent — `hermes claw migrate` archives OpenClaw `memory.*` for
  // manual review rather than translating it. The panel renders a notice instead
  // of the editor on a confirmed AgentCore workspace, and its RPCs (including the
  // toggle that used to fan a dead key out to every roster profile) never fire.
  const visibility = useHiddenOnAgentCore();
  const rpcReady = visibility === "shown";

  // Agent selector. `{}` → default agent server-side; an explicit pick scopes
  // every doctor.memory.* call to that agent's workspace. `agents.list`
  // returns `{ defaultId?, agents? }`, not a bare array.
  const { agents: roster } = useAgents();
  const agents = roster as Agent[];
  const params = agentId ? { agentId } : {};

  const { data, error, isLoading, mutate } = useRuntimeRpc<MemoryStatusResponse>(
    rpcReady ? "doctor.memory.status" : null,
    params,
  );

  if (visibility === "hidden") {
    return (
      <div className="p-6">
        <p className="text-sm text-[var(--midnight-muted)]">
          Dreams isn&apos;t available on this runtime.
        </p>
      </div>
    );
  }

  if (!rpcReady || (isLoading && !data)) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 size={20} className="animate-spin text-[var(--midnight-muted)]" />
      </div>
    );
  }
  if (error && !data) {
    return (
      <div className="p-6 space-y-3">
        <p className="text-sm text-destructive">Failed to load dreams: {error.message}</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw size={16} className="mr-1.5" /> Retry
        </Button>
      </div>
    );
  }

  const dreaming = data?.dreaming;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Moon size={20} className="text-[var(--midnight-muted)]" />
          <h2 className="font-editorial-serif text-title text-[var(--midnight)]">Dreams</h2>
        </div>
        {agents.length > 1 && (
          <select
            className="text-sm border rounded-md px-2 py-1 bg-cream-deep"
            value={agentId ?? ""}
            onChange={(e) => setAgentId(e.target.value || undefined)}
          >
            <option value="">Default agent</option>
            {agents.map((a) => (
              <option key={a.id} value={a.id}>{agentDisplayName(a)}</option>
            ))}
          </select>
        )}
      </div>

      <div className="flex gap-1 border-b">
        {(["scene", "diary", "advanced"] as SubTab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-3 py-1.5 text-sm capitalize border-b-2 -mb-px ${
              tab === t ? "border-[var(--midnight)] text-[var(--midnight)]" : "border-transparent text-[var(--midnight-muted)]"
            }`}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "scene" && <SceneTab status={data} dreaming={dreaming} onChanged={() => mutate()} />}
      {tab === "diary" && <DiaryTab params={params} />}
      {tab === "advanced" && <AdvancedTab params={params} onAfterAction={() => mutate()} />}
    </div>
  );
}

function SceneTab({
  status,
  dreaming,
  onChanged,
}: {
  status?: MemoryStatusResponse;
  dreaming?: MemoryStatusResponse["dreaming"];
  onChanged: () => void;
}) {
  if (!dreaming) {
    return (
      <p className="text-sm text-[var(--midnight-muted)]">
        No dreaming status for this agent yet. Embedding store:{" "}
        {status?.embedding?.ok ? "ok" : status?.embedding?.error || "unavailable"}.
      </p>
    );
  }
  return (
    <div className="space-y-5">
      <div className="flex items-center gap-2 text-sm">
        <Sparkles size={16} className="text-[var(--midnight-muted)]" />
        <span className="font-medium">Dreaming</span>
        <span className={dreaming.enabled ? "text-ok" : "text-[var(--midnight-muted)]"}>
          {dreaming.enabled ? "active" : "idle"}
        </span>
        {dreaming.timezone && <span className="text-[var(--midnight-muted)]">· {dreaming.timezone}</span>}
      </div>

      <div className="grid grid-cols-3 gap-4">
        <Stat label="Promoted today" value={dreaming.promotedToday ?? 0} />
        <Stat label="Promoted total" value={dreaming.promotedTotal ?? 0} />
        <Stat label="Signals" value={dreaming.totalSignalCount ?? 0} />
      </div>

      <PhaseToggles dreaming={dreaming} onChanged={onChanged} />
    </div>
  );
}

function PhaseToggles({
  dreaming,
  onChanged,
}: {
  dreaming: MemoryStatusResponse["dreaming"];
  onChanged: () => void;
}) {
  const { configReady, patchConfig } = useConfigPatch();
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const on = dreaming?.enabled ?? false;

  // ONE switch, `dreaming.enabled`. The phases are not independently
  // schedulable on the 9.x image: memory-core runs a single managed sweep
  // (light → REM → deep) and builds its cron job from the DEEP config, so the
  // old per-phase "Deep: Off" button silently removed the whole sweep — no
  // cron job, no light notes, no diary — for every workspace. Phases render
  // below as read-only indicators of what the sweep reports.
  const toggle = async () => {
    setBusy(true);
    setMsg(null);
    try {
      await patchConfig({
        plugins: { entries: { "memory-core": { config: { dreaming: { enabled: !on } } } } },
      });
      onChanged();
    } catch (e) {
      setMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between rounded-lg border bg-cream px-3 py-2">
        <div className="flex items-center gap-2 text-sm">
          <span className={`h-2 w-2 rounded-full ${on ? "bg-ok" : "bg-[var(--edge)]"}`} />
          <Moon size={16} className="text-[var(--midnight-muted)]" />
          <span>Nightly sweep</span>
        </div>
        <Button
          variant="outline"
          size="sm"
          aria-label="Toggle dreaming"
          disabled={!configReady || busy}
          onClick={toggle}
        >
          {busy ? "…" : on ? "On" : "Off"}
        </Button>
      </div>
      <p className="label-caps text-[var(--midnight-muted)] mt-4 mb-2">Sleep phases</p>
      <div className="flex flex-wrap gap-4 text-sm">
        {PHASE_ORDER.map((key) => {
          const phaseOn = dreaming?.phases?.[key]?.enabled ?? on;
          return (
            <span key={key} className="flex items-center gap-1.5">
              <span className={`h-2 w-2 rounded-full ${phaseOn ? "bg-ok" : "bg-[var(--edge)]"}`} />
              {key === "light" ? <Sun size={16} className="text-[var(--midnight-muted)]" /> : <Moon size={16} className="text-[var(--midnight-muted)]" />}
              <span>{PHASE_LABEL[key]}</span>
            </span>
          );
        })}
      </div>
      {msg && <p className="text-sm text-destructive mt-2">{msg}</p>}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border bg-cream p-3">
      <p className="text-title tabular-nums text-[var(--midnight)]">{value}</p>
      <p className="text-xs text-[var(--midnight-muted)]">{label}</p>
    </div>
  );
}

function DiaryTab({ params }: { params: Record<string, unknown> }) {
  const { data, error, isLoading, mutate } = useRuntimeRpc<DreamDiaryResponse>(
    "doctor.memory.dreamDiary",
    params,
  );
  if (isLoading && !data) return <Loader2 size={16} className="animate-spin text-[var(--midnight-muted)]" />;
  if (error && !data) return <p className="text-sm text-destructive">{error.message}</p>;
  if (!data?.found || !data.content) {
    return (
      <div className="space-y-2">
        <p className="text-sm text-[var(--midnight-muted)]">No DREAMS.md yet — REM/deep phases populate it nightly.</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw size={16} className="mr-1.5" /> Reload
        </Button>
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <p className="text-xs text-[var(--midnight-muted)]">{data.path}</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw size={16} className="mr-1.5" /> Reload
        </Button>
      </div>
      <pre className="whitespace-pre-wrap text-sm bg-cream border rounded-lg p-4 max-h-[60vh] overflow-auto">
        {data.content}
      </pre>
    </div>
  );
}

const ADVANCED_ACTIONS: { method: string; label: string; confirm?: boolean }[] = [
  { method: "doctor.memory.backfillDreamDiary", label: "Backfill diary" },
  { method: "doctor.memory.dedupeDreamDiary", label: "Dedupe diary", confirm: true },
  { method: "doctor.memory.repairDreamingArtifacts", label: "Repair cache", confirm: true },
  { method: "doctor.memory.resetDreamDiary", label: "Reset diary", confirm: true },
  { method: "doctor.memory.resetGroundedShortTerm", label: "Clear grounded short-term", confirm: true },
];

function AdvancedTab({
  params,
  onAfterAction,
}: {
  params: Record<string, unknown>;
  onAfterAction: () => void;
}) {
  const callRpc = useRuntimeRpcMutation();
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const run = async (method: string, confirm?: boolean) => {
    if (confirm && !window.confirm(`Run ${method}? This rewrites dream artifacts.`)) return;
    setBusy(method);
    setMsg(null);
    try {
      const res = await callRpc<DreamActionResponse>(method, params);
      const detail = [
        res.written != null ? `${res.written} written` : null,
        res.dedupedEntries != null ? `${res.dedupedEntries} deduped` : null,
        res.removedEntries != null ? `${res.removedEntries} removed` : null,
        res.removedShortTermEntries != null ? `${res.removedShortTermEntries} cleared` : null,
      ].filter(Boolean).join(", ");
      setMsg(`${method.split(".").pop()}: ${detail || "done"}`);
      onAfterAction();
    } catch (e) {
      setMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        {ADVANCED_ACTIONS.map((a) => (
          <Button
            key={a.method}
            variant="outline"
            size="sm"
            disabled={busy !== null}
            onClick={() => run(a.method, a.confirm)}
          >
            {busy === a.method ? "Working…" : a.label}
          </Button>
        ))}
      </div>
      {msg && <p className="text-sm text-[var(--midnight-muted)]">{msg}</p>}
    </div>
  );
}
