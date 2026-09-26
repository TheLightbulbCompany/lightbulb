"use client";

import { useEffect, useRef, useState } from "react";
import useSWR from "swr";

import { Button } from "@/components/ui/button";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { useApi } from "@/lib/api";

interface BrowserExtension {
  slug: string;
  name: string;
  version: string;
  source: "catalog" | "upload";
  addedAt: string | null;
}

interface ExtensionsResponse {
  extensions: BrowserExtension[];
  catalog: { slug: string; name: string; version: string }[];
}

const KEY = "/browser/extensions";

/**
 * Chrome extensions the workspace's shared browser starts with. Agents cannot install from
 * the Web Store into the managed browser, so an admin adds them here. Extensions are fixed
 * when a session starts: a change applies to the next one, hence "Restart browser now".
 *
 * Mounted in Connections (and opened from the Browser panel's menu). Admin-only on the server.
 */
export function BrowserExtensionsSection({ canManage }: { canManage: boolean }) {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<ExtensionsResponse>(
    canManage ? KEY : null,
    () => api.get(KEY) as Promise<ExtensionsResponse>,
  );
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useState<{ text: string; failed?: boolean } | null>(null);
  const [changed, setChanged] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  const self = useRef<HTMLElement>(null);
  useEffect(() => {
    if (window.location.hash === "#browser-extensions") self.current?.scrollIntoView?.({ block: "start" });
  }, []);

  const extensions = data?.extensions ?? [];
  const installed = new Set(extensions.map((e) => e.slug));
  const offers = (data?.catalog ?? []).filter((c) => !installed.has(c.slug));

  const run = async (id: string, action: () => Promise<unknown>, fallback: string) => {
    setBusy(id);
    setStatus(null);
    try {
      await action();
      await mutate();
      setChanged(true);
    } catch (e) {
      setStatus({ text: e instanceof Error && e.message ? e.message : fallback, failed: true });
    } finally {
      setBusy(null);
    }
  };

  const onFile = (picked: File | undefined) => {
    if (!picked) return;
    const form = new FormData();
    form.append("file", picked);
    void run("upload", () => api.upload(KEY, form), "Couldn't add that extension.");
    if (file.current) file.current.value = "";
  };

  const restart = async () => {
    setBusy("restart");
    setStatus(null);
    try {
      // Stop saves sign-ins first; it 409s when nothing is running, which is fine here.
      await api.post("/browser/stop", {}).catch(() => undefined);
      await api.post("/browser/start", {});
      setChanged(false);
      setStatus({ text: "Browser restarted with the current extensions." });
    } catch (e) {
      setStatus({ text: e instanceof Error && e.message ? e.message : "Couldn't restart the browser.", failed: true });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section ref={self} id="browser-extensions" aria-labelledby="browser-extensions-heading"
      className="overflow-hidden rounded-md border border-[var(--edge)] bg-cream">
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 border-b border-[var(--edge)] px-5 py-4">
        <div className="min-w-0 flex-1">
          <h3 id="browser-extensions-heading" className="font-editorial-serif text-base font-semibold text-[var(--midnight)]">
            Browser extensions
          </h3>
          <p className="mt-1 max-w-prose text-sm leading-relaxed text-[var(--midnight-muted)]">
            Chrome extensions the shared browser starts with. Changes apply the next time the
            browser starts.
          </p>
        </div>
        {canManage && <div className="flex flex-wrap gap-2">
          {offers.map((c) => <Button key={c.slug} size="sm" disabled={busy !== null}
            onClick={() => void run(c.slug, () => api.post(`${KEY}/catalog/${encodeURIComponent(c.slug)}`, {}), "Couldn't add that extension.")}>
            {busy === c.slug ? "Adding…" : `Add ${c.name}`}
          </Button>)}
          <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => file.current?.click()}>
            {busy === "upload" ? "Uploading…" : "Upload ZIP"}
          </Button>
          <input ref={file} type="file" accept=".zip,application/zip" className="hidden" aria-label="Extension ZIP"
            onChange={(e) => onFile(e.target.files?.[0])} />
        </div>}
      </header>

      {!canManage
        ? <p className="px-5 py-4 text-sm text-[var(--midnight-muted)]" role="status">
          Only organization admins can manage browser extensions.
        </p>
        : <div className="px-5 py-4 space-y-3">
          {error
            ? <p role="alert" className="text-sm text-[var(--err)]">Couldn&apos;t load browser extensions.</p>
            : isLoading
              ? <p role="status" className="text-sm text-[var(--midnight-muted)]">Loading…</p>
              : extensions.length === 0
                ? <p className="text-sm text-[var(--midnight-muted)]">
                  No extensions. Upload an unpacked Manifest V3 extension as a ZIP with manifest.json at the top.
                </p>
                : <ul className="divide-y divide-[var(--edge)] text-sm">
                  {extensions.map((ext) => <li key={ext.slug} className="flex items-center justify-between gap-3 py-2">
                    <span className="min-w-0">
                      <span className="text-[var(--midnight)]">{ext.name}</span>
                      <span className="ml-2 text-[var(--midnight-muted)]">
                        {ext.version && `v${ext.version} · `}{ext.addedAt ? `added ${timeAgo(ext.addedAt)}` : ""}
                      </span>
                    </span>
                    <Button size="sm" variant="ghost" disabled={busy !== null} aria-label={`Remove ${ext.name}`}
                      onClick={() => void run(ext.slug, () => api.del(`${KEY}/${encodeURIComponent(ext.slug)}`), "Couldn't remove that extension.")}>
                      Remove
                    </Button>
                  </li>)}
                </ul>}
          {changed && <div className="flex flex-wrap items-center gap-3 rounded-md border border-[var(--edge)] p-3 text-sm">
            <span className="flex-1 text-[var(--midnight-muted)]">
              A running browser keeps its old extensions until it restarts. Restarting saves sign-ins first.
            </span>
            <Button size="sm" disabled={busy !== null} onClick={() => void restart()}>
              {busy === "restart" ? "Restarting…" : "Restart browser now"}
            </Button>
          </div>}
          {status && <p role={status.failed ? "alert" : "status"}
            className={`text-sm ${status.failed ? "text-[var(--err)]" : "text-[var(--midnight-muted)]"}`}>{status.text}</p>}
        </div>}
    </section>
  );
}
