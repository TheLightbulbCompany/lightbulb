"use client";

import type { IDockviewPanelProps } from "dockview-react";
import { useEffect, useState } from "react";
import { FileViewer } from "@/components/chat/FileViewer";
import { useAgents } from "@/hooks/useAgents";
import { useWorkspaceState } from "../workspaceController";

interface FileViewerDockPanelParams {
  path?: string;
}

export function FileViewerDockPanel(props: IDockviewPanelProps<FileViewerDockPanelParams>): React.ReactElement {
  // The viewer follows the focused chat agent (already sentinel-RESOLVED in the
  // snapshot); when nothing is focused, fall back to the resolved default agent
  // so the tree still has a workspace to browse.
  const { focusedChatAgentId } = useWorkspaceState();
  const { defaultId } = useAgents();
  const agentId = focusedChatAgentId ?? defaultId ?? null;

  // Opening a file path into an already-open viewer arrives via
  // updateParameters — subscribe so the viewer re-targets instead of ignoring
  // the second open.
  const [params, setParams] = useState(props.params);
  useEffect(() => {
    const d = props.api.onDidParametersChange((next) => setParams(next as FileViewerDockPanelParams));
    return () => d.dispose();
  }, [props.api]);

  return (
    <FileViewer
      agentId={agentId}
      initialFilePath={params.path ?? null}
      onClose={() => props.api.close()}
    />
  );
}
