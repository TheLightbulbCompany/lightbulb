"use client";

import { useCallback } from "react";
import useSWR from "swr";

import { ApiError, useApi } from "@/lib/api";

/**
 * Third-party integration ("Connection") for the settings panel.
 *
 * Acquisition mode decides the connect UX:
 *   - paste_token  → user pastes a provider API key (Linear, Stripe, …)
 *   - direct_oauth → user is redirected out to the provider (Google, …)
 *
 * `GET /connections` returns the FULL provider catalog already merged
 * with this owner's status, so the UI renders whatever comes back — it
 * never hardcodes a provider list.
 */
export interface Connection {
  provider: string;
  display_name: string;
  acquisition: "paste_token" | "direct_oauth";
  status: "connected" | "disconnected";
  /**
   * Static product flag from the catalog: this provider may only be granted
   * by owners on Bedrock-managed inference. The UI pairs it with the owner's
   * `provider_choice` to render a "switch to Bedrock to unlock" upsell in
   * place of the connect action. The grant endpoints enforce the same rule
   * server-side (a structured `requires_bedrock` 403).
   */
  requires_bedrock?: boolean;
}

interface ConnectionsResponse {
  connections: Connection[];
}

interface AuthorizeResponse {
  authorize_url: string;
}

/**
 * Surfaces the customer-facing Connections API to the settings UI.
 *
 * The three mutating actions (connectPaste / startOAuth / disconnect) are
 * all admin-gated server-side — non-admin org members get a 403. We let
 * the ApiError propagate to the caller (rather than swallowing it) so the
 * UI can show the 403 ("only org admins…") and 422 ("token failed
 * validation…") messages inline. On success we `await mutate()` so the
 * row reflects the new status immediately.
 */
export function useConnections() {
  const api = useApi();

  const { data, error, isLoading, mutate } = useSWR<ConnectionsResponse>(
    "/connections",
    () => api.get("/connections") as Promise<ConnectionsResponse>,
  );

  const connectPaste = useCallback(
    async (provider: string, token: string): Promise<void> => {
      // Paste-key providers only. 422 → token failed validation,
      // 400 → not a paste provider, 502 → apply failed, 403 → non-admin.
      // The ApiError (with .status + .message) bubbles to the caller.
      await api.post(`/connections/${encodeURIComponent(provider)}`, { token });
      await mutate();
    },
    [api, mutate],
  );

  const startOAuth = useCallback(
    async (provider: string): Promise<void> => {
      const r = (await api.get(
        `/connections/${encodeURIComponent(provider)}/authorize`,
      )) as AuthorizeResponse;
      // Hand the browser to the provider's consent screen. It redirects
      // back to /settings/connections?connected=… (or ?error=…), which the
      // page handles on return. No state to clean up here — the full-page
      // navigation discards this React tree.
      window.location.href = r.authorize_url;
    },
    [api],
  );

  const disconnect = useCallback(
    async (provider: string): Promise<void> => {
      await api.del(`/connections/${encodeURIComponent(provider)}`);
      await mutate();
    },
    [api, mutate],
  );

  return {
    connections: data?.connections ?? [],
    isLoading,
    error,
    mutate,
    connectPaste,
    startOAuth,
    disconnect,
  };
}

export { ApiError };
