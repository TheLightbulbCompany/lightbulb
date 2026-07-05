/**
 * @isol8/shared — framework-neutral core for Isol8 clients.
 *
 * Phase 0: adapter contract only. Phase 1 moves in the API client core
 * (ApiError, deriveWebSocketUrl, authenticated fetch), the gateway
 * WebSocket protocol machine, and the data hooks — see
 * docs/superpowers/plans/2026-07-05-mobile-app-expo-ios.md.
 */
export type { PlatformAdapter } from "./adapter";
