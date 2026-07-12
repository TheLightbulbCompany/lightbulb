"use client";

import type { IDockviewPanelProps } from "dockview-react";
import { FileViewer } from "@/components/chat/FileViewer";
import { useAgents } from "@/hooks/useAgents";
import { useWorkspaceState } from "../workspaceController";
import { useDockPanelParams } from "./useDockPanelParams";

interface FileViewerDockPanelParams {
  path?: string;
  agent?: string; // durable: the chat agent that opened the viewer
}

export function FileViewerDockPanel(props: IDockviewPanelProps<FileViewerDockPanelParams>): React.ReactElement {
  // Opening a file path into an already-open viewer arrives via
  // updateParameters — the hook merges dockview's PARTIAL payloads so the
  // viewer re-targets instead of ignoring the second open.
  const params = useDockPanelParams(props);

  // Agent resolution order: (1) the agent the opening chat panel bound into
  // the params (durable — survives restore), (2) the LAST focused chat agent
  // (sticky: dockview activates the viewer itself on open, so the plain
  // focused id is null at exactly the moment it matters), (3) the resolved
  // default agent so the tree still has a workspace to browse.
  const { lastFocusedChatAgentId } = useWorkspaceState();
  const { defaultId } = useAgents();
  const agentId = params.agent ?? lastFocusedChatAgentId ?? defaultId ?? null;

  return (
    <FileViewer
      agentId={agentId}
      initialFilePath={params.path ?? null}
      onClose={() => props.api.close()}
    />
  );
}
