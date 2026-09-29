"use client";

import { useCallback, useEffect, useRef } from "react";
import useSWR from "swr";

import { ApiError, useApi } from "@/lib/api";
import { isTauri, openAuthUrl, pollUntil } from "@/lib/externalAuth";

/**
 * Third-party integration ("Connection") for the settings panel.
 *
 * Acquisition mode decides the connect UX:
 *   - paste_token  → user pastes a provider API key (Linear, Stripe, …)
 *   - direct_oauth → user is redirected out to the provider (Google, …)
 *   - aws_sts      → STS assume-role flow (AWS): ExternalId + principal ARN →
 *                    create a cross-account role → paste its ARN (no key)
 *
 * `GET /connections` returns the FULL provider catalog already merged
 * with this owner's status, so the UI renders whatever comes back — it
 * never hardcodes a provider list.
 */
export interface PasteField {
  key: string;
  label: string;
  placeholder?: string | null;
  /** Render masked (a client secret). */
  secret?: boolean;
}

export interface Connection {
  provider: string;
  display_name: string;
  acquisition: "paste_token" | "direct_oauth" | "aws_sts" | "container_login";
  status: "connected" | "disconnected" | "unavailable";
  /** Whether this Hermes runtime can deliver the grant to an agent. */
  runtime_supported?: boolean;
  /**
   * BYO ("bring your own") OAuth app affordance:
   *   - `byo_required`   → this provider has no managed app; the owner MUST
   *                        register their own OAuth app to connect it.
   *   - `byo_configured` → THIS owner already stored + acknowledged one.
   *   - `byo_setup_url`  → where to register one (provider console deep link).
   */
  byo_required?: boolean;
  byo_configured?: boolean;
  byo_setup_url?: string | null;
  /**
   * Where a paste-key provider's token is minted. Distinct from
   * `byo_setup_url`, which deep-links an OAuth-app console — this answers
   * "where do I get the key this box is asking for".
   */
  token_setup_url?: string | null;
  /**
   * Placeholder for the paste box, when the provider packs two values into the
   * one input (Shopify's `<shop>|<token>`). Null for the usual bare-key
   * provider, where the generic placeholder already says everything.
   */
  token_paste_hint?: string | null;
  /**
   * Labelled inputs for a provider whose credential is several values (Shopify:
   * store domain, client id, client secret). The form renders one input per
   * field and posts them as `fields`; the backend joins them. Null = one box.
   */
  paste_fields?: PasteField[] | null;
  /**
   * This owner cannot connect the provider directly — it is gated off their
   * environment and they are not on its owner_allowlist. The row offers
   * Request access (emails support) instead of Connect. Set by the backend's
   * inverse of registry.available_for.
   */
  requestable?: boolean;
  /**
   * The integration's rail — how it reaches the agent once connected:
   *   - "mcp"   → an MCP server; its tools appear in the agent's tool list.
   *   - "cli"   → an authenticated CLI in the agent's shell (gh, aws, gog).
   *               Never appears in the tool list — that's by design, not
   *               a broken connection.
   *   - "skill" → a bundled skill reads the credential.
   */
  actor?: "cli" | "mcp" | "skill" | "nango_proxy";
  /**
   * Inbound face: event labels this provider can fire as an Automations
   * trigger (registry `inbound`, Phase 2). Empty/absent when the provider
   * isn't (yet) wired as a trigger source.
   */
  can_trigger?: string[];
  /**
   * Same registry `inbound` block as `can_trigger`, but carrying event IDS
   * (not just labels) plus the hookup mode — the Add-flow's "A connected
   * app" rung (Phase 2 T6) needs both to submit `POST
   * /automations/from-integration {provider, event_id}` and to drive what
   * comes after: `"guided_paste"` (e.g. Stripe) is the paste-secret flow,
   * `"auto_github"` adds a repository picker and registers the hook
   * server-side. `null`/absent when the provider isn't wired as a trigger
   * source.
   */
  inbound?: {
    webhook_registration: string;
    /**
     * Does a delivery's BODY name which event fired? Multi-select is gated on
     * it: one trigger hands the agent only the payload (the proxy forwards it
     * verbatim and Paperclip's run context carries no headers), so a provider
     * that identifies its event in an HTTP header — GitHub's X-GitHub-Event,
     * Sentry's Sentry-Hook-Resource — stays ONE event per trigger until
     * Isol8AI/isol8#1916 lands. Absent = treat as false.
     */
    event_in_body?: boolean;
    /**
     * `registration` is a per-EVENT override of `webhook_registration`
     * (google: gmail.poll → "poll", gmail.message → "owner_setup" while the
     * watch events inherit the block level). Null/absent = inherit.
     */
    events: {
      id: string;
      label: string;
      registration?: string | null;
      /** Per-event override of `template.starter_prompt` — same precedence
       * the seed applies (event wins). */
      starter_prompt?: string | null;
    }[];
    /**
     * The registry template the seed renders server-side (placeholders
     * `{event_label}` / `{account}`). The create dialog pre-fills an EDITABLE
     * draft from it and sends an override back only when the owner changes it,
     * so an untouched draft still lets the server's own template win.
     */
    template?: { title: string; starter_prompt: string } | null;
  } | null;
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

  // Stops the desktop grant-poll when this hook unmounts — an abandoned wait
  // must not keep issuing /connections GETs for up to the poll timeout.
  const pollAbortRef = useRef<AbortController | null>(null);
  useEffect(() => () => pollAbortRef.current?.abort(), []);

  const connectPaste = useCallback(
    async (provider: string, token: string | Record<string, string>): Promise<void> => {
      // Paste-key providers only. 422 → token failed validation,
      // 400 → not a paste provider, 502 → apply failed, 403 → non-admin.
      // The ApiError (with .status + .message) bubbles to the caller.
      // A paste_fields provider sends its labelled inputs; the server joins them.
      const body = typeof token === "string" ? { token } : { fields: token };
      await api.post(`/connections/${encodeURIComponent(provider)}`, body);
      await mutate();
    },
    [api, mutate],
  );

  const startOAuth = useCallback(
    async (provider: string): Promise<void> => {
      const r = (await api.get(
        `/connections/${encodeURIComponent(provider)}/authorize${isTauri() ? "?return_to=app" : ""}`,
      )) as AuthorizeResponse;
      // Hand the browser to the provider's consent screen. On the web it
      // redirects back to /settings/connections?connected=… (or ?error=…),
      // which forwards to the workspace Connections panel, and
      // the full-page navigation discards this React tree. In the desktop app
      // the consent screen opens in the SYSTEM browser (the webview must not
      // navigate there — Google blocks embedded webviews and the callback
      // would complete in a session the webview never sees), so this tree
      // stays mounted: poll the same /connections status the panel renders
      // until the grant lands, then resolve so the caller's busy state clears.
      // A failed browser open REJECTS here (openAuthUrl throws) so the caller
      // shows the error instead of a waiting state.
      if (await openAuthUrl(r.authorize_url)) {
        const ac = new AbortController();
        pollAbortRef.current = ac;
        await pollUntil(
          async () => {
            const d = await mutate();
            return (
              d?.connections.some(
                (c) => c.provider === provider && c.status === "connected",
              ) ?? false
            );
          },
          { signal: ac.signal },
        );
      }
    },
    [api, mutate],
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
