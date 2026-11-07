"use client";

import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { useAuth } from "@clerk/nextjs";
import { MessageSquare } from "lucide-react";
import {
  auditLine, pickupSource, pickupStatus, sessionChanges, sessionFacts, sessionPlan, sessionStatus, sessionTitle, sessionToolName, usageLine,
  useSessionAudit, useTeamControl, useUsageSharing,
  type LocalSession, type LocalSessionEvent, type SessionControlClient, sessionAgentName,
} from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { SessionCommitPr } from "./SessionCommitPr";
import { SessionOverview } from "./SessionOverview";
import { SessionPickup } from "./SessionPickup";
import { SessionShareLink } from "./SessionShareLink";
import { SessionRenameButton } from "./SessionRenameButton";
import type { PickupClient } from "./useSessionPickup";

export type SessionDetailsTab = "about" | "changes" | "access";

type Client = SessionControlClient & PickupClient & Parameters<typeof SessionShareLink>[0]["client"] & Parameters<typeof SessionCommitPr>[0]["client"] & { setLocalSessionDisplayTitle?: (sessionId: string, title: string | null) => Promise<LocalSession> };

/** "Claude Code 2.1.283": the tool by its name, then the version out of whatever its `--version` printed. */
function toolVersion(session: LocalSession): string {
  const version = session.providerVersion.match(/\d+(?:\.\d+)+/)?.[0];
  return [sessionAgentName(session.provider), version].filter(Boolean).join(" ");
}

/** One fact about the session: what it is, and the value as its terminal would print it. */
function Fact({ label, children }: { label: string; children: ReactNode }) {
  return <div className="session-fact"><dt>{label}</dt><dd>{children}</dd></div>;
}

/** One owner setting. It takes effect as it moves, so it is a switch and says what it does underneath. */
function Setting({ label, children, checked, disabled, onChange, error }: {
  label: string; children: ReactNode; checked: boolean; disabled?: boolean; onChange: (next: boolean) => void; error?: string | null;
}) {
  const id = useId();
  return <div className="session-setting">
    <div className="session-setting-text">
      <label htmlFor={id}>{label}</label>
      <div id={`${id}-about`} className="session-setting-about">{children}</div>
      {error && <p role="alert" className="text-warn">{error}</p>}
    </div>
    <Switch id={id} checked={checked} disabled={disabled} onCheckedChange={onChange} aria-describedby={`${id}-about`} />
  </div>;
}

function About({ session, ownerName, own }: { session: LocalSession; ownerName: string; own: boolean }) {
  const usage = usageLine(session.usage);
  const source = pickupSource(session.pickup);
  const queued = session.capabilities.sendMode === "queue";
  return <>
    <dl className="session-facts">
      <Fact label="Owner">{own ? `${ownerName} (you)` : ownerName}</Fact>
      <Fact label="Tool"><span className="session-fact-value">{toolVersion(session)}</span></Fact>
      {sessionFacts(session) && <Fact label="Model and mode"><span className="session-fact-value">{sessionFacts(session)}</span></Fact>}
      {session.cwdName && <Fact label="Folder"><span className="session-fact-value">{session.cwdName}</span></Fact>}
      {source && <Fact label="Repository"><span className="session-fact-value">{source}</span></Fact>}
      <Fact label="Status">{sessionStatus[session.status]}{session.lastEventAt && <span className="text-muted-foreground"> · active {timeAgo(session.lastEventAt)}</span>}</Fact>
      {usage && <Fact label="Tokens"><span className="session-fact-value" title="As the tool reports them. Neither Claude Code nor Codex reports a cost.">{usage}</span></Fact>}
    </dl>
    <div className="session-notes">
      <p>Follow-ups {queued ? "queue behind the current turn" : "wait for the current turn"} on {own ? "your" : `${ownerName}'s`} Mac. They run with {own ? "your" : "their"} permissions and count against {own ? "your" : "their"} usage.</p>
      <p>{session.workspace?.overlap ? "Another session is working in the same checkout; coordinate edits. " : ""}
        {session.workspace?.coverage === "bounded" ? "Ignored, sensitive and oversized files are not watched for outside changes." : "Outside changes to this checkout are not being watched."}</p>
    </div>
  </>;
}

/**
 * The owner's switches. Held by SessionDetails, which stays mounted: Radix
 * unmounts a tab it is not showing, and a switch's saved state lives in the
 * hook until the session refresh brings it, so a hook inside the tab would
 * forget a save and show the old value on the way back.
 */
