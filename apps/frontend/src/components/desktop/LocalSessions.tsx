"use client";
import { useAuth, useOrganization } from "@clerk/nextjs";
import { useEffect, useRef, useState } from "react";
import { Bot, FolderOpen, Globe2, Monitor, Terminal, type LucideIcon } from "lucide-react";
import { LocalBrowserSetup } from "@/components/desktop/LocalBrowserSetup";
import { SettingRow } from "@/components/desktop/SettingRow";
import { useApi } from "@/lib/api";
import {
  type DesktopPermissionSet,
  desktopErrorMessage,
  readDesktopPermissionGrant,
  readDesktopPermissionsFromDesktop,
  writeDesktopPermissionGrant,
} from "@/lib/desktopPermissions";

type State = { enrollment: { ownerId: string; humanUserId: string; enabled: boolean; paused: boolean; recoveryPending?: boolean; remoteDisabled?: boolean } | null; pairedWorkspaceId: string | null; status: string; providers?: Record<"claude" | "codex", ProviderSetup> };
type ProviderSetup = { cli: boolean; mcp: "configured" | "missing" | "collision"; hooks: "configured" | "missing" | "disabled" };

/** One line per provider, from what the desktop actually read on disk. */
export function providerSetupLine(name: string, p: ProviderSetup): string {
  if (!p.cli) return `${name} — Not installed`;
  const reasons = [
    p.mcp === "collision" ? "another lightbulb MCP server is configured" : p.mcp === "missing" ? "MCP server missing" : null,
    p.hooks === "disabled" ? "Claude hooks are disabled in settings.json" : p.hooks === "missing" ? "hooks missing" : null,
  ].filter(Boolean);
  return reasons.length ? `${name} — Needs attention: ${reasons.join("; ")}` : `${name} — Ready for new sessions`;
}
type Invoke = <T = unknown>(command: string, args?: Record<string, unknown>) => Promise<T>;
function bridge(): Invoke | undefined {
  return (window as Window & { __TAURI__?: { core?: { invoke?: Invoke } } }).__TAURI__?.core?.invoke;
}
const message = desktopErrorMessage;

export const DESKTOP_PERMISSION_ITEMS: Array<{
  key: keyof DesktopPermissionSet;
  label: string;
  description: string;
  Icon: LucideIcon;
}> = [
  { key: "shell", label: "Shell", description: "Agents can run commands on this Mac.", Icon: Terminal },
  { key: "browser", label: "Browser", description: "Agents can open and use a browser on this Mac.", Icon: Globe2 },
  { key: "files", label: "Files", description: "Agents can read and write files in folders you allow.", Icon: FolderOpen },
  { key: "screen", label: "Screen", description: "Agents can take screenshots of this Mac.", Icon: Monitor },
  { key: "paperclip", label: "Paperclip actions", description: "Team (Paperclip) runs can use the switches above on this Mac.", Icon: Bot },
];

/** Settings: the per-capability switches chosen at pairing. The Mac answers every
 *  approval from these; there is no per-action prompt. */
export function DesktopPermissions() {
  const [permissions, setPermissions] = useState<DesktopPermissionSet | null>(null);
  useEffect(() => {
    if (!bridge()) return;
    void readDesktopPermissionsFromDesktop().then(p => setPermissions(p ?? readDesktopPermissionGrant()?.permissions ?? null));
  }, []);
  if (!permissions) return null;
  const toggle = (key: keyof DesktopPermissionSet) => {
    const next = { ...permissions, [key]: !permissions[key] };
    setPermissions(next);
    writeDesktopPermissionGrant(next);
  };
  return <section className="settings-card" aria-labelledby="desktop-permissions-heading">
    <div className="settings-card-header settings-card-header--rows">
      <div>
        <h2 id="desktop-permissions-heading" className="settings-card-title">Agents on this Mac</h2>
        <div className="settings-card-desc">What your agents may do here. They work within these switches and do not ask each time.</div>
      </div>
    </div>
    {DESKTOP_PERMISSION_ITEMS.map(({ key, label, description }) => (
      <SettingRow key={key} title={label} description={description} toggle={{ checked: permissions[key], onChange: () => toggle(key) }}>
        {key === "browser" && permissions.browser && <LocalBrowserSetup />}
      </SettingRow>
    ))}
  </section>;
}

export const SHARE_LIVE_LABEL = "Share every new session";
export const SHARE_LIVE_FILES = "~/.zshrc, ~/.bashrc and your bash login file (~/.bash_profile, ~/.bash_login or ~/.profile, whichever you have)";
export const SHARE_LIVE_DESCRIPTION = `On: every claude or codex you type in a new terminal tab is shared with your workspace, live, from its start. Off: a session is shared only when you choose to share it. Adds a marked block to ${SHARE_LIVE_FILES}; turning it off removes it.`;

