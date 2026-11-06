"use client";

// New project (UX slice 6): a name with its tile picker inside the field, and an optional goal that becomes the
// project's Instructions. Home's New project and the sidebar's + › New project both open it. It is pinned on create
// (MessagingWorkspaceContext.createRoom). Context and "who's in it" are skipped until someone asks for them.

import { useId, useRef, useState, type ReactElement } from "react";
import { ROOM_TOPIC_MAX_CHARS, type ProjectColor, type ProjectIcon } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import type { ConversationCreation } from "@/components/messaging/RoomDirectory";
import { ProjectIconPicker } from "./ProjectTile";

export function NewProjectDialog({ open, onOpenChange, onCreate, busy = false, error }: {
  open: boolean; onOpenChange: (open: boolean) => void;
  /** False (or a throw) keeps the dialog open with what was typed. */
  onCreate: (creation: ConversationCreation) => void | Promise<boolean>;
  busy?: boolean; error?: string;
}): ReactElement {
  const id = useId();
  const nameRef = useRef<HTMLInputElement>(null);
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [tile, setTile] = useState<{ icon?: ProjectIcon | null; color?: ProjectColor | null }>({});
  const reset = () => { setName(""); setGoal(""); setTile({}); };
  const submit = async () => {
    if (!name.trim() || busy) return;
    const created = await onCreate({ kind: "channel", name: name.trim(), members: [], topic: goal.trim() || null,
      icon: tile.icon ?? null, color: tile.color ?? null });
    if (created !== false) { onOpenChange(false); reset(); }
  };
  return <Dialog open={open} onOpenChange={(next) => { if (!busy) onOpenChange(next); }}>
    <DialogContent className="project-new" showCloseButton={!busy}
      onOpenAutoFocus={(event) => { event.preventDefault(); nameRef.current?.focus(); }}>
      <DialogHeader>
        <DialogTitle>New project</DialogTitle>
        <DialogDescription className="sr-only">A project is where you and Lightbulb work on one thing.</DialogDescription>
      </DialogHeader>
      <form className="grid gap-4" aria-busy={busy} onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <div className="grid gap-1.5">
          <label htmlFor={`${id}-name`} className="text-ui font-medium">Name</label>
          <div className="project-field">
            <ProjectIconPicker icon={tile.icon} color={tile.color} name={name} disabled={busy} onChange={setTile} />
            <input ref={nameRef} id={`${id}-name`} value={name} maxLength={80} disabled={busy} autoComplete="off"
              placeholder="Spring sale launch" onChange={(event) => setName(event.target.value)} />
          </div>
        </div>
        <div className="grid gap-1.5">
          <label htmlFor={`${id}-goal`} className="text-ui font-medium">What should this project get done? <span className="text-muted-foreground font-normal">(optional)</span></label>
          <textarea id={`${id}-goal`} className="project-textarea" value={goal} maxLength={ROOM_TOPIC_MAX_CHARS} disabled={busy}
            placeholder="Sell 200 loaves through the new site by 1 Dec." onChange={(event) => setGoal(event.target.value)} />
        </div>
        {error && <p role="alert" className="text-ui text-err">{error}</p>}
        <DialogFooter className="gap-2">
          <Button type="button" variant="ghost" disabled={busy} onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" disabled={!name.trim() || busy}>{busy ? "Creating…" : "Create project"}</Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>;
}
