"use client";

import { useDesktopAuth } from "@/hooks/useDesktopAuth";

/**
 * Invisible component that listens for Clerk sign-in tickets
 * from the Tauri desktop app. Must be inside ClerkProvider.
 */
export function DesktopAuthListener() {
  useDesktopAuth();
  return null;
}
