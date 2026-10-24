"use client";

import { useState } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useApi, BACKEND_URL } from "@/lib/api";
import { isTauri } from "@/lib/externalAuth";
import { ApiError, type Connection } from "@/hooks/useConnections";
import { AwsConnectCard } from "@/components/settings/AwsConnectCard";
import { AwsChangeAccessCard } from "@/components/settings/AwsChangeAccessCard";
import { notifyApiError } from "@/lib/notify";
import { useSupport } from "@/components/support/SupportProvider";
import { capture } from "@/lib/analytics";

interface ConnectButtonProps {
  connection: Connection;
  /** Whether the current user may manage connections (org admin). */
  canManage: boolean;
  connectPaste: (provider: string, token: string | Record<string, string>) => Promise<void>;
  startOAuth: (provider: string) => Promise<void>;
  disconnect: (provider: string) => Promise<void>;
  /**
   * Re-fetch the connections list (the `useConnections` SWR `mutate`). Called
   * after the BYO-app PUT succeeds so `byo_configured` reflects the server
   * BEFORE the OAuth redirect: if `startOAuth` then fails, the row leaves the
   * BYO panel for the normal Connect path instead of being stuck (the app is
   * already stored server-side, so re-showing the BYO form is wrong).
   */
  refreshConnections?: () => Promise<unknown>;
}

const NON_ADMIN_MSG = "Only organization admins can manage connections.";

/**
 * The OAuth redirect URI an owner must register in their own provider app.
 *
 * Mirrors the backend's `_redirect_uri` (connections.py): the callback leg is
 * `{PUBLIC_API_BASE}/api/v1/oauth/{provider}/callback`. `BACKEND_URL` already
 * carries the `…/api/v1` suffix, so we only append `/oauth/{provider}/callback`.
 * Keeping this in lockstep with the backend matters — Google rejects the OAuth
 * dance if the registered redirect URI doesn't match byte-for-byte.
 */
function byoRedirectUri(provider: string): string {
  return `${BACKEND_URL}/oauth/${provider}/callback`;
}

/**
 * Maps an ApiError from a connect/disconnect/authorize call to a short,
 * user-facing string. The backend already hands back a useful `detail`
 * via ApiError.message for most cases (e.g. the 422 validation copy); we
 * special-case 403 so non-admins get a clear permissions message.
 */
function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return NON_ADMIN_MSG;
    if (e.message && e.message !== `API ${e.status}`) return e.message;
    if (e.status === 422) return "Token failed validation against the provider.";
    if (e.status === 502) return "Could not apply the credential. Try again.";
  }
  return e instanceof Error ? e.message : "Something went wrong.";
}

