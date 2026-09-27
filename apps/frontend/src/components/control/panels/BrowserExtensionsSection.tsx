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
  source: "webstore" | "upload";
  addedAt: string | null;
}

interface ExtensionsResponse {
  extensions: BrowserExtension[];
  catalog: { slug: string; name: string }[];
}

const KEY = "/browser/extensions";

/** What a change did to the running browser: extensions load only when a session starts. */
const restartNote = (restarting?: boolean) => restarting
  ? "The browser is restarting to apply the change; sign-ins are saved first."
  : "It applies the next time the browser starts.";

/**
 * Chrome extensions the workspace's shared browser starts with. Any member adds one by pasting
 * its Chrome Web Store link (or uploading a ZIP); the same thing happens from inside the shared
 * browser, where a Web Store page offers "Add to the shared browser". A running browser that no
 * agent is using restarts to load the change.
 *
 * Mounted in Connections and in the Browser panel's Extensions popover (`onChanged` lets the
 * panel poll at once, so its "Restarting the browser…" state shows without waiting).
 */
export function BrowserExtensionsSection({ onChanged }: { onChanged?: () => void } = {}) {
  const api = useApi();
  const { data, error, isLoading, mutate } = useSWR<ExtensionsResponse>(KEY, () => api.get(KEY) as Promise<ExtensionsResponse>);
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useState<{ text: string; failed?: boolean } | null>(null);
  const [link, setLink] = useState("");
  const file = useRef<HTMLInputElement>(null);
  const self = useRef<HTMLElement>(null);
  useEffect(() => {
    if (window.location.hash === "#browser-extensions") self.current?.scrollIntoView?.({ block: "start" });
  }, []);

  const extensions = data?.extensions ?? [];
  const installed = new Set(extensions.map((e) => e.slug));
  const offers = (data?.catalog ?? []).filter((c) => !installed.has(c.slug));

  const run = async (id: string, action: () => Promise<unknown>, done: string, fallback: string) => {
    setBusy(id);
    setStatus(null);
    try {
      const result = await action() as { name?: string; restarting?: boolean } | undefined;
      await mutate();
      onChanged?.();
      setStatus({ text: `${done.replace("{name}", result?.name ?? "It")} ${restartNote(result?.restarting)}` });
      return true;
    } catch (e) {
      setStatus({ text: e instanceof Error && e.message ? e.message : fallback, failed: true });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const addLink = async () => {
    if (!link.trim()) return;
    const ok = await run("webstore", () => api.post(`${KEY}/webstore`, { idOrUrl: link.trim() }), "Added {name}.", "Couldn't add that extension.");
    if (ok) setLink("");
  };

  const onFile = (picked: File | undefined) => {
    if (!picked) return;
    const form = new FormData();
    form.append("file", picked);
    void run("upload", () => api.upload(KEY, form), "Added {name}.", "Couldn't add that extension.");
    if (file.current) file.current.value = "";
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
            Chrome extensions the shared browser starts with, for everyone in the workspace. Paste a
            Chrome Web Store link, or open the extension&apos;s store page in the shared browser and
            choose &ldquo;Add to the shared browser&rdquo;.
          </p>
        </div>
      </header>

      <div className="space-y-3 px-5 py-4">
        {error
          ? <p role="alert" className="text-sm text-[var(--err)]">Couldn&apos;t load browser extensions.</p>
          : isLoading
            ? <p role="status" className="text-sm text-[var(--midnight-muted)]">Loading…</p>
            : extensions.length === 0
              ? <p className="text-sm text-[var(--midnight-muted)]">No extensions yet.</p>
              : <ul className="divide-y divide-[var(--edge)] text-sm">
                {extensions.map((ext) => <li key={ext.slug} className="flex items-center justify-between gap-3 py-2">
                  <span className="min-w-0">
                    <span className="text-[var(--midnight)]">{ext.name}</span>
                    <span className="ml-2 text-[var(--midnight-muted)]">
                      {ext.version && `v${ext.version} · `}{ext.addedAt ? `added ${timeAgo(ext.addedAt)}` : ""}
                    </span>
                  </span>
                  <Button size="sm" variant="ghost" disabled={busy !== null} aria-label={`Remove ${ext.name}`}
                    onClick={() => void run(ext.slug, () => api.del(`${KEY}/${encodeURIComponent(ext.slug)}`), `Removed ${ext.name}.`, "Couldn't remove that extension.")}>
                    Remove
                  </Button>
                </li>)}
              </ul>}
        <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); void addLink(); }}>
          <input value={link} onChange={(e) => setLink(e.target.value)} disabled={busy !== null}
            aria-label="Chrome Web Store link or extension id"
            placeholder="Add from the Web Store: paste a link or id"
            className="h-8 min-w-0 flex-1 rounded-md border border-[var(--edge)] bg-[var(--cream-deep)] px-3 text-sm text-[var(--midnight)] placeholder:text-[var(--midnight-faint)]" />
          <Button size="sm" type="submit" disabled={busy !== null || !link.trim()}>
            {busy === "webstore" ? "Adding…" : "Add"}
          </Button>
        </form>
        <div className="flex flex-wrap gap-2">
          {offers.map((c) => <Button key={c.slug} size="sm" variant="outline" disabled={busy !== null}
            onClick={() => void run(c.slug, () => api.post(`${KEY}/catalog/${encodeURIComponent(c.slug)}`, {}), "Added {name}.", "Couldn't add that extension.")}>
            {busy === c.slug ? "Adding…" : `Add ${c.name}`}
          </Button>)}
          <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => file.current?.click()}>
            {busy === "upload" ? "Uploading…" : "Upload ZIP"}
          </Button>
          <input ref={file} type="file" accept=".zip,application/zip" className="hidden" aria-label="Extension ZIP"
            onChange={(e) => onFile(e.target.files?.[0])} />
        </div>
        {status && <p role={status.failed ? "alert" : "status"}
          className={`text-sm ${status.failed ? "text-[var(--err)]" : "text-[var(--midnight-muted)]"}`}>{status.text}</p>}
      </div>
    </section>
  );
}
