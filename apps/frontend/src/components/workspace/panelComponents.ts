// What each panel kind renders in the right pane. Panels read their params from props and change them through
// updatePanelParams / closePane. Only the pane kinds: no page attaches the shell any more (voice pivot), so a place
// (chat, home, activity, settings, team) never renders through here, and nothing opens the mail or control pane.
import type { ComponentType } from "react";
import type { PanelKind } from "./panelRegistry";
import { FileViewerDockPanel } from "./panels/FileViewerDockPanel";
import { ArtifactDockPanel } from "./panels/ArtifactDockPanel";
import { TaskDockPanel } from "./panels/TaskDockPanel";
import { AutomationDockPanel } from "./panels/AutomationDockPanel";

export interface PanelProps {
  /** The slot's panel id (updatePanelParams' key). */
  id: string;
  params: Record<string, string>;
}

export const PANEL_COMPONENTS: Partial<Record<PanelKind, ComponentType<PanelProps>>> = {
  "file-viewer": FileViewerDockPanel,
  artifact: ArtifactDockPanel,
  task: TaskDockPanel,
  automation: AutomationDockPanel,
};
