"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useBilling } from "@/hooks/useBilling";
import { useApi } from "@/lib/api";

type Install = { team_id: string; team_name: string; agent_id: string; status: string; installed_at: string };

const PAID_NOTE = "Slack needs a paid plan. Upgrade to connect a workspace.";

/** The platform Slack app's card: one click installs Lightbulb into a
 * workspace and binds it to this agent. The BYO "create your own app"
 * wizard stays available below it as the advanced path. */
export function SlackInstallCard({ agentId }: { agentId: string }) {
  const api = useApi();
  // Phase 1 forwards Slack traffic for paid owners only, and the backend
  // refuses the start for anyone else — don't offer a button that ends in silence.
  // If the billing read itself fails, fall through to the button and let the
  // backend's 402 decide — never mislabel a paid owner as unpaid on a blip.
  const { account: billing, isLoading: billingLoading, error: billingError } = useBilling();
  const paid = billing?.is_subscribed === true || Boolean(billingError);
  const [installs, setInstalls] = useState<Install[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = (await api.get("/slack/install")) as { installs: Install[] };
      setInstalls(res.installs.filter((i) => i.status === "active"));
      setError(null);
    } catch {
      setError("Couldn't load Slack status.");
    }
  }, [api]);

  useEffect(() => {
    void load();
    const flag = new URLSearchParams(window.location.search).get("slack");
    if (flag === "connected") setNote("Slack connected — your agents are restarting (about a minute).");
    else if (flag?.startsWith("error:")) setNote(`Slack connection failed (${flag.slice(6)}).`);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- read the URL once on mount, not on every load() identity change
  }, []);

  const start = async () => {
    setBusy(true);
    try {
      const res = (await api.get(`/slack/oauth/start?agent_id=${encodeURIComponent(agentId)}`)) as { url: string };
      window.location.assign(res.url);
    } catch (e) {
      const status = (e as { status?: number } | null)?.status;
      setNote(status === 402 ? PAID_NOTE : "Slack request failed. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async (teamId: string) => {
    setBusy(true);
    try {
      await api.del(`/slack/install/${encodeURIComponent(teamId)}`);
      await load();
    } catch {
      setNote("Slack request failed. Try again.");
    } finally {
      setBusy(false);
    }
  };

  if (error && installs === null) {
    return (
      <div className="flex items-center gap-2 text-xs">
        <span className="text-err">{error}</span>
        <Button variant="outline" size="sm" onClick={() => load()}>
          Retry
        </Button>
      </div>
    );
  }

  if (installs === null) return null;
  const mine = installs.filter((i) => i.agent_id === agentId);

  return (
    <div className="space-y-2">
      {note && <p className="text-xs text-[var(--midnight-muted)]">{note}</p>}
      {mine.length === 0 ? (
        billingLoading ? null : paid ? (
          <Button variant="outline" size="sm" onClick={start} disabled={busy}>
            Add to Slack
          </Button>
        ) : (
          <p className="text-xs text-[var(--midnight-muted)]">{PAID_NOTE}</p>
        )
      ) : (
        mine.map((i) => (
          <div key={i.team_id} className="flex items-center gap-2 text-sm">
            <CheckCircle2 size={16} className="text-ok" />
            <span>Connected to {i.team_name || i.team_id}</span>
            <div className="flex-1" />
            <Button variant="ghost" size="sm" onClick={() => disconnect(i.team_id)} disabled={busy}>
              Disconnect
            </Button>
          </div>
        ))
      )}
    </div>
  );
}
