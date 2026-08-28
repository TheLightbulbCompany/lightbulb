// apps/frontend/src/components/control/panels/SkillProposalsPanel.tsx
"use client";

import { useState } from "react";
import { Loader2, RefreshCw, FileCheck, Check, X } from "lucide-react";
import { useGatewayRpc, useGatewayRpcMutation } from "@/hooks/useGatewayRpc";
import { Button } from "@/components/ui/button";
import {
  type SkillProposalManifest,
  type SkillProposalReadResult,
} from "./skill-proposals-types";

export function SkillProposalsPanel() {
  const { data, error, isLoading, mutate } =
    useGatewayRpc<SkillProposalManifest>("skills.proposals.list");
  const [selected, setSelected] = useState<string | null>(null);

  if (isLoading && !data) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-[var(--midnight-muted)]" />
      </div>
    );
  }
  if (error && !data) {
    return (
      <div className="p-6 space-y-3">
        <p className="text-sm text-destructive">Failed to load proposals: {error.message}</p>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw className="h-3.5 w-3.5 mr-1.5" /> Retry
        </Button>
      </div>
    );
  }

  const proposals = data?.proposals ?? [];

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <FileCheck className="h-5 w-5 text-[var(--midnight-muted)]" />
          <h2 className="text-lg font-medium text-[var(--midnight)]">Skill Proposals</h2>
        </div>
        <Button variant="outline" size="sm" onClick={() => mutate()}>
          <RefreshCw className="h-3.5 w-3.5 mr-1.5" /> Reload
        </Button>
      </div>

      {proposals.length === 0 ? (
        <p className="text-sm text-[var(--midnight-muted)]">No skill proposals. Agents create these via the skill_workshop tool.</p>
      ) : (
        <div className="space-y-2">
          {proposals.map((p) => (
            <div key={p.id} className="rounded-lg border bg-white">
              <button
                className="w-full text-left p-3 flex items-center justify-between"
                onClick={() => setSelected(selected === p.id ? null : p.id)}
              >
                <div>
                  <p className="text-sm font-medium text-[var(--midnight)]">{p.title}</p>
                  <p className="text-xs text-[var(--midnight-muted)]">{p.skillName} · {p.kind} · scan: {p.scanState}</p>
                </div>
                <span className={`text-xs px-2 py-0.5 rounded-full ${
                  p.status === "pending" ? "bg-amber-100 text-amber-700" :
                  p.status === "applied" ? "bg-emerald-100 text-emerald-700" :
                  "bg-[#eee] text-[var(--midnight-muted)]"
                }`}>{p.status}</span>
              </button>
              {selected === p.id && <ProposalDetail proposalId={p.id} onResolved={() => mutate()} />}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ProposalDetail({ proposalId, onResolved }: { proposalId: string; onResolved: () => void }) {
  const { data, error, isLoading } = useGatewayRpc<SkillProposalReadResult>(
    "skills.proposals.inspect",
    { proposalId },
  );
  const callRpc = useGatewayRpcMutation();
  const [busy, setBusy] = useState<"apply" | "reject" | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const act = async (action: "apply" | "reject") => {
    if (action === "reject" && !window.confirm("Reject this proposal?")) return;
    setBusy(action);
    setMsg(null);
    try {
      await callRpc(`skills.proposals.${action}`, { proposalId });
      onResolved();
    } catch (e) {
      setMsg(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(null);
    }
  };

  if (isLoading && !data) return <div className="p-3"><Loader2 className="h-4 w-4 animate-spin text-[var(--midnight-muted)]" /></div>;
  if (error && !data) return <p className="p-3 text-sm text-destructive">{error.message}</p>;

  const pending = data?.record.status === "pending";

  return (
    <div className="border-t p-3 space-y-3">
      {data?.record.goal && <p className="text-xs text-[var(--midnight-muted)]"><span className="font-medium">Goal:</span> {data.record.goal}</p>}
      <pre className="whitespace-pre-wrap text-sm bg-[#faf9f6] border rounded-md p-3 max-h-[40vh] overflow-auto">
        {data?.content ?? ""}
      </pre>
      {pending && (
        <div className="flex gap-2">
          <Button size="sm" disabled={busy !== null} onClick={() => act("apply")}>
            <Check className="h-3.5 w-3.5 mr-1.5" /> {busy === "apply" ? "Applying…" : "Apply"}
          </Button>
          <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => act("reject")}>
            <X className="h-3.5 w-3.5 mr-1.5" /> {busy === "reject" ? "Rejecting…" : "Reject"}
          </Button>
        </div>
      )}
      {msg && <p className="text-sm text-destructive">{msg}</p>}
    </div>
  );
}
