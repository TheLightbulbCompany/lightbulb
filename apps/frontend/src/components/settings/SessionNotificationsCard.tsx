"use client";

import { useState } from "react";
import useSWR from "swr";
import { SESSION_NOTIFICATION_DEFAULTS, type NotifyLevel, type SessionNotifications } from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";
import { Switch } from "@/components/ui/switch";

const PREFS_KEY = "/users/me/prefs";

/** The person's session notification prefs; the defaults until they load (or if they fail). */
export function useSessionNotificationPrefs() {
  const api = useApi();
  const { data, mutate } = useSWR(PREFS_KEY, () => api.get(PREFS_KEY) as Promise<{ sessionNotifications?: SessionNotifications }>);
  const prefs: SessionNotifications = { ...SESSION_NOTIFICATION_DEFAULTS, ...data?.sessionNotifications };
  return { prefs, loaded: data !== undefined, mutate };
}

const KINDS: { key: "waiting" | "finished"; title: string; desc: string }[] = [
  { key: "waiting", title: "Waiting on you", desc: "A session asks for permission or asks you a question." },
  { key: "finished", title: "Finished", desc: "A turn of a minute or more ends." },
];
const LEVELS: { value: NotifyLevel; label: string }[] = [
  { value: "banner", label: "Banner" },
  { value: "badge", label: "Badge only" },
  { value: "off", label: "Off" },
];

// Settings → Profile, beside the daily summary: how your own sessions tell you things. "Badge only"
// counts the session on the Dock and in the tab title with no banner; the phone gets a push only for "Banner".
export function SessionNotificationsCard() {
  const api = useApi();
  const { prefs, loaded, mutate } = useSessionNotificationPrefs();
  const [busy, setBusy] = useState(false);

  const save = async (change: Partial<SessionNotifications>) => {
    setBusy(true);
    try {
      await mutate(await api.put(PREFS_KEY, { sessionNotifications: { ...prefs, ...change } }) as { sessionNotifications: SessionNotifications }, { revalidate: false });
    } catch (err) {
      notifyApiError(err, "Couldn't save the notification setting");
    } finally {
      setBusy(false);
    }
  };

  if (!loaded) return null;
  return (
    <div className="settings-card">
      <div className="settings-card-title">Session notifications</div>
      <div className="settings-card-desc">For your own coding sessions, on this computer, the web and your phone.</div>
      {KINDS.map(({ key, title, desc }) => (
        <label key={key} className="settings-card-header" style={{ marginTop: 12 }}>
          <div>
            <div className="settings-card-title">{title}</div>
            <div className="settings-card-desc">{desc}</div>
          </div>
          <select aria-label={title} value={prefs[key]} disabled={busy}
            onChange={(event) => void save({ [key]: event.target.value as NotifyLevel })}>
            {LEVELS.map((level) => <option key={level.value} value={level.value}>{level.label}</option>)}
          </select>
        </label>
      ))}
      <label className="settings-card-header" style={{ marginTop: 12, cursor: "pointer" }}>
        <div>
          <div className="settings-card-title">Only when Lightbulb isn&apos;t focused</div>
          <div className="settings-card-desc">No banner while you are in Lightbulb on this computer.</div>
        </div>
        <Switch aria-label="Only when Lightbulb isn't focused" checked={prefs.onlyUnfocused} disabled={busy}
          onCheckedChange={(on) => void save({ onlyUnfocused: on })} />
      </label>
      <label className="settings-card-header" style={{ marginTop: 12, cursor: "pointer" }}>
        <div className="settings-card-title">Sound</div>
        <Switch aria-label="Sound" checked={prefs.sound} disabled={busy}
          onCheckedChange={(on) => void save({ sound: on })} />
      </label>
    </div>
  );
}
