/**
 * Presence for mobile — is a human plausibly using the app right now?
 *
 * On the web this needed care (a lit screen reports "visible" with nobody
 * there — the 5am display-wake that started the whole wake-on-intent work).
 * On mobile it is far cleaner: iOS never brings an app to the FOREGROUND on
 * its own — background refresh and push wake-ups keep AppState at "background"
 * — so `AppState.currentState === "active"` genuinely means the user switched
 * to or opened the app. And an idle foreground app auto-locks within a minute
 * or two, which backgrounds it. So presence = active, full stop; there is no
 * "visible but unattended" state to defend against the way there is on desktop.
 *
 * The gateway provider uses AppState transitions directly to park/unpark; the
 * status read no longer takes a wake hint (it has no side effects).
 */
import { useAuth } from "@clerk/expo";
import { createApiClient } from "@lightbulb/shared";
import { useEffect, useMemo, useRef, useState } from "react";
import { AppState } from "react-native";

import { makeAdapter } from "./adapter";

/** True when the app is foregrounded and interactive. */
export function isPresent(): boolean {
  return AppState.currentState === "active";
}

const HEARTBEAT_MS = 45_000;
const SNAPSHOT_MS = 60_000;

/**
 * Who in this workspace is online — the web's `usePresence`, on the phone's
 * clock (AppState, not tab visibility). `enabled` only gates whether this
 * hook runs at all; the heartbeat itself is separately gated by `isPresent()`
 * so an always-`true` caller (e.g. the Chats tab) still stops beating the
 * moment the app is backgrounded.
 */
export function usePresence(enabled: boolean): ReadonlySet<string> {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  const [online, setOnline] = useState<ReadonlySet<string>>(new Set());

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    const beat = () => {
      if (isPresent()) void api.post("/messaging/presence", {}).catch(() => {});
    };
    const snapshot = () =>
      void (api.get("/messaging/presence") as Promise<{ online?: string[] }>)
        .then((body) => {
          if (alive) setOnline(new Set(body.online ?? []));
        })
        .catch(() => {});
    beat();
    snapshot();
    const beats = setInterval(beat, HEARTBEAT_MS);
    const snaps = setInterval(snapshot, SNAPSHOT_MS);
    return () => {
      alive = false;
      clearInterval(beats);
      clearInterval(snaps);
    };
  }, [enabled, api]);

  return online;
}
