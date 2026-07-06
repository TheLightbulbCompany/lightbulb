/**
 * Environment configuration for the mobile app.
 *
 * Mirrors the web frontend's posture: only safe-public values, provided via
 * EXPO_PUBLIC_* (the Expo equivalent of NEXT_PUBLIC_*). Values are inlined at
 * bundle time by Metro, so a rebuild is required when they change.
 */
import { deriveWebSocketUrl } from "@isol8/shared";

export const API_URL =
  process.env.EXPO_PUBLIC_API_URL ?? "https://api-dev.isol8.co/api/v1";

export const WS_URL = deriveWebSocketUrl(
  API_URL,
  process.env.EXPO_PUBLIC_WS_URL,
);
