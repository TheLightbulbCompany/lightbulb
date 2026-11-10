"use client";

import "@/components/messaging/messaging.css";
import "./mail.css";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, Check, Copy, Mail, RefreshCw } from "lucide-react";
import { useOrganization } from "@clerk/nextjs";
import { useSWRConfig } from "swr";
import { agentDisplayName, ApiError } from "@lightbulb/shared";

import { Button } from "@/components/ui/button";
import { useAgents } from "@/hooks/useAgents";
import { useBilling } from "@/hooks/useBilling";
import { useApi } from "@/lib/api";
import { navigateShell, replaceSearchParams } from "@/lib/shellNav";
import { buildPanelUrl, clearDurableParamFromUrl } from "@/components/workspace/urlChannel";
import { MailList } from "./MailList";
import { MailDetail } from "./MailDetail";
import { MeetingDetail } from "./MeetingDetail";
import {
  buildSections,
  isNew,
  MAIL_FILTERS,
  newestReceived,
  type MailEntry,
  type MailFilter,
} from "./mailModel";
import { advanceSeen, isTextLineKey, readSeen, useTeamMail, useTextLine } from "./useTeamMail";

const WIDE_PX = 840;

/** Opens an item (or the list) through the URL, so both shells and Back follow it. */
function openMailItem(item: string | null): void {
  navigateShell(buildPanelUrl("mail", item ? { item } : {}));
}

function useCopy(text: string | undefined) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    if (!text || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      return;
    }
    setCopied(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1500);
  };
  return { copied, copy };
}

// The buy door's 409/400 answers, by their `detail`.
const BUY_ERRORS: Record<string, string> = {
  apple_managed: "Not available on this plan",
  no_front_desk_agent: "This workspace has no Customer Operations agent",
};

/** Links the member's own phone by texting a code to the line, so a text from it posts to the main room as them. */
function MyPhone({ phone, number }: { phone: string | null | undefined; number: string }) {
  const api = useApi();
  const { mutate } = useSWRConfig();
  const [code, setCode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const getCode = async () => {
    setError(null);
    try {
      setCode(((await api.post("/agent-text/me/phone/code", {})) as { code: string }).code);
    } catch {
      setError("Could not get a code");
    }
  };
  const remove = async () => {
    setError(null);
    try {
      await api.del("/agent-text/me/phone");
      await mutate(isTextLineKey);
    } catch {
      setError("Could not remove");
    }
  };
  return <div className="mail-panel__myphone">
    {phone ? <>
      <span>Your phone: {phone}</span>
      <Button type="button" variant="ghost" size="sm" onClick={() => void remove()}>Remove</Button>
    </> : code ? <>
      <span>Text <strong>LINK {code}</strong> to {number} from your phone. The code works for 15 minutes.</span>
      <Button type="button" variant="ghost" size="sm" onClick={() => void mutate(isTextLineKey)}>Check</Button>
    </> : <Button type="button" variant="ghost" size="sm" onClick={() => void getCode()}>Link my phone</Button>}
    {error && <span role="status" className="mail-panel__number">{error}</span>}
  </div>;
}

function CopyButton({ text }: { text: string | undefined }) {
  const { copied, copy } = useCopy(text);
  return <Button type="button" variant="ghost" size="sm" aria-label={copied ? "Copied" : "Copy address"} onClick={() => void copy()}>
    {copied ? <><Check size={14} aria-hidden="true" /> Copied</> : <Copy size={14} aria-hidden="true" />}
  </Button>;
}

/** Wide when the panel itself is ≥ 840px: it docks, so the window size says nothing. */
function useWide(ref: React.RefObject<HTMLElement | null>): boolean {
  const [wide, setWide] = useState(false);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWide(el.getBoundingClientRect().width >= WIDE_PX);
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => setWide(entry.contentRect.width >= WIDE_PX));
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return wide;
}