/** Settings row: "Share every new session" — the `claude`/`codex` shell functions (the desktop's
 *  shell_integration module). Hidden in a browser and on a desktop build without the command. */
export function ShareEverySessionLive() {
  const [on, setOn] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const invoke = bridge();
    if (!invoke) return;
    void invoke<boolean>("shell_integration_status").then(setOn).catch(() => setOn(null));
  }, []);
  if (on === null) return null;
  const toggle = async () => {
    const invoke = bridge();
    if (!invoke) return;
    setBusy(true); setError(null);
    try { setOn(await invoke<boolean>("shell_integration_set", { enabled: !on })); }
    catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  };
  return <SettingRow
    id="share-every-session-live"
    title={SHARE_LIVE_LABEL}
    description={<>Typing <code>claude</code> or <code>codex</code> in a terminal starts it through Lightbulb, shared and live. Off: a session is shared only when you start it with Lightbulb or choose Share to Lightbulb.</>}
    toggle={{ checked: on, disabled: busy, onChange: () => void toggle() }}
  >
    <p className="settings-row-note">Adds a marked block to {SHARE_LIVE_FILES}, and removes it when you turn this off. Open a new terminal tab after you change it.</p>
    {error ? <p className="settings-row-error" role="alert">{error}</p> : null}
  </SettingRow>;
}

type AgentSessionPrefs = { agentSendWithoutAsking: boolean; sessionsReachableFromBackgroundWork: boolean };
const AGENT_SESSION_PREFS: Array<{ key: keyof AgentSessionPrefs; title: string; description: string }> = [
  { key: "agentSendWithoutAsking", title: "Agents can message my sessions without asking", description: "Off: an agent asks you first, and nothing is sent until you choose Send." },
  { key: "sessionsReachableFromBackgroundWork", title: "Background work (issues, heartbeats, routines) can reach my sessions", description: "Off: only an agent answering in a conversation can see or message your sessions." },
];

/** Settings rows: who may reach this person's shared sessions (spec 2026-10-07-agent-session-approval §3.4). */
export function AgentSessionSwitches() {
  const api = useApi();
  const [prefs, setPrefs] = useState<AgentSessionPrefs | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let cancelled = false;
    api.get("/users/me/prefs").then(value => { if (!cancelled) setPrefs(value as AgentSessionPrefs); }).catch(() => undefined);
    return () => { cancelled = true; };
    // `api` identity churns every render; fetch once per mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (prefs === null) return null;
  const toggle = async (key: keyof AgentSessionPrefs, next: boolean) => {
    setBusy(true);
    try { setPrefs((await api.put("/users/me/prefs", { [key]: next })) as AgentSessionPrefs); }
    catch { /* Unchanged on failure; the switch keeps showing the stored state. */ }
    finally { setBusy(false); }
  };
  // Account-level, so it shows in a browser too (the ask card points here).
  return <section className="settings-card" aria-label="Agents and your sessions">
    <div className="settings-card-header settings-card-header--rows">
      <div>
        <div className="settings-card-title">Agents and your sessions</div>
        <div className="settings-card-desc">When an agent may message your shared Claude Code and Codex sessions.</div>
      </div>
    </div>
    {AGENT_SESSION_PREFS.map(({ key, title, description }) => (
      <SettingRow key={key} title={title} description={description}
        toggle={{ checked: prefs[key], disabled: busy, onChange: next => void toggle(key, next) }} />
    ))}
  </section>;
}

