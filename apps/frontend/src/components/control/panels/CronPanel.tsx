"use client";

import { useState, useCallback } from "react";
import {
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";

import { capture } from "@/lib/analytics";
import { notifyApiError } from "@/lib/notify";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { useAgents } from "@/hooks/useAgents";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { JobList } from "./cron/JobList";
import { RunList } from "./cron/RunList";
import { RunDetailPanel } from "./cron/RunDetailPanel";
import {
  JobEditDialog,
  EMPTY_FORM,
  buildSchedule,
  buildFailureAlertPayload,
  jobToForm,
  type FormState,
} from "./cron/JobEditDialog";
import { buildEditPayloadDiff } from "./cron/formState";
import type { RunStatusFilter } from "./cron/RunFilters";
import type {
  CronJob,
  CronListResponse,
  CronRunEntry,
  CronRunsResponse,
} from "./cron/types";

// --- View state ---

type ViewState =
  | { kind: "overview" }
  | { kind: "runs"; jobId: string; selectedRunTs: number | null };

// --- State B shell (runs list + placeholder detail) ---

function StateBShell({
  jobId,
  selectedRunTs,
  onSelectRun,
  onBack,
  jobName,
  job,
  onCloseRun,
  onRunNow,
  onEdit,
}: {
  jobId: string;
  selectedRunTs: number | null;
  onSelectRun: (run: CronRunEntry) => void;
  onBack: () => void;
  jobName: string;
  job: CronJob | undefined;
  onCloseRun: () => void;
  onRunNow: () => void;
  onEdit: () => void;
}) {
  const [statusFilter, setStatusFilter] = useState<RunStatusFilter>("all");
  const [queryFilter, setQueryFilter] = useState("");
  const [limit, setLimit] = useState(50);

  const { data, error, isLoading, mutate } = useGatewayRpc<CronRunsResponse>(
    "cron.runs",
    {
      scope: "job",
      id: jobId,
      limit,
      sortDir: "desc",
      ...(statusFilter !== "all" ? { statuses: [statusFilter] } : {}),
      ...(queryFilter ? { query: queryFilter } : {}),
    },
    { refreshInterval: 30_000, revalidateOnFocus: true },
  );
  const runs = data?.entries ?? [];
  const hasMore = data?.hasMore ?? false;
  const selectedRun =
    selectedRunTs !== null
      ? runs.find((r) => r.triggeredAtMs === selectedRunTs)
      : undefined;
  const jobDeleted = !job;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-3 px-4 h-12 border-b border-[#e0dbd0]">
        <Button size="sm" variant="ghost" onClick={onBack}>
          ← Back to jobs
        </Button>
        <span className="text-sm font-medium">{jobName}</span>
        {jobDeleted && (
          <span className="px-2 py-0.5 rounded text-xs uppercase bg-[#f3efe6] text-[#8a8578]">
            (deleted)
          </span>
        )}
      </div>
      <div className="flex-1 grid grid-cols-[320px_1fr] min-h-0">
        <div className="border-r border-[#e0dbd0] min-h-0 flex flex-col">
          {error && (
            <div
              role="alert"
              className="m-3 flex items-center justify-between gap-2 rounded-md bg-destructive/10 border border-destructive/20 px-3 py-2 text-xs text-destructive"
            >
              <span>Failed to load runs</span>
              <Button size="sm" variant="outline" onClick={() => mutate()}>
                <RefreshCw className="h-3 w-3 mr-1" /> Retry
              </Button>
            </div>
          )}
          <RunList
            runs={runs}
            selectedTs={selectedRunTs}
            onSelect={onSelectRun}
            statusFilter={statusFilter}
            queryFilter={queryFilter}
            onStatusFilterChange={(s) => {
              setStatusFilter(s);
              setLimit(50);
            }}
            onQueryFilterChange={(q) => {
              setQueryFilter(q);
              setLimit(50);
            }}
            hasMore={hasMore}
            onLoadMore={() => setLimit((n) => n + 50)}
            isLoading={isLoading}
          />
        </div>
        {selectedRun ? (
          <RunDetailPanel
            run={selectedRun}
            job={job}
            onClose={onCloseRun}
            onRunNow={onRunNow}
            onEdit={onEdit}
          />
        ) : (
          <div className="flex items-center justify-center h-full text-[#8a8578]">
            Select a run to vet
          </div>
        )}
      </div>
    </div>
  );
}

// --- Main panel ---

export function CronPanel() {
  const { data, error, isLoading, mutate } = useGatewayRpc<CronListResponse>(
    "cron.list",
    { includeDisabled: true },
    { refreshInterval: 30_000, revalidateOnFocus: true },
  );
  const { agents: agentsData } = useAgents();
  const callRpc = useGatewayRpcMutation();

  const [mode, setMode] = useState<"list" | "create" | "edit">("list");
  const [editingJob, setEditingJob] = useState<CronJob | null>(null);
  const [expandedJob, setExpandedJob] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<{ type: "success" | "error"; message: string } | null>(null);
  const [view, setView] = useState<ViewState>({ kind: "overview" });
  // Optimistic overrides for the enabled flag keyed by job id. When set, the
  // JobCard reads from here instead of the server data; the entry is cleared
  // on successful revalidation (or on error to roll back).
  const [enabledOverrides, setEnabledOverrides] = useState<Record<string, boolean>>({});

  const noAgents = (agentsData ?? []).length === 0;

  const showFeedback = useCallback((type: "success" | "error", message: string) => {
    setFeedback({ type, message });
    setTimeout(() => setFeedback(null), 3000);
  }, []);

  const handleCreate = async (form: FormState) => {
    setSaving(true);
    try {
      const cleanFallbacks = form.fallbacks?.map((s) => s.trim()).filter(Boolean) ?? [];
      await callRpc("cron.add", {
        name: form.name.trim(),
        schedule: buildSchedule(form),
        payload: {
          kind: "agentTurn",
          message: form.message.trim(),
          ...(form.model ? { model: form.model } : {}),
          ...(cleanFallbacks.length > 0 ? { fallbacks: cleanFallbacks } : {}),
          ...(form.timeoutSeconds != null ? { timeoutSeconds: form.timeoutSeconds } : {}),
          ...(form.thinking ? { thinking: form.thinking } : {}),
          ...(form.lightContext ? { lightContext: true } : {}),
          ...(form.toolsAllow && form.toolsAllow.length > 0 ? { toolsAllow: form.toolsAllow } : {}),
        },
        enabled: form.enabled,
        sessionTarget: "isolated",
        wakeMode: form.wakeMode,
        ...(form.delivery ? { delivery: form.delivery } : {}),
        ...(form.agentId ? { agentId: form.agentId } : {}),
        ...(form.deleteAfterRun ? { deleteAfterRun: true } : {}),
        failureAlert: buildFailureAlertPayload(form),
      });
      capture("cron_job_created", { schedule: buildSchedule(form) });
      setMode("list");
      mutate();
      showFeedback("success", `Created "${form.name.trim()}"`);
    } catch (err) {
      notifyApiError(err, "Couldn't create the cron job");
    } finally {
      setSaving(false);
    }
  };

  const handleEdit = async (form: FormState) => {
    if (!editingJob) return;
    setSaving(true);
    try {
      // buildEditPayloadDiff diffs the form against the original job so that
      // optional overrides the user cleared (model, fallbacks, timeoutSeconds,
      // thinking, lightContext, toolsAllow, delivery, agentId, deleteAfterRun)
      // are sent as explicit clearing values instead of being silently
      // omitted.
      await callRpc("cron.update", {
        id: editingJob.id,
        patch: buildEditPayloadDiff(form, editingJob),
      });
      setMode("list");
      setEditingJob(null);
      mutate();
      showFeedback("success", `Updated "${form.name.trim()}"`);
    } catch (err) {
      notifyApiError(err, "Couldn't update the cron job");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await callRpc("cron.remove", { id });
      capture("cron_job_deleted");
      mutate();
      showFeedback("success", "Job deleted");
    } catch (err) {
      notifyApiError(err, "Couldn't delete the cron job");
    }
  };

  const handleToggle = async (id: string, currentlyEnabled: boolean) => {
    const next = !currentlyEnabled;
    // Optimistic flip: update local override immediately so the badge toggles
    // without waiting for the RPC round-trip.
    setEnabledOverrides((prev) => ({ ...prev, [id]: next }));
    try {
      await callRpc("cron.update", { id, patch: { enabled: next } });
      capture("cron_job_toggled", { enabled: next });
      mutate();
      // Clear the override; the revalidated server data becomes the source of truth.
      setEnabledOverrides((prev) => {
        if (!(id in prev)) return prev;
        const rest = { ...prev };
        delete rest[id];
        return rest;
      });
    } catch (err) {
      // Roll back: drop the override so the UI reverts to the server state.
      setEnabledOverrides((prev) => {
        if (!(id in prev)) return prev;
        const rest = { ...prev };
        delete rest[id];
        return rest;
      });
      notifyApiError(err, "Couldn't toggle the cron job");
    }
  };

  const handleRun = async (id: string) => {
    try {
      await callRpc("cron.run", { id, mode: "force" });
      capture("cron_job_triggered");
      showFeedback("success", "Job triggered");
      mutate();
    } catch (err) {
      notifyApiError(err, "Couldn't run the cron job");
    }
  };

  // Loading state
  if (isLoading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-[#8a8578]" />
      </div>
    );
  }

  // Error state -- only take over the panel when we have no data yet. When
  // a revalidation fails after a successful fetch, we keep rendering the
  // stale jobs and surface the failure as an inline banner below.
  if (error && !data) {
    return (
      <div className="p-6 space-y-3">
        <p className="text-sm text-destructive">{error.message}</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw className="h-3.5 w-3.5 mr-1.5" /> Retry
        </Button>
      </div>
    );
  }

  const rawJobs =
    data?.jobs ?? (Array.isArray(data) ? (data as unknown as CronJob[]) : []);
  // Apply optimistic enabled overrides before handing jobs off to JobList /
  // StateBShell so the toggle UI flips immediately.
  const jobs = rawJobs.map((j) =>
    Object.prototype.hasOwnProperty.call(enabledOverrides, j.id)
      ? { ...j, enabled: enabledOverrides[j.id] }
      : j,
  );

  return (
    <div className="p-6 space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Cron Jobs</h2>
        <div className="flex gap-1">
          <Button variant="ghost" size="sm" onClick={() => mutate()}>
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {/* Stale-data error banner (revalidation failed after a successful load) */}
      {error && data && (
        <div
          role="alert"
          className="flex items-center justify-between gap-2 rounded-md bg-destructive/10 border border-destructive/20 px-3 py-2 text-sm text-destructive"
        >
          <span>Failed to refresh cron jobs: {error.message}</span>
          <Button size="sm" variant="outline" onClick={() => mutate()}>
            <RefreshCw className="h-3 w-3 mr-1" /> Retry
          </Button>
        </div>
      )}

      {/* Feedback banner */}
      {feedback && (
        <div
          className={cn(
            "flex items-center justify-between rounded-md px-3 py-2 text-sm",
            feedback.type === "success"
              ? "bg-[#e8f5e9] text-[#2d8a4e] border border-[#2d8a4e]/20"
              : "bg-destructive/10 text-destructive border border-destructive/20",
          )}
        >
          <span>{feedback.message}</span>
          <button onClick={() => setFeedback(null)} className="opacity-60 hover:opacity-100">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      )}

      {/* Create form */}
      {mode === "create" && (
        <JobEditDialog
          initial={EMPTY_FORM}
          onSave={handleCreate}
          onCancel={() => setMode("list")}
          saving={saving}
        />
      )}

      {/* Edit form */}
      {mode === "edit" && editingJob && (
        <JobEditDialog
          initial={jobToForm(editingJob)}
          onSave={handleEdit}
          onCancel={() => {
            setMode("list");
            setEditingJob(null);
          }}
          saving={saving}
        />
      )}

      {/* Job list or runs drill-in */}
      {mode === "list" && (view.kind === "overview" ? (
        <JobList
          jobs={jobs}
          expandedJobId={expandedJob}
          onToggleExpand={(jobId) =>
            setExpandedJob(expandedJob === jobId ? null : jobId)
          }
          onCreate={() => setMode("create")}
          onEdit={(job) => {
            setEditingJob(job);
            setMode("edit");
          }}
          onPauseResume={(job) => handleToggle(job.id, !!job.enabled)}
          onRunNow={(job) => handleRun(job.id)}
          onDelete={(job) => handleDelete(job.id)}
          onSelectRun={(job, run) => {
            setMode("list");
            setView({ kind: "runs", jobId: job.id, selectedRunTs: run.triggeredAtMs });
          }}
          createDisabled={noAgents}
          createHelperText={noAgents ? "Create an agent first" : undefined}
        />
      ) : (
        (() => {
          const selectedJob = jobs.find((j) => j.id === view.jobId);
          return (
            <StateBShell
              jobId={view.jobId}
              selectedRunTs={view.selectedRunTs}
              onSelectRun={(run) => setView({ ...view, selectedRunTs: run.triggeredAtMs })}
              onBack={() => setView({ kind: "overview" })}
              jobName={selectedJob?.name ?? view.jobId}
              job={selectedJob}
              onCloseRun={() =>
                setView({ kind: "runs", jobId: view.jobId, selectedRunTs: null })
              }
              onRunNow={() => {
                if (selectedJob) handleRun(selectedJob.id);
              }}
              onEdit={() => {
                if (selectedJob) {
                  setEditingJob(selectedJob);
                  setMode("edit");
                }
              }}
            />
          );
        })()
      ))}
    </div>
  );
}
