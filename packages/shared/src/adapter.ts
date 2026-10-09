/**
 * PlatformAdapter is the seam between the framework-neutral core and each
 * client platform. The web app (Next.js + @clerk/nextjs) and the mobile app
 * (Expo + @clerk/expo) each provide one; shared code never imports a
 * platform SDK directly.
 *
 * Notes on `lib` targeting: the shared package compiles against ES2022 with
 * no DOM lib — anything platform-provided (fetch, WebSocket, storage,
 * randomness) must come through this interface or be a cross-platform
 * global (fetch and WebSocket are, on both web and React Native).
 */
export interface PlatformAdapter {
  /**
   * Returns a fresh Clerk bearer token, or null when unauthenticated.
   * Web: useAuth().getToken. Mobile: Clerk Expo's getToken via secure store.
   */
  getToken(): Promise<string | null>;
  /** Backend REST base URL including /api/v1 (NEXT_PUBLIC_* / EXPO_PUBLIC_*). */
  apiUrl: string;
  /**
   * RFC-4122 v4 UUID, lowercase. crypto.randomUUID on web; mobile injects a
   * polyfilled implementation (react-native-get-random-values).
   * Lowercase matters: OpenClaw case-folds session keys (see the 2026-07-01
   * chat-sessions plan) — a mixed-case id alias-merges sessions.
   */
  uuid(): string;
}
