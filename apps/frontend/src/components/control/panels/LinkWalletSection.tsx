"use client";

// The Link wallet card in Settings -> Connections. Admins only, and hidden entirely while the
// feature is off (the status route answers 404) -- so a workspace never sees a dead door.

import { useState } from "react";
import useSWR from "swr";
import { useApi } from "@/lib/api";
import { notifyApiError } from "@/lib/notify";
import { isTauri, openExternalUrl } from "@/lib/externalAuth";
import { EditorialButton } from "@/components/teams/shell";

interface Status {
  connected: boolean;
  configured: boolean;
}

export function LinkWalletSection({ canManage }: { canManage: boolean }) {
  const api = useApi();
  const { data, error, mutate } = useSWR<Status>(
    canManage ? "/company/link-wallet/status" : null,
    (p: string) => api.get(p) as Promise<Status>,
    { shouldRetryOnError: false },
  );
  const [busy, setBusy] = useState(false);

  if (!canManage || error || !data) return null;

  async function connect() {
    setBusy(true);
    try {
      const { authorizeUrl } = (await api.post("/company/link-wallet/connect", {})) as { authorizeUrl: string };
      if (isTauri()) await openExternalUrl(authorizeUrl);
      else window.location.assign(authorizeUrl);
    } catch (e) {
      notifyApiError(e, "Could not start the Link connection.");
    } finally {
      setBusy(false);
    }
  }

  async function disconnect() {
    setBusy(true);
    try {
      await api.post("/company/link-wallet/disconnect", {});
      await mutate();
    } catch (e) {
      notifyApiError(e, "Could not disconnect the Link wallet.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex items-start gap-3 rounded-md border border-[var(--edge)] bg-cream p-4" data-testid="link-wallet">
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div>
          <h3 className="text-sm font-medium text-midnight">Link wallet</h3>
          <p className="text-xs text-[var(--midnight-muted)]">
            {data.connected ? "Connected" : "Not connected"} — lets agents ask to buy things. You approve every purchase
            in Link; Lightbulb asks first too, unless you trust the merchant and have turned off asking before moving
            money. Agents never see a card. If you leave the workspace, the wallet disconnects.
          </p>
        </div>
        {data.connected ? (
          <EditorialButton onClick={disconnect} disabled={busy}>
            Disconnect
          </EditorialButton>
        ) : (
          <EditorialButton onClick={connect} disabled={busy || !data.configured}>
            Connect Link wallet
          </EditorialButton>
        )}
      </div>
    </div>
  );
}
