"use client";

import { useMemo, useRef, useState } from "react";
import Link from "next/link";

import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useApi } from "@/lib/api";
import type { ImportConfirmResult, ImportPreview } from "@/lib/marketplace/types";

type Phase = "pick" | "uploading" | "scanning" | "preview" | "confirming";

function errMessage(e: unknown): string {
  const err = e as Error & { body?: { detail?: unknown } };
  const d = err?.body?.detail;
  if (typeof d === "string" && d) return d;
  if (d && typeof d === "object" && (d as { error?: string }).error === "ultra_required") return "This import needs Ultra Pro.";
  return err?.message || "Import failed";
}

function mb(bytes: number) { return `${(bytes / 1024 / 1024).toFixed(1)} MB`; }

export function ImportBackupDialog({ open, onOpenChange, onImported }: {
  open: boolean; onOpenChange: (open: boolean) => void; onImported: (agentId: string) => void;
}) {
  const api = useApi();
  const inputRef = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<Phase>("pick");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [agents, setAgents] = useState<Set<string>>(new Set());
  const [skills, setSkills] = useState<Set<string>>(new Set());
  const [mcps, setMcps] = useState<Set<string>>(new Set());
  const [plugins, setPlugins] = useState<Set<string>>(new Set());
  const [memory, setMemory] = useState(true);
  const [cron, setCron] = useState(true);
  const [history, setHistory] = useState(false);

  function reset() {
    setPhase("pick"); setPreview(null); setError(null);
    setAgents(new Set()); setSkills(new Set()); setMcps(new Set()); setPlugins(new Set());
    setMemory(true); setCron(true); setHistory(false);
    if (inputRef.current) inputRef.current.value = "";
  }

  async function handleFile(file: File | undefined) {
    if (!file) return;
    setError(null); setPhase("uploading");
    try {
      const presign = (await api.post("/marketplace/import/presign", { filename: file.name, size_bytes: file.size })) as { upload_url: string; key: string };
      const put = await fetch(presign.upload_url, { method: "PUT", body: file, headers: { "content-type": "application/gzip" } });
      if (!put.ok) throw new Error(`upload failed (${put.status})`);
      setPhase("scanning");
      const p = (await api.post("/marketplace/import", { key: presign.key })) as ImportPreview;
      setPreview(p);
      setAgents(new Set(p.agents.map((a) => a.id)));
      setSkills(new Set(p.skills)); setMcps(new Set(p.mcp_servers)); setPlugins(new Set(p.plugins.map((x) => x.id)));
      setPhase("preview");
    } catch (e) { setError(errMessage(e)); setPhase("pick"); }
  }

  const needsUltra = useMemo(() => !!preview && preview.plan !== "ultra_pro" && preview.existing_agents + agents.size > preview.standard_max_agents, [preview, agents]);
  const anyHistory = !!preview && preview.agents.some((a) => agents.has(a.id) && a.has_history);

  async function confirm() {
    if (!preview) return;
    setError(null); setPhase("confirming");
    try {
      const r = (await api.post(`/marketplace/import/${preview.import_id}/confirm`, {
        agent_ids: [...agents], skills: [...skills], mcp_servers: [...mcps], include_memory: memory, include_cron: cron,
        plugins: [...plugins], include_history: history && anyHistory,
      })) as ImportConfirmResult;
      const first = r.agents.find((a) => a.ok && a.agent_id);
      const failed = r.agents.filter((a) => !a.ok);
      if (!first) throw new Error(failed[0]?.error || "nothing was imported");
      onImported(first.agent_id as string);
      onOpenChange(false); reset();
    } catch (e) { setError(errMessage(e)); setPhase("preview"); }
  }

  function cancel() {
    if (preview) void api.del(`/marketplace/import/${preview.import_id}`).catch(() => {});
    reset(); onOpenChange(false);
  }

  const toggle = (set: Set<string>, setter: (s: Set<string>) => void, id: string) => {
    const next = new Set(set);
    if (next.has(id)) next.delete(id); else next.add(id);
    setter(next);
  };
  const busy = phase === "uploading" || phase === "scanning" || phase === "confirming";

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? onOpenChange(true) : cancel())}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Import from an OpenClaw backup</DialogTitle>
          <DialogDescription>
            Run <code className="font-mono text-[12px]">openclaw backup create</code> on your machine and upload the
            <code className="font-mono text-[12px]"> .tar.gz</code>. Pick what comes along; nothing is minted until you confirm.
          </DialogDescription>
        </DialogHeader>

        {(phase === "pick" || phase === "uploading" || phase === "scanning") && (
          <label className="block rounded-2xl border border-dashed border-[var(--edge)] p-5 text-center text-[13px]">
            <input ref={inputRef} type="file" accept=".tar.gz,.tgz,application/gzip,application/x-gzip" aria-label="Choose a backup archive"
              disabled={busy} onChange={(e) => void handleFile(e.target.files?.[0])} className="block w-full text-[13px]" />
            {phase === "uploading" && <p className="mt-2 text-[#5b5b5b]">Uploading…</p>}
            {phase === "scanning" && <p className="mt-2 text-[#5b5b5b]">Scanning the archive…</p>}
          </label>
        )}

        {preview && (phase === "preview" || phase === "confirming") && (
          <div className="max-h-[60vh] space-y-3 overflow-y-auto text-[13px]">
            <div className="rounded-2xl border border-[var(--edge)] bg-white p-3">
              <p className="text-[15px] font-medium text-[var(--ink)]">{preview.name}</p>
              {preview.description && <p className="text-[#5b5b5b]">{preview.description}</p>}
              <p className="mt-1 text-[#5b5b5b]">{preview.file_count} files · {preview.cron_jobs} scheduled jobs</p>
            </div>

            <fieldset>
              <legend className="text-[12px] text-[#5b5b5b]">Agents</legend>
              {preview.agents.map((a) => (
                <label key={a.id} className="flex items-center gap-2 py-0.5">
                  <input type="checkbox" aria-label={a.name} checked={agents.has(a.id)} disabled={busy} onChange={() => toggle(agents, setAgents, a.id)} />
                  {/* Name shares this element's direct text node with the " · " separator (not just nested
                      inside the grey <span>) so its resolved text is never exactly equal to a.name alone —
                      testing-library's getByText("Quill") only reads an element's own direct text-node
                      children, and the backup's overall name commonly matches its main agent's name. */}
                  <span>{a.name} · <span className="text-[#5b5b5b]">{a.id}{a.has_history ? ` · history ${mb(a.history_bytes)}` : ""}</span></span>
                </label>
              ))}
            </fieldset>

            {needsUltra && (
              <p role="alert" className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-amber-900">
                You have {preview.existing_agents} agents; importing {agents.size} more brings you to {preview.existing_agents + agents.size}. The standard
                plan runs up to {preview.standard_max_agents} agents per workspace. <Link href="/settings" className="underline">Upgrade to Ultra Pro</Link> in Settings, or pick fewer agents.
              </p>
            )}

            {preview.skills.length > 0 && (
              <fieldset><legend className="text-[12px] text-[#5b5b5b]">Skills</legend>
                {preview.skills.map((s) => (<label key={s} className="mr-3 inline-flex items-center gap-1"><input type="checkbox" aria-label={s} checked={skills.has(s)} disabled={busy} onChange={() => toggle(skills, setSkills, s)} />{s}</label>))}
              </fieldset>
            )}
            {preview.mcp_servers.length > 0 && (
              <fieldset><legend className="text-[12px] text-[#5b5b5b]">MCP servers (secrets are never imported — you re-enter them)</legend>
                {preview.mcp_servers.map((s) => (<label key={s} className="mr-3 inline-flex items-center gap-1"><input type="checkbox" aria-label={s} checked={mcps.has(s)} disabled={busy} onChange={() => toggle(mcps, setMcps, s)} />{s}</label>))}
              </fieldset>
            )}
            {preview.plugins.length > 0 && (
              <fieldset><legend className="text-[12px] text-[#5b5b5b]">Plugins (your workspace restarts to load them)</legend>
                {preview.plugins.map((p) => (<label key={p.id} className="flex items-center gap-2 py-0.5"><input type="checkbox" aria-label={p.id} checked={plugins.has(p.id)} disabled={busy} onChange={() => toggle(plugins, setPlugins, p.id)} />{p.spec}{p.has_dependencies && <span className="text-[#5b5b5b]"> · needs deps installed in the container</span>}</label>))}
              </fieldset>
            )}
            <label className="flex items-center gap-2"><input type="checkbox" checked={memory} disabled={busy} onChange={() => setMemory(!memory)} />Memory logs (MEMORY.md, memory/)</label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={cron} disabled={busy} onChange={() => setCron(!cron)} />Scheduled jobs</label>
            {anyHistory && (
              <label className="flex items-center gap-2"><input type="checkbox" aria-label="Bring conversation history" checked={history} disabled={busy} onChange={() => setHistory(!history)} />Bring conversation history (auth tokens are stripped; applies on the next workspace restart)</label>
            )}

            {preview.warnings.length > 0 && (
              <ul className="list-disc space-y-1 rounded-2xl border border-amber-200 bg-amber-50 p-3 pl-7 text-amber-900">{preview.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            )}
            {preview.skipped_big_files.length > 0 && <p className="text-[#5b5b5b]">Skipped {preview.skipped_big_files.length} file(s) over 512 MB.</p>}
          </div>
        )}

        {error && <p role="alert" className="rounded-xl bg-red-50 p-2 text-[13px] text-red-700">{error}</p>}

        <DialogFooter>
          <button type="button" onClick={cancel} disabled={busy} className="rounded-full px-4 py-2 text-[13px] text-[#5b5b5b] hover:bg-black/5">Cancel</button>
          {preview && (
            <button type="button" onClick={() => void confirm()} disabled={busy || agents.size === 0 || needsUltra}
              className="rounded-full bg-[var(--ink)] px-4 py-2 text-[13px] font-medium text-white disabled:opacity-50">
              {phase === "confirming" ? "Adding…" : "Add to my team"}
            </button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
