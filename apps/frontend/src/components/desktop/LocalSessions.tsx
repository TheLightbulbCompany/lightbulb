"use client";
import { useAuth } from "@clerk/nextjs";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";

type State = { enrollment: { ownerId: string; humanUserId: string; enabled: boolean; paused: boolean } | null; pairedWorkspaceId: string | null; status: string };
type Invoke = <T = unknown>(command: string, args?: Record<string, unknown>) => Promise<T>;
function bridge(): Invoke | undefined {
  return (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
}
function message(error: unknown): string {
  return /not found|unknown command/i.test(String(error))
    ? "Update the Lightbulb desktop app, then reopen Settings."
    : String(error instanceof Error ? error.message : error);
}

export function LocalSessions() {
  const { userId, orgId, getToken } = useAuth();
  const [state, setState] = useState<State | null>(null);
  const [desktop, setDesktop] = useState(false);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const operation = useRef(0);
  useEffect(() => { operation.current += 1; return () => { operation.current += 1; }; }, [userId, orgId]);
  useEffect(() => {
    const invoke = bridge();
    setDesktop(Boolean(invoke));
    if (!invoke) return;
    let active = true;
    void invoke<State>("local_sessions_status").then(s => { if (active) setState(s); }).catch(e => { if (active) setError(message(e)); });
    return () => { active = false; };
  }, []);
  const enabled = state?.enrollment?.enabled ?? false;
  const sameWorkspace = Boolean(orgId && state?.pairedWorkspaceId === orgId);
  async function setEnabled(next: boolean) {
    const invoke = bridge();
    if (!invoke) return;
    const currentOperation = ++operation.current;
    setBusy(true); setError(null);
    try {
      const token = next ? await getToken().catch(() => null) : null;
      if (operation.current !== currentOperation) return;
      const revision = await invoke<number>("local_sessions_set", { enabled: next, token, humanUserId: userId ?? null, workspaceId: orgId ?? null });
      if (!next && userId && orgId) {
        // Best effort only: a hung token refresh cannot keep local sharing alive.
        void getToken().then(token => {
          if (token && operation.current === currentOperation) {
            return invoke("local_sessions_revoke", { revision, token, humanUserId:userId, workspaceId:orgId });
          }
        }).catch(() => {});
      }
      setState(await invoke<State>("local_sessions_status"));
      setConsent(false);
    } catch (e) {
      setError(message(e));
      void invoke<State>("local_sessions_status").then(setState).catch(() => {});
    } finally { setBusy(false); }
  }
  return <section className="settings-panel space-y-4" aria-labelledby="local-sessions-heading">
    <h2 id="local-sessions-heading" className="text-lg font-medium">Native terminal sessions</h2>
    <p>Share currently active Claude Code and Codex sessions, including their existing conversation transcript, and new sessions with the current human members of every conversation you belong to in the enrolled workspace.</p>
    <p>Contributions can be visible across conversations: someone in one conversation may see input from another. Adding a member grants eligible history, not only future output. Personal memory files and unrelated historical sessions are not synced.</p>
    <p className="text-sm text-muted-foreground">Coverage: ordinary local macOS terminals using default profiles; Claude Code 2.1.268 and Codex 0.149.0. No SSH, containers, IDE runtimes or custom data directories. Codex appears after its first prompt creates a rollout; its idle/busy state is unknown. Completed public messages are shared, not token deltas. Busy input queues; approvals remain in your terminal. Claude attachment needs its native socket and a verified current-generation transcript; sessions are never restarted to enable control.</p>
    <p className="text-sm text-muted-foreground">Keep Lightbulb running, including in the menu bar. Changing the selected workspace does not move sharing. Disable sharing to uninstall its Claude wake hook; native sessions and provider history remain untouched. Setup preserves a recoverable Claude settings backup and changes no Codex launch configuration.</p>
    {!desktop ? <p>Open the Lightbulb desktop app to set up local-session sharing.</p> : <>
      {state && !state.pairedWorkspaceId ? <p>Connect this Mac first using the existing desktop setup.</p> : null}
      {state?.pairedWorkspaceId && !sameWorkspace ? <p>Select the workspace connected to this Mac before enabling sharing.</p> : null}
      {state?.enrollment ? <p>Enrolled workspace: {state.enrollment.ownerId}. Sharing {enabled ? (state.enrollment.paused ? "paused until the enrolled person signs in" : state.status || "connecting") : "disabled"}.</p> : null}
      {!enabled ? <label className="flex gap-2"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />I understand this sharing audience and want to enroll this workspace.</label> : null}
      <Button disabled={busy || !state || (!enabled && (!consent || !sameWorkspace || !userId))} onClick={() => void setEnabled(!enabled)}>
        {busy ? "Saving…" : enabled ? "Disable local-session sharing" : "Enable local-session sharing"}
      </Button>
      <Button variant="ghost" disabled={busy} onClick={() => { void bridge()?.<State>("local_sessions_status").then(setState).catch(e=>setError(message(e))); }}>Refresh status</Button>
    </>}
    {error ? <p role="alert">{error}</p> : null}
  </section>;
}
