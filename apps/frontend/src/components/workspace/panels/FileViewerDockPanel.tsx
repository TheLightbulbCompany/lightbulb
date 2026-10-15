"use client";

import { FileViewer } from "@/components/chat/FileViewer";
import { useAgents } from "@/hooks/useAgents";
import { closePane, useWorkspaceState } from "../workspaceController";
import type { PanelProps } from "../panelComponents";

interface FileViewerDockPanelParams {
  path?: string;
  agent?: string; // durable: the chat agent that opened the viewer
}

export function FileViewerDockPanel(props: PanelProps): React.ReactElement {
  // Opening a file path into an already-open viewer arrives as fresh params.
  const params = props.params as FileViewerDockPanelParams;

  // Agent resolution order: (1) the agent the opening chat panel bound into
  // the params, (2) the LAST chat agent on screen (sticky: opened from a
  // non-Chat place, the plain focused id is null), (3) the resolved
  // default agent so the tree still has a workspace to browse.
  const { lastFocusedChatAgentId } = useWorkspaceState();
  const { defaultId } = useAgents();
  const agentId = params.agent ?? lastFocusedChatAgentId ?? defaultId ?? null;

  return (
    <FileViewer
      agentId={agentId}
      initialFilePath={params.path ?? null}
      onClose={closePane}
    />
  );
}