export function useLocalSessions(waitForRemoteDisable = false) {
  const { userId, orgId, getToken } = useAuth();
  const [state, setState] = useState<State | null>(null);
  const [desktop, setDesktop] = useState(false);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const operation = useRef(0);
  useEffect(() => { operation.current += 1; setBusy(false); return () => { operation.current += 1; }; }, [userId, orgId]);
  useEffect(() => {
    const invoke = bridge();
    setDesktop(Boolean(invoke));
    if (!invoke) return;
    let active = true;
    setState(null); setError(null);
    const currentOperation = operation.current;
    void invoke<State>("local_sessions_status").then(s => {
      if (active && operation.current === currentOperation) setState(s);
    }).catch(e => { if (active && operation.current === currentOperation) setError(message(e)); });
    return () => { active = false; };
  }, [userId, orgId]);
  const enabled = state?.enrollment?.enabled ?? false;
  const recoveryPending = state?.enrollment?.recoveryPending ?? false;
  const canRecover = Boolean(userId && orgId && state?.enrollment?.humanUserId === userId && state.enrollment.ownerId === orgId);
  const sameWorkspace = Boolean(orgId && state?.pairedWorkspaceId === orgId);
  async function refresh(invoke: Invoke, currentOperation: number) {
    const latest = await invoke<State>("local_sessions_status");
    if (operation.current === currentOperation) setState(latest);
    return latest;
  }
  async function setEnabled(next: boolean) {
    const invoke = bridge();
    if (!invoke) return false;
    const currentOperation = ++operation.current;
    setBusy(true); setError(null);
    try {
      let token: string | null = null;
      if (next) {
        if (!userId) throw new Error("Sign in before enabling sharing.");
        // Heal the native auth-report race: the desktop gates enable on its
        // last reported signed-in user, which can lag this click (app launch
        // or sign-in just completed). Re-report first; best-effort.
        await invoke("local_sessions_auth", { humanUserId: userId }).catch(() => {});
        if (operation.current !== currentOperation) return false;
        try {
          token = await getToken();
        } catch {
          token = null;
        }
        if (!token) throw new Error("Could not get a sign-in token. Check your sign-in and try again.");
      }
      if (operation.current !== currentOperation) return false;
      const revision = await invoke<number>("local_sessions_set", { enabled: next, token, humanUserId: userId ?? null, workspaceId: orgId ?? null });
      const latest = await refresh(invoke, currentOperation);
      if (operation.current !== currentOperation) return false;
      if (!next && latest.enrollment && userId && orgId) {
        // The local stop is already complete. Token refresh and remote cleanup
        // cannot delay it; pending state stays visible until confirmed.
        const cleanup = getToken().then(async token => {
          if (operation.current !== currentOperation) return;
          if (!token) throw new Error("Sign in to confirm remote-history revocation.");
          await invoke("local_sessions_revoke", { revision, token, humanUserId:userId, workspaceId:orgId });
          const confirmed = await refresh(invoke, currentOperation);
          if (waitForRemoteDisable && confirmed.enrollment && !confirmed.enrollment.remoteDisabled) {
            throw new Error("Local sharing stopped. Sign in as the enrolled person in the enrolled workspace to disable retained history in Settings.");
          }
        });
        if (waitForRemoteDisable) await cleanup;
        else void cleanup.catch(e => { if (operation.current === currentOperation) setError(message(e)); });
      }
      setConsent(false);
      return true;
    } catch (e) {
      if (operation.current === currentOperation) setError(message(e));
      void refresh(invoke, currentOperation).catch(() => {});
      return false;
    } finally { if (operation.current === currentOperation) setBusy(false); }
  }
  return {state,desktop,consent,setConsent,busy,error,setError,enabled,recoveryPending,canRecover,sameWorkspace,userId,orgId,setEnabled,refresh,operation};
}


/** Settings card: "Session sharing" — the Claude Code and Codex sessions this Mac shares,
 *  plus the "Share every new session" row and the details of what sharing changes. */