export function MailPanel({ item, visible = true }: { item: string | null; visible?: boolean }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const wide = useWide(rootRef);
  const { orgId, inbox, noAddress, address, loadedAt: now } = useTeamMail();
  const { agents } = useAgents();
  const { account, refresh: refreshBilling } = useBilling();
  const line = useTextLine(orgId);
  // The number stopped being set up (it arrived, or none is coming): billing changed, so read it once.
  const wasPending = useRef(false);
  useEffect(() => {
    if (line.pending) wasPending.current = true;
    else if (wasPending.current) {
      wasPending.current = false;
      void refreshBilling();
    }
  }, [line.pending, refreshBilling]);
  const isAdmin = useOrganization().membership?.role === "org:admin";
  const api = useApi();
  const { mutate: mutateGlobal } = useSWRConfig();
  const [buying, setBuying] = useState(false);
  const [buyStatus, setBuyStatus] = useState<string | null>(null);
  const [priceGone, setPriceGone] = useState(false);
  const price = priceGone ? null : account?.front_desk_price_usd;
  const buyNumber = async () => {
    setBuying(true);
    setBuyStatus(null);
    try {
      const res = (await api.post("/billing/front-desk", { return_to: "web" })) as { status: string; url?: string };
      if (res.status === "checkout" && res.url) {
        window.location.assign(res.url);
        return; // leaving the page: stay disabled
      }
      await Promise.all([mutateGlobal(isTextLineKey), refreshBilling()]);
    } catch (e) {
      const body = e instanceof ApiError && typeof e.body === "object" && e.body !== null ? e.body : {};
      const detail = String((body as { detail?: unknown }).detail ?? "");
      if (detail === "front_desk_price_unconfigured") setPriceGone(true);
      else setBuyStatus(BUY_ERRORS[detail] ?? "Could not start checkout");
    }
    setBuying(false);
  };
  const [filter, setFilter] = useState<MailFilter>("all");

  const pages = inbox.data;
  const meetings = useMemo(() => pages?.[0]?.meetings ?? [], [pages]);
  const messages = useMemo(() => (pages ?? []).flatMap((p) => p.messages ?? []), [pages]);
  const hasMore = Boolean(pages?.[pages.length - 1]?.next);
  const teamAddress = pages?.[0]?.address ?? address.data?.address;
  const sections = useMemo(() => buildSections(meetings, messages, filter, now), [meetings, messages, filter, now]);
  const entries = useMemo(() => sections.flatMap((s) => s.entries), [sections]);
  const names = useMemo(() => new Map((agents ?? []).map((a) => [a.id, agentDisplayName(a)])), [agents]);
  const agentName = useCallback((id: string) => names.get(id) ?? null, [names]);

  // "New" is snapshotted when the panel opens, then the stored mark moves to the newest
  // received mail: rows that were new keep their dot until the panel closes.
  const [snapshot, setSnapshot] = useState<number | null | undefined>(undefined);
  const newest = newestReceived(messages);
  const ready = visible && pages !== undefined && Boolean(orgId);
  if (!visible && snapshot !== undefined) setSnapshot(undefined);
  // A first visit (no mark) snapshots the newest mail: nothing old is new, later arrivals are.
  if (ready && snapshot === undefined) setSnapshot(readSeen(orgId) ?? newest ?? now);
  useEffect(() => {
    if (!ready || !orgId) return;
    advanceSeen(orgId, newest);
  }, [ready, orgId, newest]);
  const newKeys = useMemo(
    () => new Set(entries.filter((e) => e.kind === "mail" && isNew(e.message, snapshot ?? null)).map((e) => e.key)),
    [entries, snapshot],
  );

  // The item is looked up across every loaded entry, so a filter chip never drops the selection.
  const allEntries = useMemo(
    () => (filter === "all" ? entries : buildSections(meetings, messages, "all", now).flatMap((s) => s.entries)),
    [filter, entries, meetings, messages, now],
  );
  // An unknown item (not in the loaded pages, or a stale link) is dropped for good, so a later
  // "Load more" does not pop a detail open; when the URL names Mail, the item leaves it too.
  const [dropped, setDropped] = useState<string | null>(null);
  const requested = item && item !== dropped ? allEntries.find((e) => e.key === item) ?? null : null;
  const unknownItem = Boolean(item) && pages !== undefined && requested === null;
  if (unknownItem && item !== dropped) setDropped(item);
  else if (dropped !== null && item !== dropped) setDropped(null); // the URL let go of it
  useEffect(() => {
    if (!unknownItem || !visible) return;
    const next = clearDurableParamFromUrl(window.location.search, "mail", "item");
    if (next !== null) replaceSearchParams(new URLSearchParams(next));
  }, [unknownItem, visible, item]);
  // Wide with no item: the newest dated row, kept while it exists so a live refresh does not
  // swap what is being read.
  const [autoKey, setAutoKey] = useState<string | null>(null);
  const kept = autoKey ? entries.find((e) => e.key === autoKey) ?? null : null;
  const fallback = wide && !requested
    ? kept ?? sections.find((s) => s.key.startsWith("day-"))?.entries[0] ?? entries[0] ?? null
    : null;
  if (fallback && fallback !== kept && fallback.key !== autoKey) setAutoKey(fallback.key);
  const selected: MailEntry | null = requested ?? fallback;

  // Narrow: opening a row moves focus to Back (the row unmounts, and Escape needs focus inside
  // the detail); Back (button, Escape, or the browser) lands focus on the row that opened it.
  const opener = useRef<string | null>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const showingDetail = !wide && requested !== null;
  useEffect(() => {
    if (!opener.current) return;
    if (showingDetail) {
      backRef.current?.focus();
      return;
    }
    const key = opener.current;
    opener.current = null;
    rootRef.current?.querySelector<HTMLElement>(`[data-mail-key="${CSS.escape(key)}"]`)?.focus();
  }, [showingDetail]);
  const open = (key: string) => {
    if (key === dropped) setDropped(null); // opening it on purpose (e.g. after "Load more") works
    if (!wide) opener.current = key;
    openMailItem(key);
  };
  const back = () => openMailItem(null);

  const header = <header className="mail-panel__header">
    <h2>Mail</h2>
    {teamAddress && <span className="mail-panel__address">
      <span className="mail-panel__address-text">{teamAddress}</span>
      <CopyButton text={teamAddress} />
    </span>}
    {line.data?.number && <span className="mail-panel__number">{line.data.number}</span>}
    {isAdmin && line.data === null && (line.pending || account?.front_desk
      ? <span role="status" className="mail-panel__number">Setting up your number…</span>
      : typeof price === "number" && <>
      <Button type="button" variant="ghost" size="sm" disabled={buying} onClick={() => void buyNumber()}>
        {`Get a phone number · $${price}/month`}
      </Button>
      {buyStatus && <span role="status" className="mail-panel__number">{buyStatus}</span>}
    </>)}
    <span className="mail-panel__actions">
      <button type="button" className="mail-link" onClick={() => navigateShell(buildPanelUrl("agents", {}))}>Settings</button>
      <Button type="button" variant="ghost" size="sm" aria-label="Refresh mail" disabled={inbox.isValidating}
        onClick={() => void inbox.mutate()}>
        <RefreshCw size={14} aria-hidden="true" />
      </Button>
    </span>
  </header>;

  let body: React.ReactNode;
  const addressFailed = address.error && !(address.error instanceof ApiError && address.error.status === 409);
  if (noAddress && !addressFailed) {
    body = address.data
      ? <MailEmpty address={address.data.address} />
      : <p role="status" className="mail-panel__message">Your workspace address is being set up…</p>;
  } else if ((inbox.error && !pages) || addressFailed) {
    body = <div role="alert" className="mail-panel__message">
      <p>Couldn&apos;t load mail.</p>
      <Button type="button" variant="outline" size="sm"
        onClick={() => void (addressFailed ? address.mutate() : inbox.mutate())}>Retry</Button>
    </div>;
  } else if (!pages) {
    body = <div className="mail-panel__skeleton" aria-busy="true" aria-label="Loading mail">
      {Array.from({ length: 6 }, (_, i) => <div key={i} className="room-skeleton" aria-hidden="true"><span /><span /></div>)}
    </div>;
  } else if (meetings.length === 0 && messages.length === 0) {
    body = <MailEmpty address={teamAddress} />;
  } else {
    const list = <div className="mail-list">
      <div className="mail-chips" role="toolbar" aria-label="Filter mail">
        {MAIL_FILTERS.map((chip) => <button key={chip.id} type="button" className="mail-chip"
          aria-pressed={filter === chip.id} onClick={() => setFilter(chip.id)}>{chip.label}</button>)}
      </div>
      <div className="mail-list__scroll">
        {sections.length === 0
          ? <p className="mail-panel__message">{filter === "meetings" ? "No meetings yet." : "No mail yet."}</p>
          : <MailList sections={sections} selectedKey={selected?.key ?? null} newKeys={newKeys}
            agentName={agentName} onOpen={open} now={now} />}
        {hasMore && filter !== "meetings" && <div className="mail-list__more">
          <Button type="button" variant="ghost" size="sm" disabled={inbox.isValidating}
            onClick={() => void inbox.setSize((s) => s + 1)}>Load more</Button>
        </div>}
      </div>
    </div>;
    const detail = selected && <div className="mail-detail-pane"
      onKeyDown={(e) => {
        if (wide || e.key !== "Escape") return;
        // Escape in a filled Find field clears the search (the browser's own behaviour), not the detail.
        if (e.target instanceof HTMLInputElement && e.target.value) return;
        e.preventDefault();
        back();
      }}>
      {!wide && <button ref={backRef} type="button" className="mail-back" onClick={back}><ArrowLeft size={16} aria-hidden="true" /> Mail</button>}
      {selected.kind === "mail"
        ? <MailDetail key={selected.key} message={selected.message} at={selected.at} orgId={orgId}
          agentName={selected.message.agent_id ? agentName(selected.message.agent_id) : null} />
        : <MeetingDetail key={selected.key} entry={selected} orgId={orgId} now={now} onOpen={open}
          onOpenNotes={(room) => navigateShell(buildPanelUrl("chat", { room }))} />}
    </div>;
    body = wide
      ? <div className="mail-panel__split">{list}{detail}</div>
      : showingDetail ? detail : list;
  }

  return <section ref={rootRef} className="mail-panel" aria-label="Mail" data-wide={wide || undefined}>
    {header}
    {line.data?.number && <MyPhone key={line.data.myPhone ?? ""} phone={line.data.myPhone} number={line.data.number} />}
    {body}
  </section>;
}

function MailEmpty({ address }: { address: string | undefined }) {
  return <div className="mail-empty">
    <Mail size={28} aria-hidden="true" className="mail-empty__icon" />
    {address && <p className="mail-empty__address">
      <span>{address}</span>
      <CopyButton text={address} />
    </p>}
    <p className="mail-empty__text">
      Mail to this address goes to your agents. Add it to a calendar event and they join the meeting.
    </p>
  </div>;
}
