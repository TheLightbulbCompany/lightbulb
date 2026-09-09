"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { IDockviewPanelProps } from "dockview-react";
import {
  ArrowUpRight,
  Cloud,
  Images,
  Laptop,
  ListTodo,
  LockKeyhole,
  Pause,
  Play,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { AgentChatWindow } from "@/components/chat/AgentChatWindow";
import { useAgents, agentDisplayName } from "@/hooks/useAgents";
import { useGateway } from "@/hooks/useGateway";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useAgentMutations } from "@/components/teams/agents/hooks/useAgentMutations";
import { classifyAgentStatus } from "@/components/teams/shared/lib/agentStatus";
import { DEFAULT_AGENT_SENTINEL } from "../panelRegistry";
import { openPanel } from "../workspaceController";

interface ChatPanelParams {
  agentId: string;
}

export function ChatPanel(props: IDockviewPanelProps<ChatPanelParams>): React.ReactElement {
  const { agents, defaultId, isSettled } = useAgents();
  const { nodeConnected, nodeStatus } = useGateway();
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
  const agentStatus = teamsAgentsData?.agents?.find((a) => a.id === resolvedId)?.status;
  const statusClass = classifyAgentStatus(agentStatus);
  const paused = statusClass === "paused";
  const statusLabel =
    statusClass === "live"
      ? "Working"
      : statusClass === "attention"
        ? "Needs attention"
        : statusClass === "paused"
          ? "Paused"
          : statusClass === "terminated"
            ? "Unavailable"
            : statusClass === "idle"
              ? "Ready"
              : "Status unavailable";
  const [busy, setBusy] = useState(false);

  const title = useMemo(() => {
    const agent = agents.find((a) => a.id === resolvedId);
    if (agent) return agentDisplayName(agent);
    if (raw === DEFAULT_AGENT_SENTINEL) return "Chat";
    // Roster not settled yet (e.g. onboarding, gateway still connecting):
    // a raw agent UUID as the tab title reads as a bug — hold the neutral
    // "Chat" and let the memo re-resolve to the agent name once the roster
    // arrives (this memo is reactive to `agents`). Once settled, an unknown
    // id falls through to the agent-gone state below anyway.
    return isSettled ? raw : "Chat";
  }, [agents, resolvedId, raw, isSettled]);

  // Stable identity: MessageList's memoized bubble tree compares onOpenFile
  // by reference — an inline arrow here would defeat the bail-outs whenever
  // this panel re-renders. Bind the viewer to THIS chat's agent: dockview
  // activates the freshly opened viewer, so the focused-chat fallback
  // resolves to null (→ default agent) at exactly the moment the file opens.
  const handleOpenFile = useCallback(
    (path: string) =>
      openPanel("file-viewer", {
        ...(path ? { path } : {}),
        ...(resolvedId ? { agent: resolvedId } : {}),
      }),
    [resolvedId],
  );

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
      <div className="flex h-full flex-col items-center justify-center gap-3 text-sm text-[var(--midnight-muted)]">
        <p>This agent no longer exists.</p>
        <button
          className="rounded border border-[var(--edge)] px-3 py-1 hover:bg-[var(--cream-deep)]"
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
    <div className="team-room h-full min-h-0">
      <header className="team-room__header">
        <div className="min-w-0">
          <span className="team-room__overline">Workspace</span>
          <div className="team-room__heading-row">
            <h2 className="team-room__heading font-editorial-serif">Private with {title}</h2>
            <span className="team-room__audience">
              <LockKeyhole size={12} aria-hidden="true" /> Private
            </span>
          </div>
          <p className="team-room__status">{title} · {statusLabel}</p>
        </div>
        {resolvedId ? (
          <div className="team-room__actions">
            <button title="Inspect agent" aria-label="Inspect agent" className="team-room__icon-button" onClick={() => openPanel("agent-inspector", { agent: resolvedId }, { pushUrl: true })}>
              <SlidersHorizontal size={16} />
            </button>
            <button title="Agent tasks" aria-label="Agent tasks" className="team-room__icon-button" onClick={() => openPanel("teams-issues", { agent: resolvedId }, { pushUrl: true })}>
              <ListTodo size={16} />
            </button>
            <button title={paused ? "Resume agent" : "Pause agent"} aria-label={paused ? "Resume agent" : "Pause agent"} className="team-room__icon-button" disabled={busy} onClick={togglePause}>
              {paused ? <Play size={16} /> : <Pause size={16} />}
            </button>
          </div>
        ) : null}
      </header>
      <div className="team-room__body">
        <div className="team-room__conversation">
          <AgentChatWindow agentId={resolvedId} onOpenFile={handleOpenFile} />
        </div>
        {resolvedId ? (
          <aside className="team-room__context" aria-label="Conversation context">
            <section className="team-room__section">
              <span className="team-room__section-label">Conversation</span>
              <div className="team-room__context-title">
                <LockKeyhole size={16} aria-hidden="true" />
                <strong>Private</strong>
              </div>
              <p>This conversation is isolated to your signed-in profile.</p>
            </section>

            <section className="team-room__section">
              <span className="team-room__section-label">Work</span>
              <p>Keep the task board and finished artifacts beside the conversation.</p>
              <div className="team-room__work-links">
                <button
                  type="button"
                  onClick={() => openPanel("teams-issues", { agent: resolvedId }, { pushUrl: true })}
                >
                  <ListTodo size={15} aria-hidden="true" />
                  <span>Open {title}&apos;s tasks</span>
                  <ArrowUpRight size={14} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  onClick={() => openPanel("teams-artifacts", {}, { pushUrl: true })}
                >
                  <Images size={15} aria-hidden="true" />
                  <span>Browse Library</span>
                  <ArrowUpRight size={14} aria-hidden="true" />
                </button>
              </div>
            </section>

            <section className="team-room__section">
              <span className="team-room__section-label">Execution</span>
              <div className="team-room__execution-row">
                <Cloud size={17} aria-hidden="true" />
                <div>
                  <strong>Lightbulb cloud</strong>
                  <span>{title}&apos;s agent runtime</span>
                </div>
                <b className={statusClass === "attention" ? "text-err" : statusClass === "live" ? "text-info" : statusClass === "paused" ? "text-warn" : "text-[var(--midnight-muted)]"}>
                  {statusLabel}
                </b>
              </div>
              <div className="team-room__execution-row">
                <Laptop size={17} aria-hidden="true" />
                <div>
                  <strong>Local node</strong>
                  <span>
                    {nodeConnected
                      ? "Local tools available"
                      : nodeStatus === "error"
                        ? "Connection failing"
                        : "No local node connected"}
                  </span>
                </div>
                <b className={nodeConnected ? "text-ok" : nodeStatus === "error" ? "text-warn" : "text-[var(--midnight-muted)]"}>
                  {nodeConnected ? "Ready" : nodeStatus === "error" ? "Needs attention" : "Not connected"}
                </b>
              </div>
            </section>
          </aside>
        ) : null}
      </div>
    </div>
  );
}
