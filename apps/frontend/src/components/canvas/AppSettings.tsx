"use client";

// Settings for the creator (can_publish) or a workspace admin for one app: who can open it, invites, monthly budget, database console, removal.
// The backend re-enforces who may change these with 403; hiding this for others is only a courtesy.
import { useEffect, useState } from "react";
import { notifyError, notifySuccess } from "@/lib/notify";
import { closePendingTab, navigatePendingTab, openPendingTab } from "@/components/settings/openInNewTab";
import { useApps, type AppRow } from "@/hooks/useApps";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { PublishButton, type PublishCtl } from "./PublishButton";
import { LiveLink, Problem, displayName } from "./appText";
import "./canvas.css";

const CHOICES: { value: AppRow["open_to"]; label: string }[] = [
  { value: "me", label: "Only me (and workspace admins)" },
  { value: "room", label: "" }, // "#room (this room)", filled in from the app; left out when it came from no room
  { value: "workspace", label: "Everyone in the workspace" },
  { value: "invited", label: "People I invite" },
  { value: "public", label: "Anyone with the link" },
];

type OpenTo = AppRow["open_to"];

// Who stops being able to open the app if it moves from `from` to `to`; null when nobody does (widening).
function whoLosesAccess(from: OpenTo, to: OpenTo, room: string): string | null {
  if (from === to || to === "public" || from === "me" || (from === "room" && to === "workspace")) return null;
  if (to === "me") return "Only you and workspace admins will be able to open it.";
  if (from === "public") {
    const outside = to === "room" ? `outside ${room}` : to === "workspace" ? "outside the workspace" : "you haven't invited";
    return `Anyone with the link ${outside} will lose access.`;
  }
  if (from === "invited") return "People you invited from outside will lose access.";
  if (to === "room") return `Everyone in the workspace outside ${room} will lose access.`;
  return `${from === "room" ? `People in ${room}` : "Everyone in the workspace"} will lose access unless you invite them.`;
}

