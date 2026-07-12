"use client";

import { useEffect, useMemo, useState } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import { SlidersHorizontal, ListTodo, Pause, Play, X } from "lucide-react";
import { AgentChatWindow } from "@/components/chat/AgentChatWindow";
import { useAgents, agentDisplayName } from "@/hooks/useAgents";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";
import { openPanel } from "../workspaceController";

interface ChatPanelParams {
  agentId: string;
}

export function ChatPanel(props: IDockviewPanelProps<ChatPanelParams>): React.ReactElement {
  const { agents, defaultId, isSettled } = useAgents();
  const raw = props.params.agentId;
  const resolvedId = raw === DEFAULT_AGENT_SENTINEL ? (defaultId ?? null) : raw;

  // Agent-gone gating (spec §4): ONLY once the roster has settled
  // (data !== undefined). isLoading is false during SWR error-retry cycles
  // and the `?? []` default makes membership checks lie during connect —
  // both would false-fire this state on every persisted panel at every load.
  const agentGone =
    isSettled && raw !== DEFAULT_AGENT_SENTINEL && !agents.some((a) => a.id === raw);

  // Per-agent action cluster (moved from ChatLayout's header — it has no
  // single referent in a multi-chat world). Roster read is the GLOBAL
  // /teams/agents SWR key: N panels dedupe to one fetch, and pause/resume
  // revalidates the same key so every panel's icon updates together.
  const { read: readTeams } = useTeamsApi();
  const { data: teamsAgentsData } = readTeams<{ agents?: { id: string; status?: string | null }[] }>(
    "/agents",
  );
  const { pause, resume } = useAgentMutations();
  const paused =
    teamsAgentsData?.agents?.find((a) => a.id === resolvedId)?.status === "paused";
  const [busy, setBusy] = useState(false);

  const title = useMemo(() => {
    const agent = agents.find((a) => a.id === resolvedId);
    return agent ? agentDisplayName(agent) : raw === DEFAULT_AGENT_SENTINEL ? "Chat" : raw;
  }, [agents, resolvedId, raw]);

  // Keep the dockview tab title in sync with the resolved agent name. An
  // effect, not a render-phase write: setTitle fires dockview's
  // onDidLayoutChange as an AsapEvent microtask, which would otherwise run
  // BEFORE React's passive effects sync the shell's defaultIdRef — the
  // controller's recompute would then read the stale resolver.
  useEffect(() => {
    if (props.api.title !== title) props.api.setTitle(title);
  }, [props.api, title]);

  if (agentGone) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-sm text-[#8a8578]">
        <p>This agent no longer exists.</p>
        <button
          className="rounded border border-[#e0dbd0] px-3 py-1 hover:bg-[#f3efe6]"
          onClick={() => props.api.close()}
        >
          <X size={14} className="mr-1 inline" /> Close panel
        </button>
      </div>
    );
  }

  async function togglePause(): Promise<void> {
    if (!resolvedId || busy) return;
    setBusy(true);
    try {
      if (paused) await resume(resolvedId);
      else await pause(resolvedId);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex h-10 shrink-0 items-center gap-1 border-b border-[#e0dbd0] bg-[#faf7f2] px-2">
        <span className="truncate text-sm font-medium">{title}</span>
        <div className="flex-1" />
        {resolvedId && (
          <>
            <button title="Inspect agent" onClick={() => openPanel("agent-inspector", { agent: resolvedId }, { pushUrl: true })}>
              <SlidersHorizontal size={16} />
            </button>
            <button title="Agent tasks" onClick={() => openPanel("teams-issues", { agent: resolvedId }, { pushUrl: true })}>
              <ListTodo size={16} />
            </button>
            <button title={paused ? "Resume agent" : "Pause agent"} disabled={busy} onClick={togglePause}>
              {paused ? <Play size={16} /> : <Pause size={16} />}
            </button>
          </>
        )}
      </div>
      <div className="min-h-0 flex-1">
        <AgentChatWindow
          agentId={resolvedId}
          onOpenFile={(path) => openPanel("file-viewer", path ? { path } : {})}
        />
      </div>
    </div>
  );
}
