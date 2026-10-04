// The module-scope components map dockview's `components` prop is watched
// against. Identity-stable (module singleton) so the shell never re-registers
// panels on re-render. Keys are the PanelKind literals from panelRegistry plus
// `unknown` — the deserializer defaults any missing contentComponent to the
// literal `'unknown'`, which routes to MissingPanel like the explicit `missing`.
import type { IDockviewPanelProps } from "dockview-react";
import { ChatPanel } from "./panels/ChatPanel";
import { TeamsDockPanel } from "./panels/TeamsDockPanel";
import { ControlDockPanel } from "./panels/ControlDockPanel";
import { FileViewerDockPanel } from "./panels/FileViewerDockPanel";
import { BrowserDockPanel } from "./panels/BrowserDockPanel";
import { ActivityDockPanel } from "./panels/ActivityDockPanel";
import { MailDockPanel } from "./panels/MailDockPanel";
import { MissingPanel } from "./panels/MissingPanel";

export const PANEL_COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>> = {
  chat: ChatPanel as React.FunctionComponent<IDockviewPanelProps>,
  teams: TeamsDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  control: ControlDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  "file-viewer": FileViewerDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  browser: BrowserDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  activity: ActivityDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  mail: MailDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  missing: MissingPanel as React.FunctionComponent<IDockviewPanelProps>,
  unknown: MissingPanel as React.FunctionComponent<IDockviewPanelProps>, // deserializer default key
};
