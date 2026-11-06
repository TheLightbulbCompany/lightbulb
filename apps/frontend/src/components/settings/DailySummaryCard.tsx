"use client";

import { useEffect, useState } from "react";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";
import { Switch } from "@/components/ui/switch";

type UserPrefs = { timezone: string; dailySummary: boolean; awayEmail?: boolean };

// Settings → Profile → "Daily summary": the once-a-day "needs you" digest
// (push + email at 08:00 local). Per human, default on; the email's one-click
// unsubscribe flips the same pref. Beside it, "Email me when I'm away" (awayEmail): a DM,
// @mention or approval still unseen five minutes later, while away, gets one email.
export function DailySummaryCard() {
  const api = useApi();
  const [prefs, setPrefs] = useState<UserPrefs | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .get("/users/me/prefs")
      .then((value) => {
        if (!cancelled) setPrefs(value as UserPrefs);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // `api` identity churns every render; fetch once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (prefs === null) return null;

  const toggle = async (key: "dailySummary" | "awayEmail", next: boolean) => {
    setBusy(true);
    try {
      setPrefs((await api.put("/users/me/prefs", { [key]: next })) as UserPrefs);
    } catch (err) {
      // Unchanged on failure; the checkbox keeps showing the stored state.
      notifyApiError(err, key === "dailySummary" ? "Couldn't save the daily summary setting" : "Couldn't save the email setting");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="settings-card">
        <label className="settings-card-header" style={{ cursor: "pointer" }}>
          <div>
            <div className="settings-card-title">Daily summary</div>
            <div className="settings-card-desc">
              A notification and email at 8am ({prefs.timezone}) when approvals, tasks or mentions are
              waiting on you.
            </div>
          </div>
          <Switch
            aria-label="Daily summary"
            checked={prefs.dailySummary}
            disabled={busy}
            onCheckedChange={(on) => void toggle("dailySummary", on)} />
        </label>
      </div>
      <div className="settings-card">
        <label className="settings-card-header" style={{ cursor: "pointer" }}>
          <div>
            <div className="settings-card-title">Email me when I&apos;m away</div>
            <div className="settings-card-desc">
              One email when a direct message, a mention or an approval waits five minutes and you are not in
              Lightbulb. Never between 10pm and 8am.
            </div>
          </div>
          <Switch
            aria-label="Email me when I'm away"
            checked={prefs.awayEmail !== false}
            disabled={busy}
            onCheckedChange={(on) => void toggle("awayEmail", on)} />
        </label>
      </div>
    </>
  );
}
