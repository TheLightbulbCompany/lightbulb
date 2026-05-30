"use client";

import { useTeamsApi } from "@/hooks/useTeamsApi";

interface Run {
  id: string;
  status: string;
  transcript?: string;
}

export function RunDetailPanel({ runId }: { runId: string }) {
  const { read } = useTeamsApi();
  // A run-detail link can target a heartbeat-run (failed-runs, issue-runs) or a
  // legacy generic run (older agent-run rows), served by different BFF routes.
  // Read both and prefer the heartbeat-run; the other resolves to undefined.
  const hb = read<Run>(`/heartbeat-runs/${runId}`, { shouldRetryOnError: false });
  const legacy = read<Run>(`/runs/${runId}`, { shouldRetryOnError: false });
  const data = hb.data ?? legacy.data;
  const isLoading = (hb.isLoading || legacy.isLoading) && !data;

  if (isLoading) return <div className="p-8">Loading…</div>;

  return (
    <div className="p-8 max-w-4xl">
      <h1 className="text-xl font-semibold mb-4">Run {data?.id ?? runId}</h1>
      <div className="text-sm text-zinc-500 mb-4">
        Status: {data?.status ?? "unknown"}
      </div>
      <pre className="bg-zinc-50 border rounded p-4 text-xs whitespace-pre-wrap">
        {data?.transcript ?? "(no transcript)"}
      </pre>
    </div>
  );
}
