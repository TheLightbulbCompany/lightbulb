"use client";

import { useState } from "react";
import { Trash2 } from "lucide-react";
import { ApiError } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

/**
 * Remove sessions from the list, after one confirmation. `name` is one session's title; without it, all of the
 * person's own ended ones. `live`: that session is running, and removing it ends it on its Mac first.
 */
export function SessionRemoveButton({ name, onRemove, icon = false, live = false }: {
  name?: string; onRemove: () => Promise<unknown>; icon?: boolean; live?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const label = !name ? "Remove my ended sessions" : live ? `End and remove ${name}` : `Remove ${name} from the list`;
  const remove = async () => {
    if (saving) return;
    setSaving(true); setError("");
    try { await onRemove(); setOpen(false); }
    // The server's own sentence when it refused (the Mac went offline, the session started again).
    catch (e) { setError(e instanceof ApiError && e.status === 409 && e.message ? e.message : "Could not remove it. Try again."); }
    finally { setSaving(false); }
  };
  return <>
    <Button type="button" variant="ghost" size={icon ? "icon-sm" : "sm"} className="session-rename-trigger" aria-label={label} title={label}
      onClick={() => { setError(""); setOpen(true); }}>
      <Trash2 aria-hidden="true" />{!icon && (name ? "Remove from list" : "Remove my ended sessions")}
    </Button>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>{!name ? "Remove your ended sessions?" : live ? "End and remove this session?" : "Remove this session from the list?"}</DialogTitle>
          <DialogDescription>
            {!name ? "Every ended session of yours is removed for everyone in the workspace."
              : live ? "It stops on your Mac, and is removed for everyone in the workspace." : "It is removed for everyone in the workspace."}
            {" "}Shared links stop working, and it cannot be resumed from Lightbulb. Nothing on your Mac is deleted.
          </DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-sm text-err">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={saving} onClick={() => setOpen(false)}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={saving} onClick={() => void remove()}>{saving ? "Removing…" : live ? "End and remove" : "Remove"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </>;
}
