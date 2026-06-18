"use client";

import { useState } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useApi, BACKEND_URL } from "@/lib/api";
import { ApiError, type Connection } from "@/hooks/useConnections";
import { notifyApiError } from "@/lib/notify";

interface ConnectButtonProps {
  connection: Connection;
  /** Whether the current user may manage connections (org admin). */
  canManage: boolean;
  /**
   * This provider is `requires_bedrock` and the owner is NOT on
   * Bedrock-managed inference, so the connect action is replaced with a
   * "switch to Bedrock to unlock" upsell. The parent passes false for
   * already-connected rows so disconnect stays reachable.
   */
  lockedForBedrock?: boolean;
  connectPaste: (provider: string, token: string) => Promise<void>;
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
// Deep link into the LLM panel's change-provider flow — mirrors the backend's
// `action_href` so the click-through path is identical whether the upsell is
// rendered upfront (locked row) or surfaced from a grant-path 403.
const SWITCH_PROVIDER_HREF = "/chat?panel=llm&action=change-provider";
const BEDROCK_LOCK_FALLBACK =
  "This integration requires Bedrock-managed inference. Switch to Bedrock credits to unlock it.";

// Limited-Use acknowledgment shown before an owner stores their own OAuth app.
// FOUNDER: finalize wording — this is a placeholder, not final legal copy.
const BYO_ACK_COPY =
  "I understand I'm using my own Google app and accept responsibility for its compliance with Google's Limited Use policy."; // FOUNDER: finalize wording

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

/** The structured Bedrock-lock 403's message, or null when `e` is any other error. */
function bedrockLockMessage(e: unknown): string | null {
  if (!(e instanceof ApiError) || e.status !== 403) return null;
  const detail = (e.body as { detail?: unknown } | null)?.detail;
  if (detail && typeof detail === "object" && (detail as { reason?: string }).reason === "requires_bedrock") {
    return (detail as { message?: string }).message ?? BEDROCK_LOCK_FALLBACK;
  }
  return null;
}

/**
 * Maps an ApiError from a connect/disconnect/authorize call to a short,
 * user-facing string. The backend already hands back a useful `detail`
 * via ApiError.message for most cases (e.g. the 422 validation copy); we
 * special-case 403 so non-admins get a clear permissions message — except
 * the Bedrock-lock 403, whose structured detail we surface as the upsell.
 */
function errorMessage(e: unknown): string {
  const lock = bedrockLockMessage(e);
  if (lock) return lock;
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
  lockedForBedrock = false,
  connectPaste,
  startOAuth,
  disconnect,
  refreshConnections,
}: ConnectButtonProps) {
  const { provider, display_name, acquisition, status } = connection;
  const api = useApi();
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // BYO ("use your own app") panel state. Lives only on the locked, not-yet-
  // configured branch; opening it reveals the client_id/secret + acknowledge
  // form. Kept local (no SWR) — a successful PUT hands off to the OAuth flow.
  const [byoOpen, setByoOpen] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      setToken("");
    } catch (e) {
      // Persistent permission / Bedrock-lock 403s stay inline: the lock is
      // already the "Switch to Bedrock" upsell, and a non-admin denial is a
      // standing state a toast would auto-dismiss. Everything else is a
      // transient failure -> toast (with the Report action).
      if (bedrockLockMessage(e) || (e instanceof ApiError && e.status === 403)) {
        setErr(errorMessage(e));
      } else {
        notifyApiError(e, "Couldn't update the connection. Please try again.");
      }
    } finally {
      setBusy(false);
    }
  };

  // Connected → offer Disconnect (admin-gated). Same shape for both
  // acquisition modes.
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
          {busy ? "Disconnecting…" : "Disconnect"}
        </Button>
        {err && (
          <p className="text-xs text-[#b63025]" role="alert">
            {err}
          </p>
        )}
      </div>
    );
  }

  // BYO escape hatch: a locked provider that accepts an owner-supplied OAuth
  // app, where this owner hasn't stored one yet. Offer "Use your own … app" in
  // place of the Bedrock upsell — storing + acknowledging an app lifts the lock
  // server-side, after which the normal Connect (OAuth) flow proceeds. Once
  // `byo_configured` is true the parent's lock is moot (the gate is lifted), so
  // we fall through to the standard branches below.
  if (lockedForBedrock && connection.byo_capable && !connection.byo_configured) {
    const ackId = `byo-ack-${provider}`;
    // Scrub the typed secret + form state. Called on Cancel/close and after a
    // successful save so a typed client secret never persists in React state to
    // re-populate the masked input on reopen.
    const resetByoForm = () => {
      setClientId("");
      setClientSecret("");
      setAcknowledged(false);
    };
    const closeByo = () => {
      resetByoForm();
      setByoOpen(false);
    };
    const saveByoApp = () => {
      if (!clientId.trim() || !clientSecret.trim() || !acknowledged || busy || !canManage) {
        return;
      }
      void run(async () => {
        // Store the owner's app, then hand straight off to OAuth. The PUT
        // lifts the lock server-side, so the redirect that follows authorizes
        // against the freshly-stored client_id.
        await api.put(`/connections/${encodeURIComponent(provider)}/byo-app`, {
          client_id: clientId.trim(),
          client_secret: clientSecret.trim(),
          acknowledge: acknowledged,
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

    if (!byoOpen) {
      return (
        <div className="flex flex-col items-end gap-1">
          <Button
            size="sm"
            variant="outline"
            disabled={!canManage}
            onClick={() => setByoOpen(true)}
            aria-label={`Use your own ${display_name} app`}
          >
            Use your own {display_name} app
          </Button>
          {err && (
            <p className="text-xs text-[#b63025]" role="alert">
              {err}
            </p>
          )}
        </div>
      );
    }

    return (
      <div className="flex w-80 flex-col items-stretch gap-2 rounded-md border border-[#e0dbd0] bg-[#faf8f3] p-3">
        <p className="text-sm font-medium text-[#1c1917]">Use your own {display_name} app</p>
        {connection.byo_setup_url && (
          <Link
            href={connection.byo_setup_url}
            target="_blank"
            rel="noreferrer noopener"
            className="text-xs text-[#2d8a4e] underline underline-offset-2"
          >
            Register an OAuth app →
          </Link>
        )}
        <div className="flex flex-col gap-1">
          <span className="text-xs text-[#8a8578]">Authorized redirect URI to register:</span>
          <code className="break-all rounded bg-white px-2 py-1 text-[11px] text-[#1c1917] border border-[#e0dbd0]">
            {byoRedirectUri(provider)}
          </code>
        </div>
        <label htmlFor={`byo-client-id-${provider}`} className="text-xs text-[#8a8578]">
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
          className="h-9 bg-white"
        />
        <label htmlFor={`byo-client-secret-${provider}`} className="text-xs text-[#8a8578]">
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
          className="h-9 bg-white"
        />
        <label htmlFor={ackId} className="flex items-start gap-2 text-xs text-[#1c1917]">
          <input
            id={ackId}
            type="checkbox"
            checked={acknowledged}
            disabled={busy || !canManage}
            onChange={(e) => setAcknowledged(e.target.checked)}
            className="mt-0.5"
          />
          <span>{BYO_ACK_COPY}</span>
        </label>
        <div className="flex items-center justify-end gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={closeByo}
          >
            Cancel
          </Button>
          <Button
            size="sm"
            disabled={
              busy || !canManage || !clientId.trim() || !clientSecret.trim() || !acknowledged
            }
            onClick={saveByoApp}
            aria-label={`Save your own ${display_name} app`}
          >
            {busy ? "Saving…" : "Save & connect"}
          </Button>
        </div>
        {err && (
          <p className="text-xs text-[#b63025]" role="alert">
            {err}
          </p>
        )}
      </div>
    );
  }

  // Bedrock-gated provider, owner not on Bedrock → swap Connect for an
  // upsell that deep-links to the change-provider flow. Reached only for
  // not-yet-connected rows (the connected branch above wins first), so a
  // provider already linked before a provider switch keeps its Disconnect.
  if (lockedForBedrock) {
    // Switching providers (POST /billing/provider) is org-admin gated, so a
    // non-admin member can't complete it. Show the reason but a disabled
    // control — consistent with how Connect/Disconnect disable here — rather
    // than a live link into a flow they'd be blocked from.
    if (!canManage) {
      return (
        <Button size="sm" variant="outline" disabled aria-label={`Switch to Bedrock to unlock ${display_name}`}>
          Switch to Bedrock
        </Button>
      );
    }
    return (
      <Button asChild size="sm" variant="outline">
        <Link href={SWITCH_PROVIDER_HREF} aria-label={`Switch to Bedrock to unlock ${display_name}`}>
          Switch to Bedrock
        </Link>
      </Button>
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
          {busy ? "Redirecting…" : "Connect"}
        </Button>
        {err && (
          <p className="text-xs text-[#b63025]" role="alert">
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
          {display_name} API token
        </label>
        <Input
          id={inputId}
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={token}
          disabled={busy || !canManage}
          placeholder="Paste API key"
          aria-invalid={err ? true : undefined}
          aria-describedby={err ? errorId : undefined}
          onChange={(e) => setToken(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              submit();
            }
          }}
          className="h-9 w-52 bg-white"
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
      {err && (
        <p id={errorId} className="text-xs text-[#b63025]" role="alert">
          {err}
        </p>
      )}
    </div>
  );
}
