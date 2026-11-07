// Where the shell's right pane stops fitting beside the main view and opens as a sheet over it (workspace.css).
// UX slice 4: below 1000 px the pane is always an overlay sheet from the right, so it never squeezes the
// transcript (RightPane also sheets it at ≥1000 when the main column would drop under 360). Below 600 px it is a
// full-screen sheet.
export const PANE_SHEET_QUERY = "(max-width: 999px)";
/** The conversations sidebar is a drawer below this width. */
export const DRAWER_UNTIL = 1000;
