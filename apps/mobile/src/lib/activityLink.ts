import type { Href } from "expo-router";

import { ROOT_DOMAIN, WEB_ORIGIN } from "../config/domain";

/**
 * Route a workspace Activity permalink (`/chat?panel=activity[&focus=<item id>]`,
 * absolute or relative) to the Activity tab. An agent's ask for a website
 * sign-in links here with `focus=login:<request id>`; the tab scrolls to that
 * item and opens its form.
 */
export function activityMobileHref(href?: string | null, origin = WEB_ORIGIN): Href | null {
  if (!href) return null;
  try {
    const relative = href.startsWith("/") && !href.startsWith("//");
    const url = new URL(relative ? `${origin}${href}` : href);
    if ((url.origin !== origin && url.origin !== `https://dev.${ROOT_DOMAIN}`)
      || url.pathname !== "/chat" || url.searchParams.get("panel") !== "activity") return null;
    const focus = url.searchParams.get("focus");
    return focus && focus.length <= 200
      ? { pathname: "/(app)/(tabs)/activity", params: { focus } }
      : "/(app)/(tabs)/activity";
  } catch {
    return null;
  }
}
