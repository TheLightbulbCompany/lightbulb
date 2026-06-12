"use client";

import { useState } from "react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, type Connection } from "@/hooks/useConnections";

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
}

const NON_ADMIN_MSG = "Only organization admins can manage connections.";
// Deep link into the LLM panel's change-provider flow — mirrors the backend's
// `action_href` so the click-through path is identical whether the upsell is
// rendered upfront (locked row) or surfaced from a grant-path 403.
const SWITCH_PROVIDER_HREF = "/chat?panel=llm&action=change-provider";
const BEDROCK_LOCK_FALLBACK =
  "This integration requires Bedrock-managed inference. Switch to Bedrock credits to unlock it.";

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
}: ConnectButtonProps) {
  const { provider, display_name, acquisition, status } = connection;
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      setToken("");
    } catch (e) {
      setErr(errorMessage(e));
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

  // Bedrock-gated provider, owner not on Bedrock → swap Connect for an
  // upsell that deep-links to the change-provider flow. Reached only for
  // not-yet-connected rows (the connected branch above wins first), so a
  // provider already linked before a provider switch keeps its Disconnect.
  if (lockedForBedrock) {
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
