// Where the shell's right pane stops fitting beside the main view and opens as a sheet over it (workspace.css).
// Super-agent: rail 56 + main 360 + pane 320 = 736 px, the sidebar being a drawer below 1000 px. Without it the
// sidebar column stays: 56 + 220 + 360 + 320 = 956 px. Below 600 px it is a full-screen sheet either way.
export const PANE_SHEET_QUERIES = { superAgent: "(max-width: 735px)", classic: "(max-width: 955px)" } as const;
export const paneSheetQuery = (superAgent: boolean): string => superAgent ? PANE_SHEET_QUERIES.superAgent : PANE_SHEET_QUERIES.classic;
/** Super-agent: the conversations sidebar is a drawer below this width; without it, only on a phone. */
export const drawerUntil = (superAgent: boolean): number => superAgent ? 1000 : 600;
