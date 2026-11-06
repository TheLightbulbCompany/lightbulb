"use client";

import { useEffect, useRef, useState } from "react";

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
  return err?.message || "Import failed";
}

// Same local bridge-detection pattern as components/desktop/DesktopFileAccess:
// the Tauri webview injects window.__TAURI__; a regular browser never has it.
type TauriInvoke = <T = unknown>(cmd: string, args?: Record<string, unknown>) => Promise<T>;

function getTauriInvoke(): TauriInvoke | null {
  if (typeof window === "undefined") return null;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const invoke = (window as any).__TAURI__?.core?.invoke as TauriInvoke | undefined;
  return invoke ?? null;
}

/** Import a Claude Code context archive onto one existing roster agent.
 *
 * The OpenClaw-backup rail this dialog was built for is gone: the backend refuses any archive
 * that is not a `~/.claude` slice, so the agent/plugin/MCP checklists it used to render — and
 * the `openclaw backup create` instruction above them — would have walked the seller into a
 * guaranteed 400. */
export function ImportBackupDialog({ open, onOpenChange, onImported }: {
  open: boolean; onOpenChange: (open: boolean) => void; onImported: (agentId: string) => void;
}) {
  const api = useApi();
  const inputRef = useRef<HTMLInputElement>(null);
  const [phase, setPhase] = useState<Phase>("pick");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Which roster agent receives the staged content, which memory collections and skills come
  // along, and the per-item results after confirm.
  const [target, setTarget] = useState("");
  const [collections, setCollections] = useState<Set<string>>(new Set());
  const [skills, setSkills] = useState<Set<string>>(new Set());
  const [items, setItems] = useState<ImportConfirmResult["claude_items"] | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  // Bumped by reset(): a native export resolving after the dialog closed
  // compares against this and drops its result instead of re-driving upload.
  const generationRef = useRef(0);
  // Set post-mount (SSR-safe, same as DesktopFileAccess) — non-null only in
  // the Tauri webview, where the one-click "Import from this Mac" renders.
  const [tauriInvoke, setTauriInvoke] = useState<TauriInvoke | null>(null);
  useEffect(() => { setTauriInvoke(() => getTauriInvoke()); }, []);

  // Null key until a preview exists — SWR's canonical "skip the request" signal, so opening the
  // dialog never hits the Teams BFF.
  const { read } = useTeamsApi();
  const roster = read<{ agents: CompanyAgent[] }>(preview ? "/agents" : null);
  const rosterAgents = roster.data?.agents ?? [];

  function reset() {
    generationRef.current += 1;
    setPhase("pick"); setPreview(null); setError(null);
    setTarget(""); setCollections(new Set()); setSkills(new Set()); setItems(null); setWarnings([]);
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
      setCollections(new Set(p.claude.collections.map((c) => c.id)));
      setSkills(new Set(p.claude.skills));
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

  const claude = preview?.claude ?? null;

  async function confirm() {
    if (!preview) return;
    setError(null); setPhase("confirming");
    try {
      // Nothing is minted — content lands on an existing roster agent, so a 2xx with
      // claude_items IS success. onImported fires from the Done button: the parent closes the
      // dialog inside onImported, which would hide these results.
      const r = (await api.post(`/marketplace/import/${preview.import_id}/confirm`, {
        target_agent_id: target, claude_collections: [...collections], claude_skills: [...skills],
      })) as ImportConfirmResult;
      setItems(r.claude_items ?? []);
      setWarnings(r.warnings ?? []);
      setPhase("preview");
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
          <DialogTitle>Import your Claude Code context</DialogTitle>
          <DialogDescription>
            Bring your CLAUDE.md, project memories and skills over to one of your agents.
            {tauriInvoke ? " Use the button below — or, from a terminal:" : " From a terminal, run"}{" "}
            <code className="font-mono text-xs">{"cd ~ && find .claude -maxdepth 3 \\( -path .claude/CLAUDE.md -o -path '.claude/projects/*/memory' -o -path .claude/skills \\) -prune -print | tar czf claude-code.tar.gz -T -"}</code>{" "}
            (include whichever exist) and upload it. Pick what comes along; nothing is applied until you confirm.
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
            <input ref={inputRef} type="file" accept=".tar.gz,.tgz,application/gzip,application/x-gzip" aria-label="Choose a Claude Code archive"
              disabled={busy} onChange={(e) => void handleFile(e.target.files?.[0])} className="block w-full text-ui" />
            {phase === "uploading" && <p className="mt-2 text-muted-foreground">Uploading…</p>}
            {phase === "scanning" && <p className="mt-2 text-muted-foreground">Scanning the archive…</p>}
          </label>
        )}

        {preview && claude && (phase === "preview" || phase === "confirming") && (
          <div className="max-h-[60vh] space-y-3 overflow-y-auto text-ui">
            <div className="rounded-xl border border-[var(--edge)] bg-card p-3">
              <p className="text-sm font-medium text-[var(--midnight)]">{preview.name}</p>
              <p className="mt-1 text-muted-foreground">
                {claude.collections.length} project memories · {claude.collections.reduce((n, c) => n + c.file_count, 0)} memory files · {claude.skills.length} skill{claude.skills.length === 1 ? "" : "s"}
              </p>
            </div>

            <label className="block">
              <span className="text-xs text-muted-foreground">Apply to agent</span>
              <select aria-label="Target agent" value={target} disabled={busy || !!items}
                onChange={(e) => setTarget(e.target.value)}
                className="mt-1 w-full rounded-lg border border-[var(--edge)] bg-cream-deep px-3 py-2 text-ui text-[var(--midnight)] disabled:opacity-50">
                <option value="">Choose an agent…</option>
                {rosterAgents.map((a) => (
                  <option key={a.id} value={a.id}>{a.name}{a.role === "ceo" ? " — Coordinator" : a.title ? ` — ${a.title}` : ""}</option>
                ))}
              </select>
            </label>
            {roster.error != null && <p className="text-xs text-[var(--err)]">Couldn&apos;t load your team — reopen the dialog to retry.</p>}
            {claude.collections.length > 0 && (
              <fieldset><legend className="text-xs text-muted-foreground">Project memories</legend>
                {claude.collections.map((c) => (
                  <label key={c.id} className="flex items-center gap-2 py-0.5">
                    <input type="checkbox" aria-label={c.label} checked={collections.has(c.id)} disabled={busy || !!items} onChange={() => toggle(collections, setCollections, c.id)} />
                    <span>{c.label} · <span className="text-muted-foreground">{c.file_count} file{c.file_count === 1 ? "" : "s"}</span></span>
                  </label>
                ))}
              </fieldset>
            )}
            {claude.skills.length > 0 && (
              <fieldset><legend className="text-xs text-muted-foreground">Skills</legend>
                {claude.skills.map((s) => (<label key={s} className="mr-3 inline-flex items-center gap-1"><input type="checkbox" aria-label={s} checked={skills.has(s)} disabled={busy || !!items} onChange={() => toggle(skills, setSkills, s)} />{s}</label>))}
              </fieldset>
            )}
            {claude.has_user_memory && <p>Global CLAUDE.md → appended to the agent&apos;s USER.md</p>}
            {Object.keys(claude.skipped).length > 0 && (
              <p className="text-muted-foreground">Skipped: {Object.entries(claude.skipped).map(([k, v]) => `${v} ${k}`).join(" · ")}</p>
            )}
            {items && (
              <div className="rounded-xl border border-[var(--edge)] bg-card p-3">
                <p className="font-medium text-[var(--midnight)]">Imported</p>
                <ul className="mt-1 space-y-0.5">
                  {items.map((i) => (
                    <li key={i.item} className="font-mono text-caption text-muted-foreground">{i.item} — {i.status === "applied" ? "applied" : "already exists, skipped"}</li>
                  ))}
                </ul>
                {warnings.length > 0 && (
                  <ul className="mt-2 list-disc space-y-1 rounded-xl border border-[var(--warn)] p-2 pl-6 text-[var(--warn)]">
                    {warnings.map((w) => <li key={w}>{w}</li>)}
                  </ul>
                )}
              </div>
            )}

            {claude.safety_flags.length > 0 && (
              <fieldset>
                <legend className="text-xs text-muted-foreground">
                  Safety notes (don&apos;t block the import; you&apos;ll see them again if you publish)
                </legend>
                <ul className="space-y-1">
                  {claude.safety_flags.map((f, i) => (
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
          {claude && (items ? (
            <button type="button" onClick={() => { onImported(target); onOpenChange(false); reset(); }}
              className="rounded-full bg-[var(--midnight)] px-4 py-2 text-ui font-medium text-[var(--cream)]">
              Done
            </button>
          ) : (
            <button type="button" onClick={() => void confirm()}
              // No target, or nothing at all would land (no collections, no skills, no user
              // memory) — an empty confirm would just delete the staging behind an empty success.
              disabled={busy || !target || (collections.size === 0 && skills.size === 0 && !claude.has_user_memory)}
              className="rounded-full bg-[var(--midnight)] px-4 py-2 text-ui font-medium text-[var(--cream)] disabled:opacity-50">
              {phase === "confirming" ? "Adding…" : "Import context"}
            </button>
          ))}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