function useAccessSwitches(client: Client, session: LocalSession) {
  return {
    control: useTeamControl(client, session),
    usage: useUsageSharing(client, session),
  };
}

/** People the owner can take back one by one, each with Remove. */
function People({ session, ids, remove, label, about, nameOf }: {
  session: LocalSession; ids: string[] | undefined; remove: ((sessionId: string, userId: string) => Promise<unknown>) | undefined;
  label: string; about: string; nameOf: (userId: string) => string;
}) {
  const [removed, setRemoved] = useState<string[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A removal is only hidden until the session catches up; a later allow shows the person again.
  useEffect(() => { setRemoved(list => list.filter(id => (ids ?? []).includes(id))); }, [ids]);
  const people = (ids ?? []).filter(id => !removed.includes(id));
  if (!remove || people.length === 0) return null;
  const take = async (id: string) => {
    // Hidden before the request: the backend announces the change before it answers, and the
    // effect above clears this once the session stops listing the person.
    setBusy(id); setError(null); setRemoved(list => [...list, id]);
    try { await remove(session.sessionId, id); }
    catch { setRemoved(list => list.filter(x => x !== id)); setError(`Could not remove ${nameOf(id)}. Try again.`); }
    finally { setBusy(null); }
  };
  return <div className="session-setting">
    <div className="session-setting-text">
      <p className="session-setting-label">{label}</p>
      <p className="session-setting-about">{about}</p>
      <ul className="session-allowed-senders">
        {people.map(id => <li key={id} className="flex items-center justify-between gap-2">
          <span>{nameOf(id)}</span>
          <Button type="button" variant="outline" size="sm" disabled={busy !== null} onClick={() => void take(id)}
            aria-label={`Remove ${nameOf(id)}`}>Remove</Button>
        </li>)}
      </ul>
      {error && <p role="alert" className="text-warn">{error}</p>}
    </div>
  </div>;
}

/** Who took a copy and when: the sharer's view of Continue (spec §2.1 step 4). */
function Copies({ session, client, nameOf }: { session: LocalSession; client: Client; nameOf: (userId: string) => string }) {
  const entries = useSessionAudit(client, session.sessionId, session.latestEventSeq).filter(e => e.action === "session_copied");
  if (!entries.length) return null;
  return <section aria-label="Copies" className="session-setting-text">
    <p className="session-setting-label">Copies taken</p>
    <ul>{entries.map(e => <li key={e.at + (e.actorUserId ?? "")}>{auditLine(e, nameOf)} · {timeAgo(e.at)}</li>)}</ul>
  </section>;
}

function Access({ session, ownerName, own, client, nameOf, switches: { control, usage } }: {
  session: LocalSession; ownerName: string; own: boolean; client: Client; nameOf: (userId: string) => string;
  switches: ReturnType<typeof useAccessSwitches>;
}) {
  const { userId } = useAuth();
  const ended = session.status === "ended";
  if (!own) {
    const allows = [session.teamControl ? "send follow-ups and answer its prompts" : "", session.pickup?.allowed ? "pick it up on their own Mac" : ""].filter(Boolean);
    const allowed = !!userId && (session.allowedSenders ?? []).includes(userId) && !session.teamControl;
    return <div className="session-notes">
      <p>{allows.length ? `${ownerName} lets teammates ${allows.join(", and ")}.`
        : allowed ? `${ownerName} let you send follow-ups and answer its prompts. Anyone in the workspace can read it.`
        : `Only ${ownerName} can send to this session or hand it on. Anyone in the workspace can read it.`}</p>
      {allowed && allows.length > 0 && <p>{ownerName} let you send follow-ups and answer its prompts.</p>}
      {session.pickup?.allowed && <p className="text-muted-foreground">{pickupStatus(session.pickup, timeAgo)}</p>}
    </div>;
  }
  return <div className="session-settings">
    {/* The one place that says who can see the session: Only me is what the old sharing switch did. */}
    <div className="session-setting session-setting-stacked">
      <SessionShareLink session={session} client={client} />
      {session.pickup?.allowed && <p role="status" className="session-setting-state">{pickupStatus(session.pickup, timeAgo) || "Taking the first snapshot."}</p>}
    </div>
    {!ended && client.setLocalSessionTeamControl && <Setting label="Let teammates control this session" checked={control.enabled} disabled={control.saving} onChange={next => void control.set(next)} error={control.error}>
      Workspace members can type in it, send follow-ups and answer permission prompts on your Mac, without asking. Off, they watch. Every action is logged in the session’s conversation. Turns off when the session ends.
    </Setting>}
    {!ended && <People session={session} ids={session.allowedSenders} remove={client.removeLocalSessionSender} nameOf={nameOf}
      label="People you let in" about="They asked to join from the session’s link. They can send follow-ups and answer permission prompts until the session ends." />}
    {ended && <People session={session} ids={session.resumeAllowed} remove={client.revokeLocalSessionResume} nameOf={nameOf}
      label="Who can resume it" about="They could send to it when it ended. They can start it again on your Mac, in plan mode, with a message." />}
    {session.usage && client.setLocalSessionUsageSharing && <Setting label="Show token usage to teammates" checked={usage.enabled} disabled={usage.saving} onChange={next => void usage.set(next)} error={usage.error}>
      Teammates see the token counts this session reports. They never see a cost.
    </Setting>}
    <Copies session={session} client={client} nameOf={nameOf} />
  </div>;
}

/**
 * Everything about a session that is not the conversation itself: what it is
 * running, what it changed, who may do what with it. The session pane keeps the
 * transcript and the prompt; this is where the rest lives.
 */
export function SessionDetails({ open, onOpenChange, onClosed, tab, onTab, session, ownerName, own, client, events, onMessageOwner, nameOf, terminalHint }: {
  open: boolean; onOpenChange: (open: boolean) => void;
  /** Once closed: put focus back on whatever opened it. Nothing here is a Radix trigger, so Radix cannot. */
  onClosed?: () => void;
  tab: SessionDetailsTab; onTab: (tab: SessionDetailsTab) => void;
  session: LocalSession; ownerName: string; own: boolean; client: Client;
  /** The session's own loaded events, for its plan and its changes. */
  events: readonly LocalSessionEvent[];
  terminalHint?: ReactNode;
  onMessageOwner?: () => void;
  /** A workspace member's name, for the people the owner let in. */
  nameOf?: (userId: string) => string;
}) {
  const switches = useAccessSwitches(client, session);
  const hasChanges = useMemo(() => !!session.changes || Boolean(sessionPlan(events)?.length) || sessionChanges(events).length > 0, [events, session.changes]);
  return <Dialog open={open} onOpenChange={onOpenChange}>
    {/* Focus lands on the dialog, not its first tab: a ring on "About" would read as the open tab. */}
    <DialogContent tabIndex={-1} onOpenAutoFocus={event => { event.preventDefault(); (event.currentTarget as HTMLElement | null)?.focus(); }}
      onCloseAutoFocus={event => { event.preventDefault(); onClosed?.(); }}
      className="session-details max-w-[calc(100%-2rem)] max-h-[calc(100dvh-2rem)] overflow-y-auto text-ui outline-none">
      <DialogHeader>
        <DialogTitle className="room-wrap">{sessionTitle(session)}</DialogTitle>
        <DialogDescription>{sessionToolName(session.provider)} session, {own ? "yours" : `owned by ${ownerName}`}.</DialogDescription>
      </DialogHeader>
      <Tabs value={tab} onValueChange={value => onTab(value as SessionDetailsTab)}>
        <TabsList variant="line" aria-label="Session details">
          <TabsTrigger value="about">About</TabsTrigger>
          <TabsTrigger value="changes">Changes</TabsTrigger>
          <TabsTrigger value="access">Access</TabsTrigger>
        </TabsList>
        <TabsContent value="about" className="session-details-tab"><About session={session} ownerName={ownerName} own={own} /></TabsContent>
        <TabsContent value="changes" className="session-details-tab">
          {hasChanges
            ? <SessionOverview expanded events={events} git={session.changes} changesAction={<SessionCommitPr session={session} client={client} own={own} ownerName={ownerName} />} />
            : <p className="text-muted-foreground">No plan or file changes yet.</p>}
        </TabsContent>
        <TabsContent value="access" className="session-details-tab"><Access session={session} ownerName={ownerName} own={own} client={client} switches={switches} nameOf={nameOf ?? (() => "A teammate")} />{terminalHint}</TabsContent>
      </Tabs>
      {/* One link to copy, under Access ("Who can see this"): a workspace member who opens it lands in the app. */}
      <div className="session-details-actions">
        {own && client.setLocalSessionDisplayTitle && <SessionRenameButton session={session} onRename={client.setLocalSessionDisplayTitle} />}
        {!own && onMessageOwner && <Button type="button" variant="outline" size="sm" onClick={onMessageOwner}><MessageSquare aria-hidden="true" />Message {ownerName}</Button>}
        {own && <SessionPickup session={session} ownerName={ownerName} own client={client} variant="quiet" />}
      </div>
    </DialogContent>
  </Dialog>;
}
