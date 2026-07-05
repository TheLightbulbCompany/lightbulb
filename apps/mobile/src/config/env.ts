/**
 * Environment configuration for the mobile app.
 *
 * Mirrors the web frontend's posture: only safe-public values, provided via
 * EXPO_PUBLIC_* (the Expo equivalent of NEXT_PUBLIC_*). Values are inlined at
 * bundle time by Metro, so a rebuild is required when they change.
 *
 * WS URL derivation (api.isol8.co → ws.isol8.co, strip /api/v1) is duplicated
 * here only until @isol8/shared exports deriveWebSocketUrl (Phase 1) — at
 * which point this module delegates to it.
 */

export const API_URL =
  process.env.EXPO_PUBLIC_API_URL ?? "https://api-dev.isol8.co/api/v1";

export function deriveWebSocketUrl(apiUrl: string): string {
  if (process.env.EXPO_PUBLIC_WS_URL) {
    return process.env.EXPO_PUBLIC_WS_URL;
  }
  return apiUrl
    .replace(/^https:\/\//, "wss://")
    .replace(/^http:\/\//, "ws://")
    .replace(/\/\/api(?=[-.])/, "//ws")
    .replace(/\/api\/v1$/, "");
}

export const WS_URL = deriveWebSocketUrl(API_URL);
