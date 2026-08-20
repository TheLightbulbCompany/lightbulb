// Normalizer for the browser tool's `GET /tabs` payload
// ({ running, tabs: [{ id, targetId, title, url }] }), enumerating every
// top-level page target (CDP Target.getTargets) — windows AND tabs alike, so a
// stuck OAuth popup shows up even though matchbox renders it as a separate
// fullscreen window. Kept in its own module (like browserViewUrl.ts) so the
// coercion is unit-testable without mounting the noVNC panel.

/** One open browser target. Closed via DELETE /tabs/:targetId. */
export type BrowserTab = {
  targetId: string;
  title: string;
  url: string;
};

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function normalizeTabs(payload: unknown): BrowserTab[] {
  const tabs = (payload as { tabs?: unknown } | null)?.tabs;
  if (!Array.isArray(tabs)) return [];
  return tabs.flatMap((tab) => {
    const record = tab as Record<string, unknown> | null;
    const targetId = asString(record?.targetId);
    // Raw target ids are volatile across navigations, but they're what
    // DELETE /tabs/:targetId addresses; a target with no id can't be closed.
    if (!targetId) return [];
    return [{ targetId, title: asString(record?.title), url: asString(record?.url) }];
  });
}
