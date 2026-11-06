// Where the shell's right pane stops fitting beside the main view and opens as a sheet over it (workspace.css).
// Super-agent (UX slice 4): below 1000 px the pane is always an overlay sheet from the right, so it never squeezes the
// transcript (RightPane also sheets it at ≥1000 when the main column would drop under 360). Without it the sidebar
// column stays: 56 + 220 + 360 + 320 = 956 px. Below 600 px it is a full-screen sheet either way.
export const PANE_SHEET_QUERIES = { superAgent: "(max-width: 999px)", classic: "(max-width: 955px)" } as const;
export const paneSheetQuery = (superAgent: boolean): string => superAgent ? PANE_SHEET_QUERIES.superAgent : PANE_SHEET_QUERIES.classic;
/** Super-agent: the conversations sidebar is a drawer below this width; without it, only on a phone. */
export const drawerUntil = (superAgent: boolean): number => superAgent ? 1000 : 600;
