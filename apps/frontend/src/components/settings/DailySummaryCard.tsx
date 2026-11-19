"use client";

import { useEffect, useState } from "react";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";
import { Switch } from "@/components/ui/switch";
import { SettingRow } from "./SettingRow";

type UserPrefs = { timezone: string; dailySummary: boolean; awayEmail?: boolean };

// Settings → Profile → "Daily summary": the once-a-day "needs you" digest
// (push + email at 08:00 local). Per human, default on; the email's one-click
// unsubscribe flips the same pref. Beside it, "Email me when I'm away" (awayEmail): a DM,
// @mention or approval still unseen five minutes later, while away, gets one email.
// Both switches use the row pattern (W13-10).
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
      <SettingRow label="Daily summary" htmlFor="pref-daily-summary"
        line={`A notification and email at 8am (${prefs.timezone}) when approvals, threads or mentions are waiting on you.`}>
        <Switch id="pref-daily-summary" checked={prefs.dailySummary} disabled={busy} onCheckedChange={(on) => void toggle("dailySummary", on)} />
      </SettingRow>
      <SettingRow label="Email me when I'm away" htmlFor="pref-away-email"
        line="One email when a message, a mention or an approval waits five minutes while you're away. Never 10pm to 8am.">
        <Switch id="pref-away-email" checked={prefs.awayEmail !== false} disabled={busy} onCheckedChange={(on) => void toggle("awayEmail", on)} />
      </SettingRow>
    </>
  );
}
