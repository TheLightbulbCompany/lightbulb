"use client";

import { useEffect, useState } from "react";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";

type UserPrefs = { timezone: string; dailySummary: boolean };

// Settings → Profile → "Daily summary": the once-a-day "needs you" digest
// (push + email at 08:00 local). Per human, default on; the email's one-click
// unsubscribe flips the same pref.
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

  const toggle = async (next: boolean) => {
    setBusy(true);
    try {
      setPrefs((await api.put("/users/me/prefs", { dailySummary: next })) as UserPrefs);
    } catch (err) {
      // Unchanged on failure; the checkbox keeps showing the stored state.
      notifyApiError(err, "Couldn't save the daily summary setting");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="settings-card">
      <label className="settings-card-header" style={{ cursor: "pointer" }}>
        <div>
          <div className="settings-card-title">Daily summary</div>
          <div className="settings-card-desc">
            A notification and email at 8am ({prefs.timezone}) when approvals, assigned issues or mentions are
            waiting on you.
          </div>
        </div>
        <input
          type="checkbox"
          aria-label="Daily summary"
          checked={prefs.dailySummary}
          disabled={busy}
          onChange={(event) => void toggle(event.target.checked)}
        />
      </label>
    </div>
  );
}
