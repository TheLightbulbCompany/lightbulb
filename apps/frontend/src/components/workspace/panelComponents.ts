// What each panel kind renders in the main view or the pane. Panels read their
// params from props and change them through updatePanelParams / closePane.
import type { ComponentType } from "react";
import type { PanelKind } from "./panelRegistry";
import { ChatPanel } from "./panels/ChatPanel";
import { TeamsDockPanel } from "./panels/TeamsDockPanel";
import { ControlDockPanel } from "./panels/ControlDockPanel";
import { FileViewerDockPanel } from "./panels/FileViewerDockPanel";
import { BrowserDockPanel } from "./panels/BrowserDockPanel";
import { ActivityDockPanel } from "./panels/ActivityDockPanel";
import { CanvasDockPanel } from "./panels/CanvasDockPanel";
import { MailDockPanel } from "./panels/MailDockPanel";
import { ArtifactDockPanel } from "./panels/ArtifactDockPanel";
import { IdeasDockPanel } from "./panels/IdeasDockPanel";
import { TaskDockPanel } from "./panels/TaskDockPanel";

export interface PanelProps {
  /** The slot's panel id (updatePanelParams' key). */
  id: string;
  params: Record<string, string>;
}

export const PANEL_COMPONENTS: Record<PanelKind, ComponentType<PanelProps>> = {
  chat: ChatPanel,
  teams: TeamsDockPanel,
  control: ControlDockPanel,
  "file-viewer": FileViewerDockPanel,
  browser: BrowserDockPanel,
  activity: ActivityDockPanel,
  canvas: CanvasDockPanel,
  mail: MailDockPanel,
  ideas: IdeasDockPanel,
  artifact: ArtifactDockPanel,
  task: TaskDockPanel,
};
