"use client";

// Admin-only settings for one app: who can open it, invites, monthly budget, database console, removal.
// The backend re-enforces admin with 403; hiding this for members is only a courtesy.
import { useState } from "react";
import { notifyError } from "@/lib/notify";
import { closePendingTab, navigatePendingTab, openPendingTab } from "@/components/settings/openInNewTab";
import { useApps, type AppRow } from "@/hooks/useApps";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import { PublishButton } from "./PublishButton";
import "./canvas.css";

const CHOICES: { value: AppRow["open_to"]; label: string }[] = [
  { value: "room", label: "This room" },
  { value: "workspace", label: "Everyone in the workspace" },
  { value: "invited", label: "People I invite" },
  { value: "public", label: "Anyone with the link" },
];

type OpenTo = AppRow["open_to"];

// Who stops being able to open the app if it moves from `from` to `to`; null when nobody does (widening).
function whoLosesAccess(from: OpenTo, to: OpenTo, room: string): string | null {
  if (from === to || to === "public" || (from === "room" && to === "workspace")) return null;
  if (from === "public") {
    const outside = to === "room" ? `outside ${room}` : to === "workspace" ? "outside the workspace" : "you haven't invited";
    return `Anyone with the link ${outside} will lose access.`;
  }
  if (from === "invited") return "People you invited from outside will lose access.";
  if (to === "room") return `Everyone in the workspace outside ${room} will lose access.`;
  return `${from === "room" ? `People in ${room}` : "Everyone in the workspace"} will lose access unless you invite them.`;
}

export function AppSettings({ app, onClose, onRemoved }: { app: AppRow; onClose(): void; onRemoved?(): void }) {
  const { update, invite, remove, databaseLink } = useApps();
  const isAdmin = useIsWorkspaceAdmin();
  const [openTo, setOpenTo] = useState(app.open_to);
  const [pending, setPending] = useState<OpenTo | null>(null);
  const [emails, setEmails] = useState("");
  const [budget, setBudget] = useState(String(app.budget_usd));
  const [removing, setRemoving] = useState(false);
  const [typed, setTyped] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const hasDb = (app.resources ?? []).some((r) => r.provider === "neon" && r.resource_id);
  const budgetNum = Number(budget);
  const budgetOk = budget.trim() !== "" && Number.isInteger(budgetNum) && budgetNum >= 0;
  if (!isAdmin) return null;

  // The hook already surfaces API errors; these handlers only keep the panel usable.
  const room = `#${app.room_name ?? "this room"}`;
  const warning = pending && whoLosesAccess(openTo, pending, room);
  const pick = (value: OpenTo) => {
    if (value === openTo) { setPending(null); return; }
    if (whoLosesAccess(openTo, value, room)) setPending(value); else { setPending(null); choose(value); }
  };
  const choose = async (value: OpenTo) => {
    const before = openTo;
    setOpenTo(value);
    try { await update(app.app_id, { open_to: value }); } catch { setOpenTo(before); }
  };
  const sendInvites = async () => {
    const list = emails.split(/[\s,;]+/).filter(Boolean);
    if (!list.length) return;
    try { await invite(app.app_id, list); setEmails(""); setNote(`Invited ${list.length}.`); } catch { /* shown by the hook */ }
  };
  const saveBudget = async () => {
    try { await update(app.app_id, { budget_usd: budgetNum }); setNote("Budget saved."); } catch { /* shown by the hook */ }
  };
  // The tab opens synchronously in the click (Safari blocks a window.open after an await).
  const openData = async () => {
    const w = openPendingTab();
    if (!w) { notifyError("Your browser blocked the new tab — allow popups for this site and try again."); return; }
    try {
      const url = await databaseLink(app);
      if (url) navigatePendingTab(w, url);
      else { closePendingTab(w); notifyError("This provider doesn't offer a console link right now."); }
    } catch { closePendingTab(w); }
  };
  const doRemove = async () => {
    try { await remove(app.app_id, typed); (onRemoved ?? onClose)(); } catch { /* shown by the hook */ }
  };

  return <section className="app-settings" aria-label={`${app.name} settings`}>
    <div className="app-settings__head">
      <h3 className="canvas__heading">Settings</h3>
      <button type="button" className="app-view__btn" onClick={onClose}>Close</button>
    </div>
    {note && <p role="status" className="canvas__notice">{note}</p>}

    <fieldset className="app-settings__group">
      <legend className="canvas__heading">Who can open it</legend>
      {CHOICES.map((c) => <label key={c.value} className="app-settings__row">
        <input type="radio" name={`open-to-${app.app_id}`} checked={(pending ?? openTo) === c.value} onChange={() => pick(c.value)} />
        {c.label}
      </label>)}
      {pending && warning && <div role="alert" className="app-settings__row">
        <p className="canvas__notice">{warning}</p>
        <button type="button" className="app-view__btn" onClick={() => { const v = pending; setPending(null); choose(v); }}>Change who can open it</button>
        <button type="button" className="app-view__btn" onClick={() => setPending(null)}>Cancel</button>
      </div>}
      {openTo === "invited" && <div className="app-settings__row">
        <label htmlFor={`emails-${app.app_id}`}>Email addresses</label>
        <textarea id={`emails-${app.app_id}`} value={emails} rows={3} onChange={(e) => setEmails(e.target.value)} />
        <button type="button" className="app-view__btn" disabled={!emails.trim()} onClick={sendInvites}>Invite</button>
      </div>}
    </fieldset>

    <div className="app-settings__group">
      <label htmlFor={`budget-${app.app_id}`}>Monthly budget (USD)</label>
      <input id={`budget-${app.app_id}`} type="number" min={0} step={1} value={budget} onChange={(e) => setBudget(e.target.value)} />
      <button type="button" className="app-view__btn" disabled={!budgetOk} onClick={saveBudget}>Save budget</button>
    </div>

    <div className="app-settings__group"><PublishButton key={app.app_id} app={app} /></div>

    <div className="app-settings__group">
      <button type="button" className="app-view__btn" disabled={!hasDb} onClick={openData}>Data and backups</button>
      {!hasDb && <span className="canvas__notice">This app doesn&apos;t store data yet.</span>}
    </div>

    <div className="app-settings__group">
      {!removing
        ? <button type="button" className="app-view__btn" onClick={() => setRemoving(true)}>Remove app…</button>
        : <>
          <label htmlFor={`confirm-${app.app_id}`}>Type the app name to confirm</label>
          <input id={`confirm-${app.app_id}`} value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          <button type="button" className="app-view__btn" disabled={typed !== app.name} onClick={doRemove}>Remove {app.name}</button>
        </>}
    </div>
  </section>;
}
