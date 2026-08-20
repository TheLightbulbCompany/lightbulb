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
// The `browser` panel now renders the CDP screencast (ScreencastDockPanel).
// BrowserDockPanel (noVNC) stays in the tree as the fallback path — the VNC
// relay + container Xvnc/websockify boot are intact — until the screencast is
// prod-validated and the VNC path is retired.
import { ScreencastDockPanel } from "./panels/ScreencastDockPanel";
import { MissingPanel } from "./panels/MissingPanel";

export const PANEL_COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>> = {
  chat: ChatPanel as React.FunctionComponent<IDockviewPanelProps>,
  teams: TeamsDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  control: ControlDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  "file-viewer": FileViewerDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  browser: ScreencastDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  missing: MissingPanel as React.FunctionComponent<IDockviewPanelProps>,
  unknown: MissingPanel as React.FunctionComponent<IDockviewPanelProps>, // deserializer default key
};
