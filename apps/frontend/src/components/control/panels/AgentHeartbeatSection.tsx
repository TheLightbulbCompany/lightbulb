"use client";

import { useCallback, useState } from "react";
import { FileContentViewer } from "@/components/chat/FileContentViewer";
import { EditorialButton } from "@/components/teams/shell";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { useConfigFile } from "@/hooks/useWorkspaceFiles";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useApi } from "@/lib/api";

const CHECKLIST_FILE = "HEARTBEAT.md";

// What the backend's arm answered when turning on armed nothing (arm_nightly_ceo).
const NOT_ARMED: Record<string, string> = {
  not_entitled: "Heartbeats need an active plan or trial.",
  company_not_active: "Your team is still being set up. Try again in a minute.",
  heartbeats_disabled: "Heartbeats are turned off for this workspace.",
  friday_not_seeded: "Lightbulb isn't set up in this workspace yet.",
};

function formatInterval(sec: number): string {
  return sec % 3600 === 0 ? `${sec / 3600} h` : `${Math.round(sec / 60)} min`;
}

/**
 * The agent's heartbeat: its Paperclip timer (on/off + interval) and its own
 * checklist, the profile's HEARTBEAT.md that the agent follows on every timer
 * wake (an empty file falls back to the bundled default). The checklist saves
 * through the same config-file PUT as the Instructions tab; only admins can
 * save it (the backend 403s everyone else). Only the CEO has an on/off toggle —
 * the specialists' daily timer is re-applied by the backend.
 */
export function AgentHeartbeatSection({
  agentId,
  agent,
}: {
  agentId: string;
  agent?: CompanyAgent;
}) {
  const isAdmin = useIsWorkspaceAdmin();
  const api = useApi();
  const { post, mutate } = useTeamsApi();
  const { file, isLoading, error } = useConfigFile(agentId, CHECKLIST_FILE);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const heartbeat = agent?.runtimeConfig?.heartbeat;
  const on = heartbeat?.enabled === true;
  const isCeo = agent?.role === "ceo";

  const handleSave = useCallback(
    async (content: string) => {
      await api.saveWorkspaceFile(agentId, CHECKLIST_FILE, content, "config");
    },
    [api, agentId],
  );

  const toggle = useCallback(async () => {
    setBusy(true);
    setNotice(null);
    try {
      const res = await post<{ status?: string }>(`/agents/${agentId}/heartbeat`, { enabled: !on });
      if (!on && res?.status !== "armed") {
        setNotice(NOT_ARMED[res?.status ?? ""] ?? "The heartbeat did not turn on. Try again.");
      }
      mutate("/teams/agents");
      mutate(`/teams/agents/${agentId}`);
    } catch {
      setNotice("Couldn't change the heartbeat. Try again.");
    } finally {
      setBusy(false);
    }
  }, [post, mutate, agentId, on]);

  return (
    <section className="py-4 flex flex-col gap-3" aria-labelledby="heartbeat-heading">
      <h3 id="heartbeat-heading" className="font-editorial-serif text-base">
        Heartbeat
      </h3>
      <div className="flex items-center gap-3 text-sm">
        <span className="text-[var(--midnight-soft)]">
          {heartbeat
            ? on
              ? `On · wakes every ${formatInterval(heartbeat.intervalSec ?? 0)}`
              : "Off"
            : "Not set"}
        </span>
        {isCeo && isAdmin ? (
          <EditorialButton variant="secondary" size="sm" disabled={busy} onClick={toggle}>
            {on ? "Turn off" : "Turn on"}
          </EditorialButton>
        ) : null}
      </div>
      {notice ? (
        <p role="alert" className="text-caption text-err">
          {notice}
        </p>
      ) : null}
      <h4 className="label-caps text-[var(--midnight-muted)]">Heartbeat checklist</h4>
      <p className="text-caption text-[var(--midnight-muted)]">
        What this agent does on every timer wake. Leave it empty to use the built-in
        default.{isAdmin ? "" : " Only workspace admins can edit it."}
      </p>
      <div className="h-80 border border-[var(--edge)] rounded-md overflow-hidden">
        <FileContentViewer
          file={file}
          isLoading={isLoading}
          error={error ?? null}
          onSave={isAdmin ? handleSave : undefined}
        />
      </div>
    </section>
  );
}
