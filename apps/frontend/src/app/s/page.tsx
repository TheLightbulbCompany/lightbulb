"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { SignInButton, SignUpButton, useAuth, useClerk } from "@clerk/nextjs";
import { AppSyncEventsClient, createMessagingClient, type LiveEvent, type SessionLinkCode, type SessionLinkJoinAsk, type SessionLinkPage, type SessionLinkViewing, sessionAgentName } from "@lightbulb/shared";
import { ApiError, BACKEND_URL, useApi } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { capture, markWorkspaceDoor } from "@/lib/analytics";
import { copyText, sessionPermalink } from "@/components/messaging/messageLinks";
import { SessionTerminalView } from "@/components/messaging/SessionTerminalView";
// For .session-state-panel: this route is outside /chat, which is where the sheet is otherwise loaded.
import "@/components/messaging/messaging.css";

// The link's credential lives in the fragment, kept in this tab across sign-up (Clerk's modal
// on this page) and onboarding, never in a URL a server sees (spec §3.7).
const KEY = "lightbulb:session-link";
/** Set by a signed-out "Ask to join": the request goes once they are back from signing up. */
const ASK_KEY = "lightbulb:session-link-ask";
const POLL_MS = 5000;
/** While the terminal is up: its own 15 s renewal already hears a refusal, an end or an offline Mac
 *  (and asks for a re-open then), so this poll only follows the title and the Continue switch.
 *  A link viewer then costs the shared /public throttle 0.1 requests a second, not 0.27. */
const WATCHING_POLL_MS = 30_000;
/** After a refusal the open page keeps asking, slowly and not for long: the owner may have narrowed
 *  the link and widened it again (a refused open costs one read). It stops while the tab is hidden,
 *  and starts again with a fresh ten minutes when the person comes back to it. */
const REFUSED_POLL_MS = 30_000;
const REFUSED_POLL_FOR_MS = 10 * 60_000;
const watchingPage = (page: SessionLinkPage) => page.state === "live" && !page.me.member;
type Stash = { token: string; port?: number; challenge?: string };

export const go = { to: (url: string) => window.location.assign(url) };

/** The CLI's confirm mode: only an integer port 1024-65535 and a 43-character challenge. */
export function parseConfirm(search: string): { port: number; challenge: string } | null {
  const q = new URLSearchParams(search);
  const port = q.get("port") ?? "", challenge = q.get("challenge") ?? "";
  const n = /^\d{4,5}$/.test(port) ? Number(port) : 0;
  return n >= 1024 && n <= 65535 && /^[A-Za-z0-9_-]{43}$/.test(challenge) ? { port: n, challenge } : null;
}

/** The stash as a JSON string ("" when there is none), so the snapshot compares stable between reads. */
let lifted = ""; // this visit's stash, for a tab whose sessionStorage is blocked
function readStash(): string {
  const fragment = decodeURIComponent(window.location.hash.slice(1));
  if (fragment) return (lifted = JSON.stringify({ token: fragment, ...(parseConfirm(window.location.search) ?? {}) }));
  try { return window.sessionStorage.getItem(KEY) ?? lifted; } catch { return lifted; }
}
// A second link opened in the same tab changes only the fragment: no navigation, just hashchange.
const onHash = (changed: () => void) => { window.addEventListener("hashchange", changed); return () => window.removeEventListener("hashchange", changed); };