export function AppSettings({ app, onClose, onRemoved, publishCtl }: { app: AppRow; onClose(): void; onRemoved?(): void; publishCtl?: PublishCtl }) {
  const { update, invite, listInvites, revokeInvite, remove, databaseLink } = useApps();
  const isAdmin = useIsWorkspaceAdmin();
  const [openTo, setOpenTo] = useState(app.open_to);
  const [pending, setPending] = useState<OpenTo | null>(null);
  const [emails, setEmails] = useState("");
  const [budget, setBudget] = useState(String(app.budget_usd));
  const [removing, setRemoving] = useState(false);
  const [typed, setTyped] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [dataErr, setDataErr] = useState<string | undefined>(undefined);
  const dn = displayName(app);
  const [noLink, setNoLink] = useState(false); // the dashboard-link route returned no url for this app
  const hasDb = (app.resources ?? []).some((r) => r.provider?.toLowerCase() === "neon" && r.resource_id);
  const budgetNum = Number(budget);
  const budgetOk = budget.trim() !== "" && Number.isInteger(budgetNum) && budgetNum >= 0;
  const canManage = app.can_publish === true || isAdmin;
  const [invited, setInvited] = useState<string[] | "failed">([]);
  const loadInvited = () => listInvites(app.app_id).then(setInvited, () => setInvited("failed"));
  useEffect(() => {
    if (canManage) void loadInvited();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- loadInvited is a fresh closure each render; the app is the trigger
  }, [app.app_id, canManage]);
  if (!canManage) return null;

  // The hook already surfaces API errors; these handlers only keep the panel usable.
  const room = `#${app.room_name ?? "this room"}`;
  const warning = pending === "public"
    ? `Anyone with the link will be able to open it, including people outside the workspace, without signing in.${hasDb ? " They can see and submit what the app shows them." : ""}`
    : pending && whoLosesAccess(openTo, pending, room);
  const pick = (value: OpenTo) => {
    if (value === openTo) { setPending(null); return; }
    if (value === "public" || whoLosesAccess(openTo, value, room)) setPending(value); else { setPending(null); choose(value); }
  };
  const choose = async (value: OpenTo) => {
    const before = openTo;
    setOpenTo(value);
    try { await update(app.app_id, { open_to: value }); } catch { setOpenTo(before); }
  };
  const sendInvites = async () => {
    const list = emails.split(/[\s,;]+/).filter(Boolean);
    if (!list.length) return;
    try { await invite(app.app_id, list); setEmails(""); setNote(`Sent ${list.length} ${list.length === 1 ? "invite" : "invites"}.`); void loadInvited(); } catch { /* shown by the hook */ }
  };
  const revoke = async (email: string) => {
    try {
      await revokeInvite(app.app_id, email);
      setInvited((now) => (now === "failed" ? now : now.filter((e) => e !== email)));
      setNote(`${email}'s invite link no longer works. If they already opened it, they keep access for up to a day.`);
    } catch { /* shown by the hook */ }
  };
  const saveBudget = async () => {
    try { await update(app.app_id, { budget_usd: budgetNum }); setNote("Spending limit saved."); } catch { /* shown by the hook */ }
  };
  // The tab opens synchronously in the click (Safari blocks a window.open after an await).
  const openData = async () => {
    setDataErr(undefined);
    const w = openPendingTab();
    if (!w) { notifyError("Your browser blocked the new tab — allow popups for this site and try again."); return; }
    try {
      const url = await databaseLink(app);
      if (url) navigatePendingTab(w, url);
      else { closePendingTab(w); setNoLink(true); }
    } catch (err) { closePendingTab(w); setDataErr((err as Error)?.message ?? ""); }
  };
  const doRemove = async () => {
    try { await remove(app.app_id, dn); notifySuccess(`Removed ${dn}.`); (onRemoved ?? onClose)(); } catch { /* shown by the hook */ }
  };

  return <section className="app-settings" aria-label={`${dn} settings`}>
    <div className="app-settings__head">
      <h3 className="canvas__heading">Settings</h3>
      <button type="button" className="app-view__btn" onClick={onClose}>Close</button>
    </div>
    {note && <p role="status" className="canvas__notice">{note}</p>}

    <fieldset className="app-settings__group">
      <legend className="canvas__heading">Who can open it</legend>
      {CHOICES.filter((c) => c.value !== "room" || app.room_id || openTo === "room").map((c) => <label key={c.value} className="app-settings__row">
        <input type="radio" name={`open-to-${app.app_id}`} checked={(pending ?? openTo) === c.value} onChange={() => pick(c.value)} />
        {c.value === "room" ? `${room} (this room)` : c.label}
      </label>)}
      <div className="app-settings__row app-settings__row--sub"><button type="button" className="app-view__btn" disabled>Use your own domain (coming soon)</button></div>
      {pending && warning && <div role="alert" className="app-settings__row">
        <p className="canvas__notice">{warning}</p>
        <button type="button" className="app-view__btn" onClick={() => { const v = pending; setPending(null); choose(v); }}>{pending === "public" ? "Make it public" : "Change who can open it"}</button>
        <button type="button" className="app-view__btn" onClick={() => setPending(null)}>Cancel</button>
      </div>}
      {openTo === "public" && <div className="app-settings__row">
        {app.live_url ? <LiveLink url={app.live_url} /> : <span className="canvas__notice">Publish it to get the link.</span>}
      </div>}
      {openTo === "invited" && <div className="app-settings__row">
        <label htmlFor={`emails-${app.app_id}`}>Email addresses (one per line or separated by commas)</label>
        <span className="canvas__notice">Each person gets an email with a link to {dn} only. No Lightbulb account needed.</span>
        <textarea id={`emails-${app.app_id}`} value={emails} rows={3} onChange={(e) => setEmails(e.target.value)} />
        <button type="button" className="app-view__btn" disabled={!emails.trim()} onClick={sendInvites}>Invite</button>
      </div>}
      {invited === "failed"
        ? <Problem action={<button type="button" className="app-view__btn" onClick={() => void loadInvited()}>Try again</button>}>We couldn&apos;t load who&apos;s invited.</Problem>
        : invited.length > 0 && <ul className="app-settings__invited" aria-label="Invited people">
          {invited.map((email) => <li key={email} className="app-settings__row">
            <span>{email}</span> · <button type="button" className="app-view__btn" aria-label={`Remove ${email}`} onClick={() => revoke(email)}>Remove</button>
          </li>)}
        </ul>}
    </fieldset>

    <div className="app-settings__group">
      <label htmlFor={`budget-${app.app_id}`}>Monthly spending limit ($)</label>
      <span className="canvas__notice">What {dn} may spend each month on hosting and data. Spent so far: {app.spend_month_cents == null ? "not yet known" : `$${(app.spend_month_cents / 100).toFixed(2)}`}.</span>
      <input id={`budget-${app.app_id}`} type="number" min={0} step={1} value={budget} onChange={(e) => setBudget(e.target.value)} />
      <button type="button" className="app-view__btn" disabled={!budgetOk} onClick={saveBudget}>Save limit</button>
    </div>

    <div className="app-settings__group"><PublishButton app={app} ctl={publishCtl} /></div>

    <div className="app-settings__group">
      {!noLink && <button type="button" className="app-view__btn" disabled={!hasDb} onClick={openData}>Data and backups ↗</button>}
      {noLink
        ? <span className="canvas__notice">{dn}&apos;s data is kept in Neon.</span>
        : hasDb
        ? <span className="canvas__notice">Opens where {dn}&apos;s data is kept (Neon). Restoring a backup replaces today&apos;s data.</span>
        : <span className="canvas__notice">This app doesn&apos;t store data yet.</span>}
      {typeof dataErr === "string" && <Problem raw={dataErr}>We couldn&apos;t open the data page. Try again.</Problem>}
    </div>

    <div className="app-settings__group">
      {!removing
        ? <button type="button" className="app-view__btn" onClick={() => setRemoving(true)}>Remove app…</button>
        : <>
          <p className="canvas__notice">Removing {dn} takes it offline for everyone and deletes its data. This can&apos;t be undone.</p>
          <label htmlFor={`confirm-${app.app_id}`}>Type {dn} to confirm</label>
          <input id={`confirm-${app.app_id}`} value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          <span className="publish">
            <button type="button" className="app-view__btn app-view__btn--danger" disabled={typed.trim().toLowerCase() !== dn.toLowerCase()} onClick={doRemove}>Remove {dn}</button>
            <button type="button" className="app-view__btn" onClick={() => { setRemoving(false); setTyped(""); }}>Cancel</button>
          </span>
        </>}
    </div>
  </section>;
}
