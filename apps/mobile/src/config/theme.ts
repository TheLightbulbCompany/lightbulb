/**
 * Shared color tokens for the mobile app (RN has no CSS variables; this is
 * the StyleSheet-era stand-in until D6 revisits NativeWind in Phase 2+).
 * Values mirror the web's cream theme.
 */
export const colors = {
  ink: "#1a1a1a",
  border: "#d5d0c4",
  bubble: "#f0ece2",
  error: "#b3261e",
  errorBg: "#fbe9e7",
  white: "#fff",
} as const;
