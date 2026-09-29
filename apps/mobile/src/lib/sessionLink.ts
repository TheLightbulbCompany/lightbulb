import type { Href } from "expo-router";

import { ROOT_DOMAIN, WEB_ORIGIN } from "../config/domain";

/** Route a workspace session permalink to its native screen. */
export function sessionMobileHref(href?: string | null, origin = WEB_ORIGIN): Href | null {
  if (!href) return null;
  try {
    const url = new URL(href);
    if ((url.origin !== origin && url.origin !== `https://dev.${ROOT_DOMAIN}`)
      || url.pathname !== "/chat" || url.searchParams.get("panel") !== "session") return null;
    const sessionId = url.searchParams.get("session");
    return sessionId ? { pathname: "/(app)/session/[sessionId]", params: { sessionId } } : null;
  } catch {
    return null;
  }
}
