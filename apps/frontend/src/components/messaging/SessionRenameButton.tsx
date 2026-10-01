"use client";

import { useState } from "react";
import { Pencil } from "lucide-react";
import { sessionTitle, type LocalSession } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

export function SessionRenameButton({ session, onRename, icon = false }: {
  session: LocalSession;
  onRename: (sessionId: string, title: string | null) => Promise<unknown>;
  icon?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const show = () => { setTitle(session.displayTitle ?? session.title ?? ""); setError(""); setOpen(true); };
  const save = async (reset = false) => {
    const next = title.trim();
    if ((!reset && (!next || next.length > 120)) || saving) return;
    setSaving(true); setError("");
    try { await onRename(session.sessionId, reset ? null : next); setOpen(false); }
    catch { setError("Could not rename this session. Try again."); }
    finally { setSaving(false); }
  };
  return <>
    <Button type="button" variant="ghost" size={icon ? "icon-sm" : "sm"} className="session-rename-trigger" aria-label={`Rename ${sessionTitle(session)}`} title={`Rename ${sessionTitle(session)}`} onClick={show}>
      <Pencil aria-hidden="true" />{!icon && "Rename"}
    </Button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader><DialogTitle>Rename session</DialogTitle><DialogDescription>This name appears anywhere the session is shared in Lightbulb.</DialogDescription></DialogHeader>
        <form onSubmit={event => { event.preventDefault(); void save(); }}>
          <label htmlFor={`rename-${session.sessionId}`} className="text-sm font-medium">Session name</label>
          <input id={`rename-${session.sessionId}`} className="room-input mt-2 w-full" value={title} maxLength={120} autoFocus disabled={saving} onChange={event => setTitle(event.target.value)} />
          {error && <p role="alert" className="mt-2 text-sm text-err">{error}</p>}
          <DialogFooter className="mt-4">{session.displayTitle && <Button type="button" variant="ghost" disabled={saving} onClick={() => void save(true)}>Use original title</Button>}
            <Button type="button" variant="outline" disabled={saving} onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" disabled={saving || !title.trim() || title.trim() === sessionTitle(session)}>{saving ? "Saving…" : "Save"}</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  </>;
}
