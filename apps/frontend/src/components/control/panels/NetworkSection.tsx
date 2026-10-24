"use client";

import { useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { ApiError, useApi } from "@/lib/api";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";

const KEY = "/settings/network";
const INSTALL = "curl -fsSL https://github.com/TheLightbulbCompany/cli/releases/latest/download/install.sh | sh";
const RETRY_MS = 10_000;
const MAX_TRIES = 12;

/** The workspace is still starting: a 504, or the server's RUNTIME_UNAVAILABLE code. */
function isStarting(e: unknown): boolean {
  if (!(e instanceof ApiError)) return false;
  const b = e.body as { code?: string; detail?: { code?: string } } | null;
  return e.status === 504 || b?.code === "RUNTIME_UNAVAILABLE" || b?.detail?.code === "RUNTIME_UNAVAILABLE";
}

/**
 * Connect a computer to the workspace's peer-to-peer network. The one-time line lives only in
 * component state (`fresh`): it is never refetched, so a remount shows the button again.
 */
export function NetworkSection() {
  const api = useApi();
  const isAdmin = useIsWorkspaceAdmin();
  const { data } = useSWR<{ connected: boolean }>(KEY, () => api.get(KEY) as Promise<{ connected: boolean }>);
  const [fresh, setFresh] = useState<{ line: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [starting, setStarting] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const getLine = async () => {
    setBusy(true);
    setFailure(null);
    try {
      for (let i = 0; i < MAX_TRIES; i++) {
        try {
          const made = await api.post(`${KEY}/invite`, {}) as { line: string };
          setFresh({ line: made.line });
          return;
        } catch (e) {
          if (!isStarting(e)) throw e;
          setStarting(true);
          if (i < MAX_TRIES - 1) await new Promise((r) => setTimeout(r, RETRY_MS));
        }
      }
      setFailure("Your workspace is still starting. Try again in a few minutes.");
    } catch (e) {
      setFailure(e instanceof Error && e.message ? e.message : "Couldn't get a line.");
    } finally {
      setStarting(false);
      setBusy(false);
    }
  };

  const reconnect = async () => {
    if (!window.confirm("Every computer will need a new line.")) return;
    setFailure(null);
    try {
      await api.post(`${KEY}/reconnect`, {});
      setFresh(null);
    } catch (e) {
      setFailure(e instanceof Error && e.message ? e.message : "Couldn't reconnect the workspace.");
    }
  };

  return (
    <section id="network" aria-labelledby="network-heading"
      className="overflow-hidden rounded-md border border-[var(--edge)] bg-cream">
      <header className="border-b border-[var(--edge)] px-5 py-4">
        <h3 id="network-heading" className="font-editorial-serif text-base font-semibold text-[var(--midnight)]">Connect your computer</h3>
        {data?.connected && <p className="mt-1 text-sm text-[var(--midnight-muted)]">Your workspace is connected.</p>}
      </header>
      <div className="space-y-3 px-5 py-4 text-sm text-[var(--midnight)]">
        <p>Install Lightbulb on your Mac or Linux computer:</p>
        <code className="block break-all rounded-md border border-[var(--edge)] p-2 font-mono text-xs">{INSTALL}</code>
        {fresh ? (
          <div role="status" className="space-y-2">
            <p>Paste this in Terminal. It works once, for 24 hours.</p>
            <code className="block break-all rounded-md border border-[var(--edge)] p-2 font-mono text-xs">{fresh.line}</code>
            <Button size="sm" variant="outline" onClick={() => {
              void navigator.clipboard?.writeText(fresh.line);
              setCopied(true);
            }}>{copied ? "Copied" : "Copy"}</Button>
          </div>
        ) : (
          <Button size="sm" disabled={busy} onClick={() => void getLine()}>Get my line</Button>
        )}
        {starting && <p role="status" className="text-[var(--midnight-muted)]">Starting your workspace… this can take a minute.</p>}
        {failure && <p role="alert" className="text-[var(--err)]">{failure}</p>}
        {isAdmin && (
          <button type="button" className="text-xs text-[var(--link)] underline" onClick={() => void reconnect()}>
            Reconnect workspace
          </button>
        )}
      </div>
    </section>
  );
}
