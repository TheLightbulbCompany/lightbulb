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
// The `browser` panel still renders the noVNC live view (BrowserDockPanel). The
// CDP screencast (ScreencastDockPanel + routers/screencast.py relay) ships
// DORMANT in this PR so the socat bridge + relay can deploy and every container
// can be rolled with ZERO user-visible change (panel stays VNC). A trivial
// follow-up flips this one line to ScreencastDockPanel once socat is on
// prod.tag fleet-wide — screencast goes live everywhere at once, no
// broken-panel window.
import { BrowserDockPanel } from "./panels/BrowserDockPanel";
import { MissingPanel } from "./panels/MissingPanel";

export const PANEL_COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>> = {
  chat: ChatPanel as React.FunctionComponent<IDockviewPanelProps>,
  teams: TeamsDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  control: ControlDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  "file-viewer": FileViewerDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  browser: BrowserDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  missing: MissingPanel as React.FunctionComponent<IDockviewPanelProps>,
  unknown: MissingPanel as React.FunctionComponent<IDockviewPanelProps>, // deserializer default key
};
