// Normalizer for the browser tool's `GET /tabs` payload
// ({ running, tabs: [{ id, targetId, title, url }] }), enumerating every
// top-level page target (CDP Target.getTargets) — windows AND tabs alike, so a
// stuck OAuth popup shows up even though matchbox renders it as a separate
// fullscreen window. Kept in its own module (like browserViewUrl.ts) so the
// coercion is unit-testable without mounting the noVNC panel.

/** One open browser target. */
export type BrowserTab = {
  /**
   * Stable panel handle for addressing this target: the plugin's per-profile
   * tab alias (`t1`, `t2`, …) when present, else the raw CDP target id. Raw
   * target ids are volatile across form submits / target replacement (exactly
   * what an OAuth redirect does), but the alias migrates server-side, so Close
   * must address the tab by THIS id — DELETE /tabs/:id resolves it against the
   * current list's tabId. See the reference client's normalizeTab.
   */
  id: string;
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
    if (!targetId) return [];
    return [
      {
        // Prefer the stable alias; fall back to the raw target id.
        id: asString(record?.tabId) || targetId,
        targetId,
        title: asString(record?.title),
        url: asString(record?.url),
      },
    ];
  });
}

/** Display label for a tab: its title, else the URL host, else "New tab".
 * Same precedence as the fork Control-UI's tab strip (browser-panel-tabs.ts). */
export function tabLabel(tab: BrowserTab): string {
  const title = tab.title.trim();
  if (title) return title;
  try {
    return new URL(tab.url).host || "New tab";
  } catch {
    return tab.url || "New tab";
  }
}
