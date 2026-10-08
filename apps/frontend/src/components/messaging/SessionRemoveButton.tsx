"use client";

import { useState } from "react";
import { Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

/** Remove ended sessions from the list, after one confirmation. `name` is one session's title; without it, all of the person's own. */
export function SessionRemoveButton({ name, onRemove, icon = false }: {
  name?: string; onRemove: () => Promise<unknown>; icon?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const label = name ? `Remove ${name} from the list` : "Remove my ended sessions";
  const remove = async () => {
    if (saving) return;
    setSaving(true); setError("");
    try { await onRemove(); setOpen(false); }
    catch { setError("Could not remove it. Try again."); }
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
          <DialogTitle>{name ? "Remove this session from the list?" : "Remove your ended sessions?"}</DialogTitle>
          <DialogDescription>
            {name ? "It is removed for everyone in the workspace." : "Every ended session of yours is removed for everyone in the workspace."}
            {" "}Shared links stop working, and it cannot be resumed from Lightbulb. Nothing on your Mac is deleted.
          </DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-sm text-err">{error}</p>}
        <DialogFooter>
          <Button type="button" variant="outline" disabled={saving} onClick={() => setOpen(false)}>Cancel</Button>
          <Button type="button" variant="destructive" disabled={saving} onClick={() => void remove()}>{saving ? "Removing…" : "Remove"}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </>;
}
