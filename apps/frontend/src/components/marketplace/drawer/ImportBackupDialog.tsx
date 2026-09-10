"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { CompanyAgent } from "@/components/teams/shared/types";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useApi } from "@/lib/api";
import type { ImportConfirmResult, ImportPreview } from "@/lib/marketplace/types";

type Phase = "pick" | "uploading" | "scanning" | "preview" | "confirming";

function errMessage(e: unknown): string {
  const err = e as Error & { body?: { detail?: unknown } };
  const d = err?.body?.detail;
  if (typeof d === "string" && d) return d;
  if (d && typeof d === "object" && (d as { error?: string }).error === "ultra_required") return "This import needs Ultra.";
  return err?.message || "Import failed";
}

function mb(bytes: number) { return `${(bytes / 1024 / 1024).toFixed(1)} MB`; }

// Same local bridge-detection pattern as components/desktop/DesktopFileAccess:
// the Tauri webview injects window.__TAURI__; a regular browser never has it.
type TauriInvoke = <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;

function getTauriInvoke(): TauriInvoke | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const invoke = (window as any).__TAURI__?.core?.invoke as TauriInvoke | undefined;
  return invoke ?? null;
}

export function ImportBackupDialog({ open, onOpenChange, onImported }: {
  open: boolean; onOpenChange: (open: boolean) => void; onImported: (agentId: string) => void;
}) {
  const api = useApi();
  const inputRef = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<Phase>("pick");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [partial, setPartial] = useState<{ okIds: string[]; failed: ImportConfirmResult["agents"] } | null>(null);
  const [agents, setAgents] = useState<Set<string>>(new Set());
  const [skills, setSkills] = useState<Set<string>>(new Set());
  const [mcps, setMcps] = useState<Set<string>>(new Set());
  const [plugins, setPlugins] = useState<Set<string>>(new Set());
  const [memory, setMemory] = useState(true);
  const [cron, setCron] = useState(true);
  const [history, setHistory] = useState(false);
  // Claude-context branch: which roster agent receives the staged content,
  // which memory collections come along, and the per-item results after
  // confirm. `skills` above is reused as the claude skill selection.
  const [target, setTarget] = useState("");
  const [claudeCols, setClaudeCols] = useState<Set<string>>(new Set());
  const [claudeItems, setClaudeItems] = useState<NonNullable<ImportConfirmResult["claude_items"]> | null>(null);
  const [claudeWarnings, setClaudeWarnings] = useState<string[]>([]);
  // Bumped by reset(): a native export resolving after the dialog closed
  // compares against this and drops its result instead of re-driving upload.
  const generationRef = useRef(0);
  // Set post-mount (SSR-safe, same as DesktopFileAccess) — non-null only in
  // the Tauri webview, where the one-click "Import from this Mac" renders.
  const [tauriInvoke, setTauriInvoke] = useState<TauriInvoke | null>(null);
  useEffect(() => { setTauriInvoke(() => getTauriInvoke()); }, []);

  // Null key until a claude preview exists — SWR's canonical "skip the
  // request" signal, so backup imports never hit the Teams BFF.
  const { read } = useTeamsApi();
  const roster = read<{ agents: CompanyAgent[] }>(preview?.kind === "claude" ? "/agents" : null);
  const rosterAgents = roster.data?.agents ?? [];

  function reset() {
    generationRef.current += 1;
    setPhase("pick"); setPreview(null); setError(null); setPartial(null);
    setAgents(new Set()); setSkills(new Set()); setMcps(new Set()); setPlugins(new Set());
    setMemory(true); setCron(true); setHistory(false);
    setTarget(""); setClaudeCols(new Set()); setClaudeItems(null); setClaudeWarnings([]);
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
      setSkills(new Set(p.kind === "claude" ? p.claude?.skills ?? [] : p.skills));
      setMcps(new Set(p.mcp_servers)); setPlugins(new Set(p.plugins.map((x) => x.id)));
      setClaudeCols(new Set(p.claude?.collections.map((c) => c.id) ?? []));
      setPhase("preview");
    } catch (e) {
      setError(errMessage(e)); setPhase("pick");
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  // Desktop one-click path: the Rust command packages the allowlisted
  // ~/.claude slice; the result feeds the SAME handleFile flow the file
  // input uses. Its Err strings are user-facing and render verbatim.
  async function importFromThisMac() {
    if (!tauriInvoke) return;
    setError(null); setPhase("uploading");
    const generation = generationRef.current;
    try {
      const exported = await tauriInvoke<{ base64: string; file_count: number; total_bytes: number }>("export_claude_context");
      if (generation !== generationRef.current) return; // dialog closed while exporting — drop the result
      const bytes = Uint8Array.from(atob(exported.base64), (c) => c.charCodeAt(0));
      await handleFile(new File([bytes], "claude-code.tar.gz", { type: "application/gzip" }));
    } catch (e) {
      if (generation !== generationRef.current) return;
      setError(e instanceof Error ? e.message : String(e)); setPhase("pick");
    }
  }

  const needsUltra = useMemo(() => !!preview && preview.plan !== "ultra_pro" && preview.existing_agents + agents.size > preview.standard_max_agents, [preview, agents]);
  const anyHistory = !!preview && preview.agents.some((a) => agents.has(a.id) && a.has_history);
  const claude = preview?.kind === "claude" ? (preview.claude ?? null) : null;
  // The claude preview carries its flags under `claude.safety_flags` (the
  // top-level list stays at its default); same shape, same renderer.
  const safetyFlags = claude ? claude.safety_flags : preview?.safety_flags ?? [];

  async function confirm() {
    if (!preview) return;
    setError(null); setPartial(null); setPhase("confirming");
    try {
      if (preview.kind === "claude") {
        // No agents are minted here — content lands on an existing roster
        // agent, so the backup path's "successful agents[] entry" contract
        // doesn't apply. 2xx with claude_items IS success. onImported fires
        // from the Done button (like continueWithPartial) — the parent closes
        // the dialog inside onImported, which would hide these results.
        const r = (await api.post(`/marketplace/import/${preview.import_id}/confirm`, {
          target_agent_id: target, claude_collections: [...claudeCols], claude_skills: [...skills],
        })) as ImportConfirmResult;
        setClaudeItems(r.claude_items ?? []);
        setClaudeWarnings(r.warnings ?? []);
        setPhase("preview");
        return;
      }
      const r = (await api.post(`/marketplace/import/${preview.import_id}/confirm`, {
        agent_ids: [...agents], skills: [...skills], mcp_servers: [...mcps], include_memory: memory, include_cron: cron,
        plugins: [...plugins], include_history: history && anyHistory,
      })) as ImportConfirmResult;
      const ok = r.agents.filter((a) => a.ok && a.agent_id);
      const failed = r.agents.filter((a) => !a.ok);
      if (!ok.length) throw new Error(failed[0]?.error || "nothing was imported");
      if (failed.length > 0) {
        // Some agents landed, some didn't — the staged import is kept (the
        // backend only sweeps it when every agent succeeds), so don't close
        // the dialog out from under a fixable failure. Let the seller retry
        // (re-runs confirm against the same staging) or continue with what
        // did land.
        setPartial({ okIds: ok.map((a) => a.agent_id as string), failed });
        setPhase("preview");
        return;
      }
      onImported(ok[0].agent_id as string);
      onOpenChange(false); reset();
    } catch (e) { setError(errMessage(e)); setPhase("preview"); }
  }

  function continueWithPartial() {
    if (!partial) return;
    onImported(partial.okIds[0]);
    onOpenChange(false); reset();
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
            Run <code className="font-mono text-xs">openclaw backup create</code> on your machine and upload the
            <code className="font-mono text-xs"> .tar.gz</code>. Pick what comes along; nothing is minted until you confirm.
            Or import your Claude Code context{tauriInvoke ? " with the button below — or, from a terminal:" : ": run"}{" "}
            <code className="font-mono text-xs">cd ~ && tar czf claude-code.tar.gz .claude/CLAUDE.md .claude/projects/*/memory .claude/skills</code>{" "}
            (include whichever exist) and upload it.
          </DialogDescription>
        </DialogHeader>

        {(phase === "pick" || phase === "uploading" || phase === "scanning") && tauriInvoke && (
          <button type="button" onClick={() => void importFromThisMac()} disabled={busy}
            className="w-full rounded-full bg-[var(--midnight)] px-4 py-2 text-ui font-medium text-[var(--cream)] disabled:opacity-50">
            Import from this Mac
          </button>
        )}
        {(phase === "pick" || phase === "uploading" || phase === "scanning") && (
          <label className="block rounded-xl border border-dashed border-[var(--edge)] p-5 text-center text-ui">
            <input ref={inputRef} type="file" accept=".tar.gz,.tgz,application/gzip,application/x-gzip" aria-label="Choose a backup archive"
              disabled={busy} onChange={(e) => void handleFile(e.target.files?.[0])} className="block w-full text-ui" />
            {phase === "uploading" && <p className="mt-2 text-muted-foreground">Uploading…</p>}
            {phase === "scanning" && <p className="mt-2 text-muted-foreground">Scanning the archive…</p>}
          </label>
        )}

        {preview && (phase === "preview" || phase === "confirming") && (
          <div className="max-h-[60vh] space-y-3 overflow-y-auto text-ui">
            <div className="rounded-xl border border-[var(--edge)] bg-card p-3">
              <p className="text-sm font-medium text-[var(--midnight)]">{preview.name}</p>
              {preview.description && <p className="text-muted-foreground">{preview.description}</p>}
              {claude ? (
                <p className="mt-1 text-muted-foreground">
                  {claude.collections.length} project memories · {claude.collections.reduce((n, c) => n + c.file_count, 0)} memory files · {claude.skills.length} skill{claude.skills.length === 1 ? "" : "s"}
                </p>
              ) : (
                <p className="mt-1 text-muted-foreground">{preview.file_count} files · {preview.cron_jobs} scheduled jobs</p>
              )}
            </div>

            {claude && (
              <>
                <label className="block">
                  <span className="text-xs text-muted-foreground">Apply to agent</span>
                  <select aria-label="Target agent" value={target} disabled={busy || !!claudeItems}
                    onChange={(e) => setTarget(e.target.value)}
                    className="mt-1 w-full rounded-lg border border-[var(--edge)] bg-cream-deep px-3 py-2 text-ui text-[var(--midnight)] disabled:opacity-50">
                    <option value="">Choose an agent…</option>
                    {rosterAgents.map((a) => (
                      <option key={a.id} value={a.id}>{a.name}{a.role === "ceo" ? " — CEO" : a.title ? ` — ${a.title}` : ""}</option>
                    ))}
                  </select>
                </label>
                {roster.error != null && <p className="text-xs text-[var(--err)]">Couldn&apos;t load your team — reopen the dialog to retry.</p>}
                {claude.collections.length > 0 && (
                  <fieldset><legend className="text-xs text-muted-foreground">Project memories</legend>
                    {claude.collections.map((c) => (
                      <label key={c.id} className="flex items-center gap-2 py-0.5">
                        <input type="checkbox" aria-label={c.label} checked={claudeCols.has(c.id)} disabled={busy || !!claudeItems} onChange={() => toggle(claudeCols, setClaudeCols, c.id)} />
                        <span>{c.label} · <span className="text-muted-foreground">{c.file_count} file{c.file_count === 1 ? "" : "s"}</span></span>
                      </label>
                    ))}
                  </fieldset>
                )}
                {claude.skills.length > 0 && (
                  <fieldset><legend className="text-xs text-muted-foreground">Skills</legend>
                    {claude.skills.map((s) => (<label key={s} className="mr-3 inline-flex items-center gap-1"><input type="checkbox" aria-label={s} checked={skills.has(s)} disabled={busy || !!claudeItems} onChange={() => toggle(skills, setSkills, s)} />{s}</label>))}
                  </fieldset>
                )}
                {claude.has_user_memory && <p>Global CLAUDE.md → appended to the agent&apos;s USER.md</p>}
                {Object.keys(claude.skipped).length > 0 && (
                  <p className="text-muted-foreground">Skipped: {Object.entries(claude.skipped).map(([k, v]) => `${v} ${k}`).join(" · ")}</p>
                )}
                {claudeItems && (
                  <div className="rounded-xl border border-[var(--edge)] bg-card p-3">
                    <p className="font-medium text-[var(--midnight)]">Imported</p>
                    <ul className="mt-1 space-y-0.5">
                      {claudeItems.map((i) => (
                        <li key={i.item} className="font-mono text-caption text-muted-foreground">{i.item} — {i.status === "applied" ? "applied" : "already exists, skipped"}</li>
                      ))}
                    </ul>
                    {claudeWarnings.length > 0 && (
                      <ul className="mt-2 list-disc space-y-1 rounded-xl border border-[var(--warn)] p-2 pl-6 text-[var(--warn)]">
                        {claudeWarnings.map((w) => <li key={w}>{w}</li>)}
                      </ul>
                    )}
                  </div>
                )}
              </>
            )}

            {!claude && (<>
            <fieldset>
              <legend className="text-xs text-muted-foreground">Agents</legend>
              {preview.agents.map((a) => (
                <label key={a.id} className="flex items-center gap-2 py-0.5">
                  <input type="checkbox" aria-label={a.name} checked={agents.has(a.id)} disabled={busy} onChange={() => toggle(agents, setAgents, a.id)} />
                  {/* Name shares this element's direct text node with the " · " separator (not just nested
                      inside the grey <span>) so its resolved text is never exactly equal to a.name alone —
                      testing-library's getByText("Quill") only reads an element's own direct text-node
                      children, and the backup's overall name commonly matches its main agent's name. */}
                  <span>{a.name} · <span className="text-muted-foreground">{a.id}{a.has_history ? ` · history ${mb(a.history_bytes)}` : ""}</span></span>
                </label>
              ))}
            </fieldset>

            {needsUltra && (
              <p role="alert" className="rounded-xl border border-[var(--warn)] p-3 text-[var(--warn)]">
                You have {preview.existing_agents} agents; importing {agents.size} more brings you to {preview.existing_agents + agents.size}. The standard
                plan runs up to {preview.standard_max_agents} agents per workspace. <Link href="/settings" className="underline">Upgrade to Ultra</Link> in Settings, or pick fewer agents.
              </p>
            )}

            {preview.skills.length > 0 && (
              <fieldset><legend className="text-xs text-muted-foreground">Skills</legend>
                {preview.skills.map((s) => (<label key={s} className="mr-3 inline-flex items-center gap-1"><input type="checkbox" aria-label={s} checked={skills.has(s)} disabled={busy} onChange={() => toggle(skills, setSkills, s)} />{s}</label>))}
              </fieldset>
            )}
            {preview.mcp_servers.length > 0 && (
              <fieldset><legend className="text-xs text-muted-foreground">MCP servers (secrets are never imported — you re-enter them)</legend>
                {preview.mcp_servers.map((s) => (<label key={s} className="mr-3 inline-flex items-center gap-1"><input type="checkbox" aria-label={s} checked={mcps.has(s)} disabled={busy} onChange={() => toggle(mcps, setMcps, s)} />{s}</label>))}
              </fieldset>
            )}
            {preview.plugins.length > 0 && (
              <fieldset><legend className="text-xs text-muted-foreground">Plugins (your workspace restarts to load them)</legend>
                {preview.plugins.map((p) => (<label key={p.id} className="flex items-center gap-2 py-0.5"><input type="checkbox" aria-label={p.id} checked={plugins.has(p.id)} disabled={busy} onChange={() => toggle(plugins, setPlugins, p.id)} />{p.spec}{p.has_dependencies && <span className="text-muted-foreground"> · needs deps installed in the container</span>}</label>))}
              </fieldset>
            )}
            <label className="flex items-center gap-2"><input type="checkbox" checked={memory} disabled={busy} onChange={() => setMemory(!memory)} />Memory logs (MEMORY.md, memory/)</label>
            <label className="flex items-center gap-2"><input type="checkbox" checked={cron} disabled={busy} onChange={() => setCron(!cron)} />Scheduled jobs</label>
            {anyHistory && (
              <label className="flex items-center gap-2"><input type="checkbox" aria-label="Bring conversation history" checked={history} disabled={busy} onChange={() => setHistory(!history)} />Bring conversation history (auth tokens are stripped; applied the next time your workspace starts on an updated platform image)</label>
            )}

            {partial && (
              <div role="alert" className="rounded-xl border border-[var(--warn)] p-3 text-[var(--warn)]">
                <p className="font-medium">Some agents couldn&apos;t be imported</p>
                <ul className="list-disc space-y-0.5 pl-5 mt-1">
                  {partial.failed.map((a) => (
                    <li key={a.source_agent_id}>{a.name} — {a.error}</li>
                  ))}
                </ul>
                <p className="mt-2">The staged import is kept — fix and retry, or continue.</p>
              </div>
            )}
            </>)}

            {preview.warnings.length > 0 && (
              <ul className="list-disc space-y-1 rounded-xl border border-[var(--warn)] p-3 pl-7 text-[var(--warn)]">{preview.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
            )}
            {preview.skipped_big_files.length > 0 && <p className="text-muted-foreground">Skipped {preview.skipped_big_files.length} file(s) over 512 MB.</p>}
            {safetyFlags.length > 0 && (
              <fieldset>
                <legend className="text-xs text-muted-foreground">
                  Safety notes (don&apos;t block the import; you&apos;ll see them again if you publish)
                </legend>
                <ul className="space-y-1">
                  {safetyFlags.map((f, i) => (
                    <li key={`${f.file}-${f.line ?? 0}-${i}`} className="rounded-lg border border-[var(--edge)] bg-cream-deep px-2 py-1 font-mono text-caption text-muted-foreground">
                      <span className="mr-2 rounded-full bg-card px-1.5 py-0.5 text-2xs font-semibold uppercase tracking-caps text-muted-foreground">{f.severity}</span>
                      {f.pattern} · {f.file}{f.line ? `:${f.line}` : ""}
                    </li>
                  ))}
                </ul>
              </fieldset>
            )}
          </div>
        )}

        {error && <p role="alert" className="rounded-xl border border-[var(--err)] p-2 text-ui text-[var(--err)]">{error}</p>}

        <DialogFooter>
          <button type="button" onClick={cancel} disabled={busy} className="rounded-full px-4 py-2 text-ui text-muted-foreground hover:bg-[var(--midnight)]/5">Cancel</button>
          {partial && (
            <button type="button" onClick={continueWithPartial} disabled={busy}
              className="rounded-full border border-[var(--edge)] px-4 py-2 text-ui font-medium text-[var(--midnight)] hover:bg-[var(--midnight)]/5">
              Continue with {partial.okIds.length} imported
            </button>
          )}
          {preview && (claudeItems ? (
            <button type="button" onClick={() => { onImported(target); onOpenChange(false); reset(); }}
              className="rounded-full bg-[var(--midnight)] px-4 py-2 text-ui font-medium text-[var(--cream)]">
              Done
            </button>
          ) : (
            <button type="button" onClick={() => void confirm()}
              disabled={busy || (claude
                // No target, or nothing at all would land (no collections, no
                // skills, no user memory) — an empty confirm would just delete
                // the staging behind an empty success.
                ? !target || (claudeCols.size === 0 && skills.size === 0 && !claude.has_user_memory)
                : agents.size === 0 || needsUltra)}
              className="rounded-full bg-[var(--midnight)] px-4 py-2 text-ui font-medium text-[var(--cream)] disabled:opacity-50">
              {phase === "confirming" ? "Adding…" : claude ? "Import context" : partial ? "Retry" : "Add to my team"}
            </button>
          ))}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