export function ConnectButton({
  connection,
  canManage,
  connectPaste,
  startOAuth,
  disconnect,
  refreshConnections,
}: ConnectButtonProps) {
  const { provider, display_name, acquisition, status } = connection;
  const api = useApi();
  const { openSupport } = useSupport();
  const [token, setToken] = useState("");
  // Values of a paste_fields provider's labelled inputs, keyed by field key.
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // BYO ("use your own app") panel state. Opening it reveals the
  // client_id/secret form. Kept local (no SWR) — a successful PUT hands off
  // to OAuth.
  const [byoOpen, setByoOpen] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      setToken("");
      setFieldValues({});
    } catch (e) {
      // A non-admin 403 is a standing permission state a toast would
      // auto-dismiss, so keep it inline. Everything else is a transient
      // failure -> toast (with the Report action).
      if (e instanceof ApiError && e.status === 403) {
        setErr(errorMessage(e));
      } else {
        notifyApiError(e, "Couldn't update the connection. Please try again.");
      }
    } finally {
      setBusy(false);
    }
  };

  // The BYO setup affordance is needed when the provider has no managed app and
  // this owner hasn't stored their own one yet. It is reachable from TWO states:
  //   - a not-yet-connected provider — the BYO panel IS the connect path;
  //   - an owner already connected via the old allowlisted/shared-app flow
  //     (status "connected", byo_configured false) — so they can store their own
  //     app and re-auth against it WITHOUT first disconnecting, which would tear
  //     down their live Google connection.
  const needsByoSetup = connection.byo_required && !connection.byo_configured;

  // Web: startOAuth navigates this page away, so the busy state is a moment of
  // "Redirecting…". Desktop (Tauri): the consent screen opens in the system
  // browser and startOAuth stays pending while it polls /connections for the
  // grant — the row flips to Disconnect by itself once it lands.
  const oauthBusyLabel = isTauri() ? "Finish in your browser…" : "Redirecting…";

  // Renders the "Use your own … app" button, or — once opened — the
  // client_id/secret form. Extracted so both the connected and not-connected
  // branches render the identical affordance (no duplication).
  const renderByoSetup = () => {
    // Scrub the typed secret + form state. Called on Cancel/close and after a
    // successful save so a typed client secret never persists in React state to
    // re-populate the masked input on reopen.
    const resetByoForm = () => {
      setClientId("");
      setClientSecret("");
    };
    const closeByo = () => {
      resetByoForm();
      setByoOpen(false);
    };
    const saveByoApp = () => {
      if (!clientId.trim() || !clientSecret.trim() || busy || !canManage) {
        return;
      }
      void run(async () => {
        // Store the owner's app, then hand straight off to OAuth. The PUT lifts
        // the authorize gate server-side, so the redirect that follows
        // authorizes against the freshly-stored client_id — re-pointing an
        // existing connection at the owner's own app when migrating.
        await api.put(`/connections/${encodeURIComponent(provider)}/byo-app`, {
          client_id: clientId.trim(),
          client_secret: clientSecret.trim(),
        });
        // Refresh the connections list BEFORE the OAuth redirect so
        // `byo_configured` reflects the now-stored app. If startOAuth then
        // fails, the row falls through to the normal Connect path instead of
        // staying stuck on the BYO panel (the app is already stored).
        if (refreshConnections) {
          await refreshConnections();
        }
        // Secret is persisted server-side now; scrub it from React state so it
        // can't re-populate the masked input.
        resetByoForm();
        await startOAuth(provider);
      });
    };

    // A stored app means this is an edit/replace (e.g. fixing a wrong client
    // secret that failed the OAuth exchange), not first-time setup.
    const setupLabel = connection.byo_configured
      ? `Edit your ${display_name} app`
      : `Use your own ${display_name} app`;
    if (!byoOpen) {
      return (
        <Button
          size="sm"
          variant="outline"
          disabled={!canManage}
          onClick={() => setByoOpen(true)}
          aria-label={setupLabel}
        >
          {setupLabel}
        </Button>
      );
    }

    return (
      <div className="flex max-h-[70vh] w-80 flex-col items-stretch gap-2 overflow-y-auto rounded-md border border-[var(--edge)] bg-cream p-3">
        <p className="text-sm font-medium text-midnight">{setupLabel}</p>
        {connection.byo_setup_url && (
          <Link
            href={connection.byo_setup_url}
            target="_blank"
            rel="noreferrer noopener"
            className="text-xs text-[var(--midnight-muted)] underline underline-offset-2 hover:text-midnight"
          >
            Register an OAuth app →
          </Link>
        )}
        <ol className="list-decimal pl-4 text-xs text-[var(--midnight-muted)] space-y-1">
          {provider !== "x" && (
            <li>Create an OAuth client ID — application type <strong>Web application</strong>.</li>
          )}
          {provider === "x" && (
            <li>
              Set the app type to a <strong>confidential</strong> client —{" "}
              <strong>Web App</strong>, <strong>Automated App</strong> or{" "}
              <strong>Bot</strong>. Native App and Single Page App are public clients and
              are never issued a client secret, which this connection requires.
            </li>
          )}
          {provider === "x" && (
            <li>
              Set <strong>App permissions</strong> to a level that covers what you want the
              agent to do — reading, posting, and Direct Messages are separate levels.
            </li>
          )}
          <li>Add the authorized redirect URI shown below.</li>
          {provider === "x" && (
            <li>
              Which endpoints actually work depends on your X API access level — if calls
              come back 403, check your plan on X rather than these settings.
            </li>
          )}
          {provider === "x" && (
            <li>
              Paste the <strong>Client ID and Client Secret</strong> here — the OAuth 2.0
              pair, <em>not</em> the API Key and Secret.
            </li>
          )}
          {provider !== "x" && <li>Paste the Client ID and secret here.</li>}
        </ol>

        <div className="flex flex-col gap-1">
          <span className="text-xs text-[var(--midnight-muted)]">Authorized redirect URI to register:</span>
          <code className="break-all rounded-sm bg-cream px-2 py-1 text-caption text-midnight border border-[var(--edge)]">
            {byoRedirectUri(provider)}
          </code>
        </div>

        <label htmlFor={`byo-client-id-${provider}`} className="text-xs text-[var(--midnight-muted)]">
          Client ID
        </label>
        <Input
          id={`byo-client-id-${provider}`}
          type="text"
          autoComplete="off"
          spellCheck={false}
          value={clientId}
          disabled={busy || !canManage}
          placeholder="Client ID"
          onChange={(e) => setClientId(e.target.value)}
          className="h-9 bg-cream"
        />
        <label htmlFor={`byo-client-secret-${provider}`} className="text-xs text-[var(--midnight-muted)]">
          Client secret
        </label>
        <Input
          id={`byo-client-secret-${provider}`}
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={clientSecret}
          disabled={busy || !canManage}
          placeholder="Client secret"
          onChange={(e) => setClientSecret(e.target.value)}
          className="h-9 bg-cream"
        />
        <div className="flex items-center justify-end gap-2">
          <Button size="sm" variant="outline" disabled={busy} onClick={closeByo}>
            Cancel
          </Button>
          <Button
            size="sm"
            disabled={busy || !canManage || !clientId.trim() || !clientSecret.trim()}
            onClick={saveByoApp}
            aria-label={`Save your own ${display_name} app`}
          >
            {busy ? "Saving…" : "Save & connect"}
          </Button>
        </div>
      </div>
    );
  };

  // Gated off this owner: the authorize leg would 404, so never offer Connect.
  // Request access emails support with the owner's account already enriched
  // server-side — which is exactly what an operator needs to add them as a
  // provider tester and to owner_allowlist. Checked before every
  // acquisition-specific branch (paste_token / direct_oauth / aws_sts) so it
  // wins for every acquisition type.
  if (connection.requestable) {
    return (
      <Button
        size="sm"
        variant="outline"
        // Same admin gate as every other action in this row. Without it a
        // non-admin member could file a request against an org whose admin never
        // asked for it — and since no request record is stored, the admin would
        // have no way to see it happened.
        disabled={!canManage}
        title={canManage ? undefined : NON_ADMIN_MSG}
        onClick={() => {
          capture("integration_access_requested", { provider: connection.provider });
          openSupport({
            category: "Integration request",
            message: `I'd like to use ${connection.display_name}.`,
          });
        }}
        aria-label={`Request access to ${connection.display_name}`}
      >
        Request access
      </Button>
    );
  }

  if (status === "unavailable") return null;

  // Connected → Disconnect, plus the BYO setup affordance when this owner still
  // needs to store their own app (non-destructive migration off the old
  // shared-app connection — no disconnect-first required).
  if (status === "connected") {
    return (
      <div className="flex flex-col items-end gap-1">
        <Button
          variant="outline"
          size="sm"
          disabled={busy || !canManage}
          onClick={() => run(() => disconnect(provider))}
          aria-label={`Disconnect ${display_name}`}
        >
          {busy ? "Working…" : "Disconnect"}
        </Button>
        {/* AWS's tier is set by which managed policy the connector role
            carries, not stored by Lightbulb — so it can be changed in place
            (update the CFN stack param, no reconnect). Offer that on the
            connected AWS row without touching the Disconnect path. */}
        {acquisition === "aws_sts" && (
          <AwsChangeAccessCard connection={connection} canManage={canManage} />
        )}
        {needsByoSetup && renderByoSetup()}
        {err && (
          <p className="text-xs text-[var(--err)]" role="alert">
            {err}
          </p>
        )}
      </div>
    );
  }

  // BYO connect path for a not-yet-connected provider: storing + acknowledging
  // the app, then the normal OAuth redirect, connects it.
  if (needsByoSetup) {
    return (
      <div className="flex flex-col items-end gap-1">
        {renderByoSetup()}
        {err && (
          <p className="text-xs text-[var(--err)]" role="alert">
            {err}
          </p>
        )}
      </div>
    );
  }

  // A byo_required provider whose app IS stored but the connection is down
  // (e.g. a wrong client secret failed the OAuth exchange). Offer BOTH a retry
  // against the stored app AND an edit affordance to overwrite a bad app — else
  // the row would fall to a plain Connect that reuses the bad secret on every
  // retry, with no UI to fix the stored credentials.
  if (connection.byo_required && connection.byo_configured) {
    return (
      <div className="flex flex-col items-end gap-1">
        {!byoOpen && (
          <Button
            size="sm"
            disabled={busy || !canManage}
            onClick={() => run(() => startOAuth(provider))}
            aria-label={`Connect ${display_name}`}
          >
            {busy ? oauthBusyLabel : "Connect"}
          </Button>
        )}
        {renderByoSetup()}
        {err && (
          <p className="text-xs text-[var(--err)]" role="alert">
            {err}
          </p>
        )}
      </div>
    );
  }

  // AWS uses an STS assume-role flow, not a pasted key: a dedicated card walks
  // the owner through ExternalId + principal ARN → create role → paste ARN.
  if (acquisition === "aws_sts") {
    return (
      <AwsConnectCard
        connection={connection}
        canManage={canManage}
        refreshConnections={refreshConnections}
      />
    );
  }

  // OAuth providers: a single Connect button that redirects out.
  if (acquisition === "direct_oauth") {
    return (
      <div className="flex flex-col items-end gap-1">
        <Button
          size="sm"
          disabled={busy || !canManage}
          onClick={() => run(() => startOAuth(provider))}
          aria-label={`Connect ${display_name}`}
        >
          {busy ? oauthBusyLabel : "Connect"}
        </Button>
        {err && (
          <p className="text-xs text-[var(--err)]" role="alert">
            {err}
          </p>
        )}
      </div>
    );
  }

  // Multi-value paste providers (Shopify): one labelled input per field. The
  // server joins them, so nobody has to type a packed "<a>|<b>:<c>" string.
  const pasteFields = connection.paste_fields;
  if (pasteFields?.length) {
    const complete = pasteFields.every((f) => fieldValues[f.key]?.trim());
    const submitFields = () => {
      if (!complete || busy || !canManage) return;
      void run(() => connectPaste(provider, fieldValues));
    };
    return (
      <div className="flex w-64 flex-col items-stretch gap-1">
        {pasteFields.map((f) => (
          <div key={f.key} className="flex flex-col gap-0.5">
            <label
              htmlFor={`connection-${provider}-${f.key}`}
              className="text-xs text-[var(--midnight-muted)]"
            >
              {f.label}
            </label>
            <Input
              id={`connection-${provider}-${f.key}`}
              type={f.secret ? "password" : "text"}
              autoComplete="off"
              spellCheck={false}
              value={fieldValues[f.key] ?? ""}
              disabled={busy || !canManage}
              placeholder={f.placeholder ?? undefined}
              onChange={(e) => setFieldValues((v) => ({ ...v, [f.key]: e.target.value }))}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submitFields();
                }
              }}
              aria-invalid={err ? true : undefined}
              aria-describedby={err ? `connection-error-${provider}` : undefined}
              className="h-9 bg-cream"
            />
          </div>
        ))}
        <div className="flex items-center justify-between gap-2">
          {connection.token_setup_url ? (
            <a
              href={connection.token_setup_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-[var(--midnight-muted)] underline underline-offset-2 hover:text-midnight"
            >
              Where to find these
            </a>
          ) : (
            <span />
          )}
          <Button
            size="sm"
            disabled={busy || !canManage || !complete}
            onClick={submitFields}
            aria-label={`Connect ${display_name}`}
          >
            {busy ? "Connecting…" : "Connect"}
          </Button>
        </div>
        {err && (
          <p id={`connection-error-${provider}`} className="text-xs text-[var(--err)]" role="alert">
            {err}
          </p>
        )}
      </div>
    );
  }

  // Paste-token providers: an inline token input + Connect.
  const inputId = `connection-token-${provider}`;
  const errorId = `connection-error-${provider}`;
  const submit = () => {
    if (!token.trim() || busy || !canManage) return;
    void run(() => connectPaste(provider, token.trim()));
  };

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2">
        <label htmlFor={inputId} className="sr-only">
          {connection.token_paste_hint
            ? `${display_name} credentials, combined as ${connection.token_paste_hint}`
            : `${display_name} API token`}
        </label>
        <Input
          id={inputId}
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={token}
          disabled={busy || !canManage}
          placeholder={
            // A provider that packs two values into this one box (Shopify's shop
            // domain + token) is undiscoverable without showing the shape — its
            // own docs have never heard of our packing convention.
            connection.token_paste_hint || "Paste API key"
          }
          aria-invalid={err ? true : undefined}
          aria-describedby={err ? errorId : undefined}
          onChange={(e) => setToken(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submit();
            }
          }}
          className="h-9 w-52 bg-cream"
        />
        <Button
          size="sm"
          disabled={busy || !canManage || !token.trim()}
          onClick={submit}
          aria-label={`Connect ${display_name}`}
        >
          {busy ? "Connecting…" : "Connect"}
        </Button>
      </div>
      {/* The placeholder is gone on the first keystroke and the box is too
          narrow to show the whole shape anyway, so a field wanting two values has
          to state the format somewhere that stays put. */}
      {connection.token_paste_hint && (
        <p className="text-xs text-[var(--midnight-muted)]">
          Paste both values as{" "}
          <code className="font-mono">{connection.token_paste_hint}</code>
        </p>
      )}
      {/* An OAuth row needs no instructions — the provider's own consent screen
          is the flow. An API-key row is a blank box, so without this the owner
          has to go find the right settings page themselves. */}
      {connection.token_setup_url && (
        <a
          href={connection.token_setup_url}
          target="_blank"
          rel="noopener noreferrer"
          className="text-xs text-[var(--midnight-muted)] underline underline-offset-2 hover:text-midnight"
        >
          Where do I find this?
        </a>
      )}
      {err && (
        <p id={errorId} className="text-xs text-[var(--err)]" role="alert">
          {err}
        </p>
      )}
    </div>
  );
}