/** Raw fetch: useApi refuses to call without a token, and these routes take none. */
async function publicPost<T>(path: string, body: unknown, token: string | null): Promise<T> {
  const res = await fetch(`${BACKEND_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(body),
  });
  const json = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, json, (json as { message?: string } | null)?.message || "Could not open this link. Try again.");
  return json as T;
}
/** The server refused the link itself. A 409 (offline, full), a 429 or a 5xx is not a refusal. */
const refusal = (e: unknown) => e instanceof ApiError && (e.status === 403 || e.status === 404 || e.status === 410);

const VIEWING_RETRY_MS = 15_000;

/** The link's viewer cap answered (409 LOCAL_SESSION_VIEWERS_FULL): the full state, with the count
 *  when the server gave one (a burst of opens past the link's ten-minute budget gives none). */
export function fullOf(e: unknown): { watching: number | null; note: string } | null {
  const body = e instanceof ApiError ? (e.body as { code?: string; details?: { watching?: unknown } } | null) : null;
  if (body?.code !== "LOCAL_SESSION_VIEWERS_FULL") return null;
  const watching = typeof body.details?.watching === "number" ? body.details.watching : null;
  return { watching, note: watching ? `Full: ${watching} people are watching. Try again in a minute.` : "This session is full. Try again in a minute." };
}
const noop = () => {};

/** An "Anyone with the link" session's live terminal, view only, with no sign-in (owner ruling
 *  2026-10-08). The page holds a 45 s seat it renews with the link's token, and one AppSync
 *  subscription whose only credential is that seat's lease. It gets screen frames and nothing else:
 *  no input route, no chat, no events, and no session or workspace id (frames carry `watchId`). */
function LinkTerminal({ token, onTrouble }: { token: string; onTrouble: () => void }) {
  const [seat, setSeat] = useState<{ view: SessionLinkViewing; client: AppSyncEventsClient } | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [liveChannels, setLiveChannels] = useState<readonly string[]>([]);
  // Bumped to take a seat again: after a failure, or when the link was made public anew (a new channel).
  const [round, setRound] = useState(0);
  const lease = useRef<string | null>(null);
  const fullSeen = useRef(false);
  const leaseOf = useRef(token);
  const handlers = useRef(new Set<(event: LiveEvent) => void>());
  // Whether this seat has drawn a snapshot. Until it has, every renewal asks for one: the server
  // grants an ask once per 15 s per link, so a viewer who joined just after someone else's would
  // otherwise look at a blank terminal until an idle Mac's own 30 s resync.
  const pictured = useRef(false);
  // AppSync lets a lease connect once and subscribe once (its authorizer marks it). So before the
  // socket connects again, the page asks for a seat it can connect with (`retake`): the server
  // renews this one if AppSync never took it, and trades it for a new one if it did.
  // `trading`: that request in flight; the socket's next connect and subscribe wait for it.
  const trading = useRef<Promise<void> | null>(null);
  // The lease the socket's last connect went out with: the same one is never offered to a second.
  const offered = useRef<string | null>(null);
  const connected = useRef(false);
  const viewing = useCallback(async (fresh?: boolean, retake?: boolean) => {
    const next = await publicPost<SessionLinkViewing>("/public/session-links/viewing",
      { token, ...(lease.current ? { lease: lease.current } : {}), ...(fresh ? { fresh: true } : {}), ...(retake ? { retake: true } : {}) }, null);
    lease.current = next.lease;
    return next;
  }, [token]);
  // Keeps a seat the socket can connect with while the channel is not live. Never while the tab is hidden.
  // One at a time: a second ask while the first is still on its way (a slow network, then the
  // 15 s tick or the tab shown again) would hand in the same used lease twice, and the server
  // would give a new seat for each.
  const reseat = useCallback((channel: string) => {
    if (document.visibilityState === "hidden" || trading.current) return;
    const trade: Promise<void> = viewing(false, true).then(
      (next) => { if (next.channel !== channel) setRound((n) => n + 1); },
      (e) => { if (e instanceof ApiError) onTrouble(); },
    ).then(() => { if (trading.current === trade) trading.current = null; });
    trading.current = trade;
  }, [viewing, onTrouble]);
  useEffect(() => {
    let alive = true;
    let client: AppSyncEventsClient | undefined;
    let retry: ReturnType<typeof setTimeout> | undefined;
    // A round on the same link keeps its lease: when the link moved to a new channel, the renewal
    // that said so already holds a seat there, and taking another would cost the channel two.
    if (leaseOf.current !== token) { lease.current = null; leaseOf.current = token; }
    pictured.current = false;
    trading.current = null;
    offered.current = null;
    connected.current = false;
    let wasLive = false;
    viewing().then((view) => {
      if (!alive) return;
      client = new AppSyncEventsClient({
        httpEndpoint: view.httpEndpoint, realtimeEndpoint: view.realtimeEndpoint,
        getToken: async () => {
          await trading.current;
          if (!connected.current) {
            // A connect. One that follows a failed connect would go out with the same lease, and
            // AppSync may have taken it: ask first (one request per attempt, on the socket's backoff).
            if (offered.current === lease.current) { reseat(view.channel); await trading.current; }
            offered.current = lease.current;
          }
          return lease.current;
        },
        onEvent: (event) => {
          if (event.type === "workspace.session.screen" && event.payload.kind === "snapshot") pictured.current = true;
          handlers.current.forEach((handler) => handler(event));
        },
        onStatus: (status) => {
          if (!alive) return;
          connected.current = status.connected;
          const liveNow = status.liveChannels.includes(view.channel);
          if (wasLive && !liveNow) reseat(view.channel); // dropped: the next connect or subscribe needs a new seat
          wasLive = liveNow;
          setLiveChannels(status.liveChannels);
        },
        // Refused: by a lapsed or used lease. One request per refusal, on the socket's own backoff.
        onError: (kind) => { if (alive && kind === "subscribe_error") reseat(view.channel); },
        uuid: () => crypto.randomUUID(),
      });
      setNote(null);
      setSeat({ view, client });
    }, (e) => {
      if (!alive) return;
      const full = fullOf(e);
      // Once per page load: a full link is retried every 15 s, and each retry is the same view.
      if (full && !fullSeen.current) { fullSeen.current = true; capture("share_link_full_viewed", { watching: full.watching }); }
      setNote(full ? full.note : e instanceof ApiError ? e.message : "Could not reach this session. Trying again.");
      if (!refusal(e)) retry = setTimeout(() => setRound((n) => n + 1), VIEWING_RETRY_MS); // a refused link is the page poll's to show
    });
    return () => { alive = false; clearTimeout(retry); client?.close(); setSeat(null); setLiveChannels([]); };
  }, [viewing, reseat, round, token]);
  const channel = seat?.view.channel;
  const watch = useCallback(async (_id: string, fresh?: boolean) => {
    await trading.current; // renew the seat a trade in flight is about to give, not the one it gives up
    try { if ((await viewing(fresh || !pictured.current)).channel !== channel) setRound((n) => n + 1); }
    // The seat was refused (narrowed, ended, offline): the page re-opens the link now and says which.
    catch (e) { if (e instanceof ApiError) onTrouble(); throw e; }
  }, [viewing, channel, onTrouble]);
  // Until the channel is live the terminal renews nothing (it asks for a picture only once it can
  // hear one), so the seat is kept here: AppSync refuses a connect or a subscribe made with a lapsed
  // lease, and the socket would retry with it for good (AppSync down past 45 s right after the seat
  // was taken, or a tab hidden that long). One request per 15 s, and at once when the tab is shown.
  const live = !!channel && liveChannels.includes(channel);
  useEffect(() => {
    if (!channel || live) return;
    const keep = () => reseat(channel);
    const timer = setInterval(keep, VIEWING_RETRY_MS);
    document.addEventListener("visibilitychange", keep);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", keep); };
  }, [channel, live, reseat]);
  const onEvent = useCallback((handler: (event: LiveEvent) => void) => { handlers.current.add(handler); return () => { handlers.current.delete(handler); }; }, []);
  const client = seat?.client;
  const claim = useCallback((wanted: string) => { client?.setChannels([wanted]); return () => client?.setChannels([]); }, [client]);
  const watchId = seat?.view.watchId;
  const session = useMemo(() => (watchId ? { sessionId: watchId, generation: watchId } : null), [watchId]);
  const frames = useMemo(() => (channel ? { channel, claim, liveChannels } : undefined), [channel, claim, liveChannels]);
  if (!session) return <section aria-label="Session state" className="session-state-panel rounded-lg border"><p role="status">{note ?? "Connecting…"}</p></section>;
  return <section aria-label="Live terminal" className="flex h-[70dvh] flex-col overflow-hidden rounded-lg border">
    <SessionTerminalView session={session} frames={frames} onEvent={onEvent} watch={watch} onLive={noop} admitted={false} />
  </section>;
}

function CodeLine({ code }: { code: SessionLinkCode }) {
  if (code.visibility === "public") return <p>{[code.remote, code.branch, code.commit?.slice(0, 7)].filter(Boolean).join(" · ")}</p>;
  if (code.visibility === "workspace") return <p>{code.remote}{code.branch ? ` on ${code.branch}` : ""}</p>;
  if (code.visibility === "private") return <p>Code is in a private repository</p>;
  return <p>Code not shared</p>;
}

/** A link that cannot be shown (incomplete, off, expired, not found): the page's own frame and
 *  centred panel, under the product name, instead of one loose line in the corner. */
function Unopened({ children }: { children: React.ReactNode }) {
  return <main className="mx-auto flex min-h-dvh max-w-3xl flex-col p-4">
    <header className="mb-4"><Link href="/" className="text-lg font-semibold">Lightbulb</Link></header>
    {/* A grid, so the panel keeps its own height in the middle of the viewport (its `flex: 1` would fill a flex parent). */}
    <div className="grid flex-1 content-center">
      <section aria-label="Session link" className="session-state-panel rounded-lg border">{children}</section>
    </div>
  </main>;
}

export default function SessionLinkPageRoute() {
  const router = useRouter();
  const api = useApi();
  const client = useMemo(() => createMessagingClient(api), [api]);
  const { isLoaded, isSignedIn, orgId, getToken } = useAuth();
  const { setActive } = useClerk();
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);
  const raw = useSyncExternalStore(onHash, readStash, () => null);
  const stash = useMemo<Stash | null>(() => { try { return raw ? JSON.parse(raw) as Stash : null; } catch { return null; } }, [raw]);
  const [page, setPage] = useState<SessionLinkPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The server refused the link (403, 404, 410), as opposed to a network or server failure.
  const [refused, setRefused] = useState(false);
  const [continueError, setContinueError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A ref, not an effect dependency: a getToken that changes identity would reopen the page each render.
  const reopen = useRef(noop);
  const reopenNow = useCallback(() => reopen.current(), []);
  const tokenOf = useRef(getToken);
  useEffect(() => { tokenOf.current = getToken; });

  // A layout effect, so the fragment leaves the URL before PostHogInit's passive effect reads
  // location.href (the analytics before_send and private-route rules are the second line).
  useLayoutEffect(() => {
    if (!window.location.hash) return;
    const now = readStash();
    try { window.sessionStorage.setItem(KEY, now); } catch { /* storage blocked: this visit only */ }
    window.history.replaceState(null, "", "/s");
  }, [raw]);

  useEffect(() => {
    if (!stash || !isLoaded) return;
    let alive = true;
    // A fresh Clerk token on each poll: they live about a minute, and the poll runs for as long as
    // the session is live.
    const bearerNow = async () => (isSignedIn ? await tokenOf.current() : null);
    let timer: ReturnType<typeof setTimeout> | undefined;
    let last: SessionLinkPage | undefined;
    let refusedAt = 0; // when the link was first refused; 0 while it opens
    // Each tick re-opens the link, so the header follows the session (live, ended, offline) and a
    // turned-off link shows its refusal; the poll stops once the session is not live. A refused
    // link is asked for again slowly, for a while (see REFUSED_POLL_MS), and the page comes back by
    // itself when it opens again. A network blip (not a refusal) just tries again next tick.
    const tick = async (first: boolean) => {
      try {
        const next = await publicPost<SessionLinkPage>("/public/session-links/open", { token: stash.token, ...(first ? {} : { refresh: true }) }, await bearerNow());
        if (!alive) return;
        // A sign-in reopens a link that was refused while signed out; so does the owner sharing it again.
        if (first || refusedAt) { setError(null); setRefused(false); }
        refusedAt = 0;
        setPage(last = next);
      } catch (e) {
        if (!alive) return;
        if (first || refusal(e)) {
          last = undefined;
          setRefused(refusal(e));
          if (refusal(e)) refusedAt ||= Date.now();
          setError(e instanceof Error ? e.message : "Could not open this link. Try again.");
        }
      }
      clearTimeout(timer);
      if (!alive) return;
      if (last?.state === "live") timer = setTimeout(() => void tick(false), watchingPage(last) ? WATCHING_POLL_MS : POLL_MS);
      else if (refusedAt && Date.now() - refusedAt < REFUSED_POLL_FOR_MS)
        timer = setTimeout(() => { if (alive && document.visibilityState !== "hidden") void tick(false); }, REFUSED_POLL_MS);
    };
    // Back on the tab after it was hidden through a retry: ask now, with a fresh ten minutes.
    const shown = () => {
      if (!alive || !refusedAt || document.visibilityState === "hidden") return;
      refusedAt = Date.now(); clearTimeout(timer); void tick(false);
    };
    document.addEventListener("visibilitychange", shown);
    reopen.current = () => { if (alive && last?.state === "live") { clearTimeout(timer); void tick(false); } };
    void tick(true);
    return () => { alive = false; clearTimeout(timer); document.removeEventListener("visibilitychange", shown); };
  }, [stash, isLoaded, isSignedIn]);

  // A workspace member lands in the app's own session view (Watch, Ask to join). Not in the CLI's
  // confirm mode: that visit is here to press Continue.
  const memberAt = page?.me.member && !stash?.port ? page.sessionId : undefined;
  useEffect(() => { if (memberAt) router.replace(sessionPermalink(memberAt, "")); }, [memberAt, router]);

  // T17 / D1: an outside viewer asks the owner to let them in. Allow makes them a member.
  const token = stash?.token;
  const ask = useCallback(async () => {
    if (!token) return;
    setAsking(true); setAskError(null);
    try {
      const { join } = await publicPost<SessionLinkJoinAsk>("/public/session-links/join-request", { token }, await tokenOf.current());
      setPage(p => p && { ...p, me: { ...p.me, canAsk: false, join: join === "member" ? undefined : join } });
      if (join === "member") reopen.current();
    } catch (e) {
      setAskError(e instanceof ApiError && e.message ? e.message : "Could not send that. Try again.");
    } finally { setAsking(false); }
  }, [token]);
  const canAsk = !!page?.me.canAsk;
  useEffect(() => {
    if (!isSignedIn || !canAsk) return;
    let waiting = false;
    try { waiting = window.sessionStorage.getItem(ASK_KEY) === "1"; window.sessionStorage.removeItem(ASK_KEY); } catch { /* storage blocked */ }
    if (waiting) void ask();
  }, [isSignedIn, canAsk, ask]);
  const openAllowed = async (sessionId: string, workspaceId?: string) => {
    if (workspaceId) await setActive({ organization: workspaceId });
    router.push(sessionPermalink(sessionId, ""));
  };

  const confirmContinue = async () => {
    if (!stash?.port || !stash.challenge) return;
    setBusy(true); setContinueError(null);
    try {
      const { code } = await client.createSessionCopy(stash.token, stash.challenge);
      try { window.sessionStorage.removeItem(KEY); } catch { /* storage blocked */ }
      go.to(`http://127.0.0.1:${stash.port}/done?code=${encodeURIComponent(code)}`);
    } catch (e) {
      // The server's own words: a turned-off link, or RATE_LIMITED's "Too many copies…".
      setContinueError(e instanceof ApiError && e.message ? e.message : "Could not make the copy. Try again.");
      setBusy(false);
    }
  };

  if (raw === "") return <Unopened><p role="alert" className="session-state-title">This link is incomplete. Ask for it again.</p></Unopened>;
  // A workspace link refuses a signed-out visitor exactly like a turned-off one, and the page must not
  // tell them apart. So a signed-out refusal leads with the one step that can work, in words true in
  // both cases (ISOA-732). The link stays in this tab's sessionStorage across the sign-in, and the
  // open above runs again once isSignedIn changes.
  if (error && refused && isLoaded && !isSignedIn && stash) return <Unopened>
    <p role="alert" className="session-state-title">Sign in to open this session.</p>
    <p>If it was shared with your workspace, it opens after you sign in. Otherwise this link was turned off.</p>
    <SignInButton mode="modal" forceRedirectUrl="/s"><Button type="button" className="mt-2">Sign in</Button></SignInButton>
  </Unopened>;
  // Signed in and still refused: a dead end, said plainly, with the one thing left to do.
  if (error) return <Unopened>
    <p role="alert" className="session-state-title">{error}</p>
    {refused && isSignedIn && <p>You are signed in, so this link was turned off or was shared with a workspace you are not in. Ask the person who sent it for a new link.</p>}
  </Unopened>;
  if (!page || !stash || memberAt) return <main className="mx-auto max-w-3xl p-4"><p role="status">Opening…</p></main>;
  const command = `lightbulb continue '${window.location.origin}/s#${stash.token}'`;
  const canContinue = page.provider === "claude" && page.continuable;
  const watching = watchingPage(page);
  return <main className={`mx-auto p-4 ${watching ? "max-w-5xl" : "max-w-3xl"}`}>
    <header className="mb-4">
      <h1 className="text-lg font-semibold">{page.title?.trim() || "A shared session"}</h1>
      <p className="text-sm text-muted-foreground">{page.ownerFirstName}&apos;s {sessionAgentName(page.provider)} session · {page.state === "live" ? "Live" : page.state === "ended" ? "Ended" : "Offline"}</p>
      <CodeLine code={page.code} />
      {page.me.member && page.sessionId && <a href={sessionPermalink(page.sessionId)}>Open in Lightbulb</a>}
    </header>
    {/* The outsider's doors (06 §2): Ask to join (T17, D1/D2) and Start your own workspace (T16), for
        every non-member, except in the CLI's confirm mode. Start your own is for people with no
        workspace, and not where Continue already offers "Create your free workspace". */}
    {!page.me.member && !stash.port && (page.me.canAsk || page.me.join || !(isSignedIn && (orgId || canContinue))) && <section aria-label="Join" className="mb-4 flex flex-col gap-2 rounded-lg border p-3">
      <p className="font-semibold">{page.ownerFirstName} is working with their team and AI employees in Lightbulb.</p>
      {page.me.join === "allowed" && page.sessionId ? <>
          <p role="status">You&apos;re in.</p>
          <Button type="button" className="self-start" onClick={() => void openAllowed(page.sessionId!, page.workspaceId)}>Open in Lightbulb</Button>
        </>
        : page.me.join === "pending" ? <p role="status">{page.ownerFirstName} has your request. This page lets you in when {page.ownerFirstName} says yes. You can keep watching.</p>
        : page.me.canAsk ? <>
          <p>Join {page.ownerFirstName}&apos;s workspace to type in this session{page.provider === "claude" ? ", pick it up on your Mac," : ""} or see the rest of the work.</p>
          {isSignedIn
            ? <Button type="button" className="self-start" disabled={asking} onClick={() => { capture("share_link_cta_clicked", { cta: "ask_to_join", signed_in: true }); void ask(); }}>Ask to join {page.ownerFirstName}&apos;s workspace</Button>
            : <SignUpButton mode="modal" forceRedirectUrl="/s"><Button type="button" className="self-start" onClick={() => {
                capture("share_link_cta_clicked", { cta: "ask_to_join", signed_in: false });
                try { window.sessionStorage.setItem(ASK_KEY, "1"); } catch { /* storage blocked: they ask again after signing up */ }
              }}>Ask to join</Button></SignUpButton>}
          {askError && <p role="alert">{askError}</p>}
        </> : null}
      {page.me.join !== "allowed" && !(isSignedIn && (orgId || canContinue)) && <div className="flex flex-wrap items-center gap-2">
        {(page.me.canAsk || page.me.join) && <span>Not on {page.ownerFirstName}&apos;s team?</span>}
        {isSignedIn
          ? <Button type="button" variant={page.me.canAsk ? "outline" : "default"} className="self-start" onClick={() => { capture("share_link_cta_clicked", { cta: "start_own", signed_in: true }); markWorkspaceDoor("session_link"); router.push("/onboarding"); }}>Start your own workspace</Button>
          : <SignUpButton mode="modal" forceRedirectUrl="/onboarding"><Button type="button" variant={page.me.canAsk ? "outline" : "default"} className="self-start" onClick={() => { capture("share_link_cta_clicked", { cta: "start_own", signed_in: false }); markWorkspaceDoor("session_link"); }}>Start your own workspace</Button></SignUpButton>}
      </div>}
    </section>}
    {canContinue && <section aria-label="Continue" className="mb-4 flex flex-col gap-2">
      {/* Signed out with no CLI waiting: the steps to continue, in order. `lightbulb continue` opens this
          page again (its confirm mode, `stash.port`), and that visit is where signing up happens. */}
      {!isSignedIn && stash.port ? <>
          <p>Sign up, free, to continue {page.title?.trim() || "this session"} in Claude Code on this computer.</p>
          <SignUpButton mode="modal" forceRedirectUrl="/s"><Button type="button">Sign up to continue</Button></SignUpButton>
        </>
        : isSignedIn && !orgId ? <Button type="button" onClick={() => { markWorkspaceDoor("session_link"); router.push(`/onboarding?returnTo=${encodeURIComponent("/s")}`); }}>Create your free workspace</Button>
        : stash.port ? <>
            <p>Continue {page.title?.trim() || "this session"} in Claude Code on this computer?</p>
            <Button type="button" disabled={busy} onClick={() => void confirmContinue()}>Continue in Claude Code</Button>
            {continueError && <p role="alert">{continueError}</p>}
          </>
        : <>
            <p>Continue {page.title?.trim() || "this session"} in your own Claude Code, with a copy of the conversation.</p>
            <p>Then run this in your terminal{isSignedIn ? "" : ". It opens this page again, where you sign up, free, and confirm"}:</p>
            <code className="font-mono text-xs">{command}</code>
            <Button type="button" variant="outline" size="sm" onClick={() => void copyText(command)}>Copy command</Button>
          </>}
    </section>}
    {watching ? <LinkTerminal token={stash.token} onTrouble={reopenNow} /> : <section aria-label="Session state" className="session-state-panel rounded-lg border">
      {/* An ended session shows no screen here: its Mac may still keep the final one, for teammates only. */}
      {page.state === "ended" ? <p className="font-semibold">Ended</p>
        : page.state === "offline" ? <p className="font-semibold">{page.ownerFirstName}&apos;s Mac is offline</p>
        : <p>This session is live in Lightbulb. Open it there to watch its terminal.</p>}
    </section>}
  </main>;
}
