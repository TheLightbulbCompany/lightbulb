"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, type Connection } from "@/hooks/useConnections";

interface ConnectButtonProps {
  connection: Connection;
  /** Whether the current user may manage connections (org admin). */
  canManage: boolean;
  connectPaste: (provider: string, token: string) => Promise<void>;
  startOAuth: (provider: string) => Promise<void>;
  disconnect: (provider: string) => Promise<void>;
}

const NON_ADMIN_MSG = "Only organization admins can manage connections.";

/**
 * Maps an ApiError from a connect/disconnect/authorize call to a short,
 * user-facing string. The backend already hands back a useful `detail`
 * via ApiError.message for most cases (e.g. the 422 validation copy); we
 * special-case 403 so non-admins get a clear permissions message instead
 * of a raw FastAPI detail.
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
