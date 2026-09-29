"use client";

import { useCallback, useEffect, useRef } from "react";
import useSWR from "swr";

import { ApiError, useApi } from "@/lib/api";
import { openAuthUrl, pollUntil, returnsToApp } from "@/lib/externalAuth";

/**
 * A single connected Google account. One owner can connect several Google
 * accounts; each is identified by its `email`. `connected_at` is the ISO
 * timestamp the OAuth grant landed.
 */
export interface GoogleAccount {
  email: string;
  connected_at: string;
}

/**
 * `GET /google/accounts` returns the owner's connected accounts plus the
 * per-agent assignment map. `assignments` is keyed by agent id; the value is
 * the account email that agent acts as. An agent absent from the map uses the
 * owner's default account (the first one connected), so the UI treats a
 * missing key as "Default".
 */
interface GoogleAccountsResponse {
  accounts: GoogleAccount[];
  assignments: Record<string, string>;
}

interface AssignmentsResponse {
  assignments: Record<string, string>;
}

interface AuthorizeResponse {
  authorize_url: string;
}

/**
 * Surfaces the Google multi-account API to the Integrations UI.
 *
 * The mutating actions (disconnect / assign / unassign) are admin-gated
 * server-side — non-admin org members get a 403. We let the ApiError
 * propagate so the caller can render the 403 message inline. Each mutation
 * `await mutate()`s so the accounts list + assignment dropdowns reflect the
 * new state immediately. `connectAnother` does a full-page redirect to the
 * provider, which discards this React tree, so it has nothing to mutate.
 */
export function useGoogleAccounts() {
  const api = useApi();

  const { data, error, isLoading, mutate } = useSWR<GoogleAccountsResponse>(
    "/google/accounts",
    () => api.get("/google/accounts") as Promise<GoogleAccountsResponse>,
  );

  // Stops the desktop grant-poll when this hook unmounts — an abandoned wait
  // must not keep issuing /google/accounts GETs for up to the poll timeout.
  const pollAbortRef = useRef<AbortController | null>(null);
  useEffect(() => () => pollAbortRef.current?.abort(), []);

  const disconnectAccount = useCallback(
    async (email: string): Promise<void> => {
      // Disconnect ONE account by email. The shared /connections provider row
      // only flips to disconnected once the LAST Google account is gone; that
      // cache is owned by useConnections and revalidated by its own surface,
      // so here we revalidate only this hook's accounts/assignments view.
      await api.del(`/connections/google?account=${encodeURIComponent(email)}`);
      await mutate();
    },
    [api, mutate],
  );

  const assign = useCallback(
    async (email: string, agentId: string): Promise<void> => {
      const r = (await api.post(
        `/google/accounts/${encodeURIComponent(email)}/assign`,
        { agent_id: agentId },
      )) as AssignmentsResponse;
      // The endpoint returns the full, post-mutation assignment map, so apply
      // it directly rather than refetching — avoids a flash of stale rows.
      await mutate(
        (prev) => (prev ? { ...prev, assignments: r.assignments } : prev),
        { revalidate: false },
      );
    },
    [api, mutate],
  );

  const unassign = useCallback(
    async (agentId: string): Promise<void> => {
      const r = (await api.del(
        `/google/accounts/assign/${encodeURIComponent(agentId)}`,
      )) as AssignmentsResponse;
      await mutate(
        (prev) => (prev ? { ...prev, assignments: r.assignments } : prev),
        { revalidate: false },
      );
    },
    [api, mutate],
  );

  const connectAnother = useCallback(async (): Promise<void> => {
    const r = (await api.get(
      `/connections/google/authorize${returnsToApp() ? "?return_to=app" : ""}`,
    )) as AuthorizeResponse;
    // Hand the browser to Google's consent screen. Adding an account is
    // additive on the backend — it never replaces an existing grant. On the
    // web the provider redirects back to /settings/connections — which
    // forwards to the workspace Connections panel — and the full-page
    // navigation discards this tree. In the desktop app the consent
    // screen opens in the SYSTEM browser (Google blocks embedded webviews),
    // so this tree stays mounted: poll the accounts list until the new grant
    // lands, then resolve so the caller's busy state clears. A failed browser
    // open REJECTS here (openAuthUrl throws) so the caller shows the error
    // instead of a waiting state.
    const before = data?.accounts.length ?? 0;
    if (await openAuthUrl(r.authorize_url)) {
      const ac = new AbortController();
      pollAbortRef.current = ac;
      // ponytail: length-growth can't detect a re-consent of an already
      // connected account (a no-op grant) — that wait self-clears at the
      // poll timeout; compare account emails if it ever matters.
      await pollUntil(
        async () => ((await mutate())?.accounts.length ?? 0) > before,
        { signal: ac.signal },
      );
    }
  }, [api, data, mutate]);

  return {
    accounts: data?.accounts ?? [],
    assignments: data?.assignments ?? {},
    isLoading,
    error,
    mutate,
    disconnectAccount,
    assign,
    unassign,
    connectAnother,
  };
}

export { ApiError };
