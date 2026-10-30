"use client";

// Admin-only settings for one app: who can open it, invites, monthly budget, database console, removal.
// The backend re-enforces admin with 403; hiding this for members is only a courtesy.
import { useState } from "react";
import { notifyError } from "@/lib/notify";
import { closePendingTab, navigatePendingTab, openPendingTab } from "@/components/settings/openInNewTab";
import { useApps, type AppRow } from "@/hooks/useApps";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import "./canvas.css";

const CHOICES: { value: AppRow["open_to"]; label: string }[] = [
  { value: "room", label: "This room" },
  { value: "workspace", label: "Everyone in the workspace" },
  { value: "invited", label: "People I invite" },
  { value: "public", label: "Anyone with the link" },
];

export function AppSettings({ app, onClose, onRemoved }: { app: AppRow; onClose(): void; onRemoved?(): void }) {
  const { update, invite, remove, databaseLink } = useApps();
  const isAdmin = useIsWorkspaceAdmin();
  const [openTo, setOpenTo] = useState(app.open_to);
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
  const choose = async (value: AppRow["open_to"]) => {
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
        <input type="radio" name={`open-to-${app.app_id}`} checked={openTo === c.value} onChange={() => choose(c.value)} />
        {c.label}
      </label>)}
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

    <div className="app-settings__group">
      <button type="button" className="app-view__btn" disabled={!hasDb} onClick={openData}>Data and backups</button>
      {!hasDb && <span className="canvas__notice">This app doesn't store data yet.</span>}
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
