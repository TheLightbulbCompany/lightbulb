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
import { AppState } from "react-native";

/** True when the app is foregrounded and interactive. */
export function isPresent(): boolean {
  return AppState.currentState === "active";
}
