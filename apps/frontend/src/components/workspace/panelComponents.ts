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
// The `browser` panel renders the noVNC live view (BrowserDockPanel). VNC
// streams the REAL Chrome window (real tabs/omnibox/extensions/infobars); the
// ~4fps that once made it feel slow was a compositor flag, fixed in #1950
// (--disable-gpu, ~8x) — so VNC is native AND fast. (A CDP-screencast panel was
// tried and removed: Page.startScreencast captures PAGE content only, so it had
// to rebuild a fake chrome that never matched native and felt broken to users.)
// `browser` stays the parameterless-singleton key (#1874).
import { BrowserDockPanel } from "./panels/BrowserDockPanel";
import { SessionDockPanel } from "./panels/SessionDockPanel";
import { MissingPanel } from "./panels/MissingPanel";

export const PANEL_COMPONENTS: Record<string, React.FunctionComponent<IDockviewPanelProps>> = {
  chat: ChatPanel as React.FunctionComponent<IDockviewPanelProps>,
  teams: TeamsDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  control: ControlDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  "file-viewer": FileViewerDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  browser: BrowserDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  session: SessionDockPanel as React.FunctionComponent<IDockviewPanelProps>,
  missing: MissingPanel as React.FunctionComponent<IDockviewPanelProps>,
  unknown: MissingPanel as React.FunctionComponent<IDockviewPanelProps>, // deserializer default key
};
