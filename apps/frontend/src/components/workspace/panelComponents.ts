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
// The `browser` panel now renders the CDP screencast live view
// (ScreencastDockPanel + routers/screencast.py relay), NOT the old noVNC panel
// (BrowserDockPanel, kept as the fallback reference). This is the flip half of
// the #1939 two-step: it MUST NOT merge until the socat bridge is on the prod.tag
// fleet-wide, or every container without the CDP proxy shows the panel's
// disconnected/Retry state. Once socat is fleet-wide, screencast goes live
// everywhere at once with no broken-panel window. `browser` stays the
// parameterless-singleton key (#1874).
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
