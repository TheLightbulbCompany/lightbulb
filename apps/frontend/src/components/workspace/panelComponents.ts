// What each panel kind renders in the main view or the pane. Panels read their
// params from props and change them through updatePanelParams / closePane.
import type { ComponentType } from "react";
import type { PanelKind } from "./panelRegistry";
import { ChatPanel } from "./panels/ChatPanel";
import { TeamsDockPanel } from "./panels/TeamsDockPanel";
import { ControlDockPanel } from "./panels/ControlDockPanel";
import { FileViewerDockPanel } from "./panels/FileViewerDockPanel";
import { ActivityDockPanel } from "./panels/ActivityDockPanel";
import { HomeProjects } from "@/components/home/HomeProjects";
import { MailDockPanel } from "./panels/MailDockPanel";
import { ArtifactDockPanel } from "./panels/ArtifactDockPanel";
import { TaskDockPanel } from "./panels/TaskDockPanel";
import { AutomationDockPanel } from "./panels/AutomationDockPanel";
import { SettingsPlace } from "@/components/settings/SettingsPlace";

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
  activity: ActivityDockPanel,
  home: HomeProjects,
  mail: MailDockPanel,
  artifact: ArtifactDockPanel,
  task: TaskDockPanel,
  automation: AutomationDockPanel,
  settings: SettingsPlace,
};
