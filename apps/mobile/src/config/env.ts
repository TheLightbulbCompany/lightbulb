/**
 * Environment configuration for the mobile app.
 *
 * Mirrors the web frontend's posture: only safe-public values, provided via
 * EXPO_PUBLIC_* (the Expo equivalent of NEXT_PUBLIC_*). Values are inlined at
 * bundle time by Metro, so a rebuild is required when they change.
 */
import { ROOT_DOMAIN, WEB_ORIGIN } from "./domain";

export const API_URL =
  process.env.EXPO_PUBLIC_API_URL ?? `https://api-dev.${ROOT_DOMAIN}/api/v1`;

