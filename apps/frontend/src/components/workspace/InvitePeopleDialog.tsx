"use client";

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { InvitePeoplePanel } from "./InvitePeoplePanel";
import { closeInviteDialog, useInviteDialogOpen } from "./workspaceController";

/** Opened by `openInviteDialog()` (the rail's Invite, the sidebar's "Invite people"). Mounted once
 *  by WorkspaceShell. The content unmounts on close, so every open re-reads the pending list. */
export function InvitePeopleDialog(): React.ReactElement {
  const open = useInviteDialogOpen();
  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) closeInviteDialog(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Invite people</DialogTitle>
          <DialogDescription>Everyone you invite shares this workspace&apos;s agents and rooms.</DialogDescription>
        </DialogHeader>
        <InvitePeoplePanel source="invite_dialog" />
      </DialogContent>
    </Dialog>
  );
}
