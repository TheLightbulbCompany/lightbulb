/**
 * Deep links from the mobile app out to the web workspace.
 *
 * These lived as a `MANAGE_URL` constant copy-pasted into five Teams screens,
 * all of them pointing at `https://isol8.co/teams`. That route tree was
 * removed when Teams became a panel inside the workspace, so every copy had
 * been opening a 404 for real users — and no single place existed to fix.
 * One module, one set of builders, tested.
 *
 * The grammar is the workspace shell's `?panel=` URL contract
 * (`apps/frontend/src/components/workspace/panelRegistry.ts`):
 *
 *   list panels    /chat?panel=teams-<listKey>
 *   detail panels  /chat?panel=teams-<kind>-detail&detailId=<id>
 *
 * Detail keys are load-bearing: `resolvePanel` returns NULL for a
 * `teams-*-detail` key with no `detailId`, so a detail link without an id
 * would resolve to nothing at all. The builders below fall back to the
 * matching list panel in that case rather than emitting a dead link.
 */

const WEB_ORIGIN = "https://isol8.co";

/** The workspace, no particular panel. */
export const workspaceUrl = (): string => `${WEB_ORIGIN}/chat`;

/** A Teams list panel — e.g. "issues", "routines", "inbox". */
export const teamsPanelUrl = (listKey: string): string =>
  `${WEB_ORIGIN}/chat?panel=teams-${listKey}`;

/**
 * A Teams detail panel. Falls back to `fallbackListKey` when the id is
 * missing, because a detail panel without a detailId resolves to null.
 */
export const teamsDetailUrl = (
  kind: string,
  detailId: string | undefined,
  fallbackListKey: string,
): string =>
  detailId
    ? `${WEB_ORIGIN}/chat?panel=teams-${kind}-detail&detailId=${encodeURIComponent(detailId)}`
    : teamsPanelUrl(fallbackListKey);
