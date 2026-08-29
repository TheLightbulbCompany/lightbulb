import { randomUUID } from "expo-crypto";
import type { PlatformAdapter } from "@lightbulb/shared";

import { API_URL } from "../config/env";

/**
 * Mobile PlatformAdapter factory. getToken comes from Clerk's useAuth() at
 * the call site (hook rules); the rest is static. expo-crypto's randomUUID
 * returns lowercase RFC-4122 v4 — required by OpenClaw's case-folded
 * session keys.
 */
export function makeAdapter(
  getToken: () => Promise<string | null>,
): PlatformAdapter {
  return {
    getToken,
    apiUrl: API_URL,
    uuid: () => randomUUID(),
  };
}
