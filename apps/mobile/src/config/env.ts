/**
 * Environment configuration for the mobile app.
 *
 * Mirrors the web frontend's posture: only safe-public values, provided via
 * EXPO_PUBLIC_* (the Expo equivalent of NEXT_PUBLIC_*). Values are inlined at
 * bundle time by Metro, so a rebuild is required when they change.
 */
import { deriveWebSocketUrl } from "@isol8/shared";

import { ROOT_DOMAIN, WEB_ORIGIN } from "./domain";

export const API_URL =
  process.env.EXPO_PUBLIC_API_URL ?? `https://api-dev.${ROOT_DOMAIN}/api/v1`;

export const WS_URL = deriveWebSocketUrl(
  API_URL,
  process.env.EXPO_PUBLIC_WS_URL,
);

/**
 * Web billing panel — the outbound target for every billing/upgrade CTA.
 * Billing, checkout, and OAuth never happen in-app (D1); always prod web,
 * regardless of which API this build points at.
 */
export const WEB_BILLING_URL = `${WEB_ORIGIN}/settings?panel=billing`;
