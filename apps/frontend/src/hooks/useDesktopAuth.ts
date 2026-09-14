"use client";

import { useSignIn, useAuth } from "@clerk/nextjs";
import { useEffect, useRef } from "react";

/**
 * Listens for Clerk sign-in tickets from the Tauri desktop app.
 *
 * When the user authenticates via Google OAuth in the system browser,
 * the desktop-callback page creates a one-time Clerk sign-in token
 * and sends it to the Tauri app via deep link (flickmeon://auth?ticket=...).
 * Tauri emits an "auth:sign-in-ticket" event to the WebView.
 * This hook consumes the ticket to establish a Clerk session in the WebView.
 */
export function useDesktopAuth() {
  const { signIn, setActive } = useSignIn();
  const { isLoaded, isSignedIn, userId } = useAuth();
  const consumingRef = useRef(false);

  useEffect(() => {
    if (!isLoaded) return; // Auth loading and route teardown are not logout.
    const invoke = (window as Window & { __TAURI__?: { core?: { invoke?: (command: string, args: object) => Promise<unknown> } } }).__TAURI__?.core?.invoke;
    if (!invoke) return;
    // Workspace switches deliberately do not rebind native sharing consent.
    void invoke("local_sessions_auth", { humanUserId: isSignedIn ? userId : null }).catch((err: unknown) => {
      console.warn("[desktop-auth] local_sessions_auth failed", err);
    });
  }, [isLoaded, isSignedIn, userId]);

  useEffect(() => {
    // Only run in Tauri desktop app
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const tauri = (window as any).__TAURI__;
    if (!tauri?.event?.listen) return;
    if (isSignedIn) return; // Already signed in, no need

    let unlisten: (() => void) | null = null;

    async function setup() {
      const { listen } = tauri.event;

      unlisten = await listen("auth:sign-in-ticket", async (event: { payload: string }) => {
        const ticket = event.payload;
        if (!ticket || !signIn || consumingRef.current) return;

        consumingRef.current = true;
        console.log("[desktop-auth] Consuming sign-in ticket...");

        try {
          const result = await signIn.create({
            strategy: "ticket",
            ticket,
          });

          if (result.status === "complete" && result.createdSessionId) {
            await setActive({ session: result.createdSessionId });
            console.log("[desktop-auth] Session activated, reloading...");
            window.location.reload();
          } else {
            console.error("[desktop-auth] Unexpected sign-in status:", result.status);
          }
        } catch (err) {
          console.error("[desktop-auth] Failed to consume ticket:", err);
        } finally {
          consumingRef.current = false;
        }
      });
    }

    setup();

    return () => {
      if (unlisten) unlisten();
    };
  }, [signIn, setActive, isSignedIn]);
}