export function LocalSessions() {
  const {state,desktop,consent,setConsent,busy,error,setError,enabled,recoveryPending,canRecover,sameWorkspace,userId,setEnabled,refresh,operation} = useLocalSessions();
  const { organization } = useOrganization();
  if (!desktop) return null;
  const enrollment = state?.enrollment;
  const paused = enabled && Boolean(enrollment?.paused);
  const workspace = enrollment && organization && enrollment.ownerId === organization.id ? organization.name : "another workspace";
  const status = !state ? (error ? null : <span role="status">Checking this Mac…</span>)
    : enabled ? (paused ? "Paused until you sign in." : `On · ${workspace}`)
    : !state.pairedWorkspaceId ? "Connect this Mac first."
    : !sameWorkspace ? "This Mac is connected to another workspace. Switch to that workspace to turn sharing on."
    : "Off";
  const checkAgain = () => { const invoke=bridge(); const current=operation.current; if(invoke) void refresh(invoke,current).catch(e=>{if(operation.current===current)setError(message(e));}); };
  const retrySetup = () => { const invoke=bridge(); const current=operation.current; if(invoke) void invoke("local_sessions_repair").then(()=>refresh(invoke,current)).catch(e=>{if(operation.current===current)setError(message(e));}); };
  return <section className="settings-card" aria-labelledby="session-sharing-heading">
    <div className="settings-card-header settings-card-header--rows">
      <div>
        <h2 id="session-sharing-heading" className="settings-card-title">Session sharing</h2>
        <div className="settings-card-desc">People in your conversations can see the Claude Code and Codex sessions you share from this Mac.</div>
      </div>
    </div>
    <SettingRow title="Share sessions from this Mac" description={status}>
      {/* "connected" is the healthy state: "On" already says it. Anything else is a sentence worth showing. */}
      {enabled && !paused && state?.status && state.status !== "connected" ? <p className="settings-row-note">{state.status}</p> : null}
      {!enabled && state?.status.startsWith("Local sharing stopped;") ? <p className="settings-row-note" role="status">{state.status}</p> : null}
      {recoveryPending ? <p className="settings-row-note">Earlier history may still be shared. Sign in as the person who turned sharing on, in that workspace, and finish turning it off.</p> : null}
      {enrollment?.remoteDisabled ? <p className="settings-row-note">Earlier history is no longer shared.</p> : null}
      {enrollment && state?.providers ? <div className="settings-row-group">
        <p className="settings-row-body">{providerSetupLine("Claude Code", state.providers.claude)}</p>
        <p className="settings-row-body">{providerSetupLine("Codex", state.providers.codex)}</p>
        <p className="settings-row-note">Sessions that are already running do not pick this up. Start a new one.</p>
      </div> : null}
      {!enabled && !recoveryPending ? <div className="settings-row-group">
        <p className="settings-row-body">Nothing is shared until you start a session with Lightbulb, choose Share to Lightbulb in it, or turn on “{SHARE_LIVE_LABEL}”. Every member of your workspace can see a shared session’s live terminal and conversation. “Anyone with the link” is a separate choice you make per session, and you can switch any session back to Only me.</p>
        <label className="settings-consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />I understand who can see my sessions.</label>
      </div> : null}
      <div className="settings-row-actions">
        <button type="button" className={enabled ? "settings-btn-outline" : "settings-btn-save"} disabled={busy || !state || (recoveryPending ? !canRecover : !enabled && (!consent || !sameWorkspace || !userId))} onClick={() => void setEnabled(!enabled && !recoveryPending)}>
          {busy ? "Saving…" : enabled ? "Turn off sharing" : recoveryPending ? "Finish turning off" : "Turn on sharing"}
        </button>
        {enabled && enrollment && state?.providers ? <button type="button" className="settings-btn-outline" disabled={busy} onClick={retrySetup}>Retry setup</button> : null}
        <button type="button" className="settings-btn-text" disabled={busy} onClick={checkAgain}>Check again</button>
      </div>
      {error ? <p className="settings-row-error" role="alert">{error}</p> : null}
    </SettingRow>
    <ShareEverySessionLive />
    <details className="settings-details">
      <summary>What is shared, and what this changes on your Mac</summary>
      <div className="settings-details-body">
        <p><strong>Who sees it.</strong> Every member of the workspace where sharing is on sees a shared session, and someone who joins later sees it too. Nobody outside the workspace does, unless you choose “Anyone with the link” for that session: then anyone with its link can watch its live terminal, view only. You can switch any session back to Only me. Sessions you have not shared and personal memory files stay on this Mac.</p>
        <p><strong>What is shared.</strong> A session you start with Lightbulb (New session, <code>lightbulb claude</code>, <code>lightbulb codex</code>), every new session while Share every new session is on, and any other session once you choose Share to Lightbulb in it. Nothing of any other session leaves this Mac. A shared session is its live terminal, whole, from its start; one that runs inside the Claude or Codex app has no terminal and is shared as its conversation. A credential filter runs on this Mac, but it is not a guarantee.</p>
        <p><strong>Where it works.</strong> Claude Code and Codex in ordinary local macOS terminals with default profiles, not over SSH, in containers, in IDE runtimes or with custom data folders. A Codex session appears after its first prompt. For a session shared as its conversation, approvals stay on your Mac.</p>
        <p><strong>What it changes on this Mac.</strong> Sharing installs a <code>lightbulb-session-sharing</code> skill and a Claude wake hook, and this Mac serves the <code>lightbulb</code> MCP server to Claude Code and Codex in place of a hosted <code>lightbulb</code> entry. Setup keeps a recoverable backup of your Claude settings and changes no Codex launch configuration. Turning sharing off removes them and restores nothing. To use Lightbulb without the app, install the Lightbulb plugin from the <code>TheLightbulbCompany/lightbulb-plugin</code> marketplace in its place: <code>claude plugin marketplace remove lightbulb --scope user</code>, then <code>claude plugin marketplace add TheLightbulbCompany/lightbulb-plugin</code> and <code>claude plugin install lightbulb@lightbulb</code>. Keep Lightbulb running (the menu bar counts). Changing the selected workspace does not move sharing. Anything Lightbulb cannot share (sharing off, the app closed, <code>claude -p</code>, scripts) runs as usual.</p>
      </div>
    </details>
  </section>;
}
