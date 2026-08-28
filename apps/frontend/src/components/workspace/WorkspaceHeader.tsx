"use client";

// Desktop workspace header row — a slimmed port of ChatLayout's `.main-header`.
// Deltas vs. ChatLayout:
//   - No mobile hamburger (desktop-only chrome).
//   - No per-agent action cluster (inspector / tasks / pause) — that moved into
//     ChatPanel's own header, since it has no single referent in a multi-chat
//     dock world.
// What remains: the workspace-file browser button, the layout-reset button,
// the support button, and the Clerk UserButton.
import "./workspace.css";

import { UserButton } from "@clerk/nextjs";
import { Chrome, FolderOpen, HelpCircle, CreditCard, RotateCcw } from "lucide-react";

import { capture } from "@/lib/analytics";
import { useSupport } from "@/components/support/SupportProvider";
import { openPanel } from "./workspaceController";

interface WorkspaceHeaderProps {
  // Shell-owned: resets the dock to Mission Control + clears the persisted
  // layout blob (the header knows neither the scopeId nor the builder).
  onResetLayout: () => void;
}

export function WorkspaceHeader({ onResetLayout }: WorkspaceHeaderProps): React.ReactElement {
  const { openSupport } = useSupport();

  return (
    <div className="workspace-header">
      <button
        onClick={() => { capture("file_browser_opened"); openPanel("file-viewer"); }}
        className="flex items-center justify-center text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors p-1"
        title="Browse workspace files"
        aria-label="Browse workspace files"
      >
        <FolderOpen size={18} />
      </button>
      <button
        onClick={() => { capture("browser_view_opened"); openPanel("browser"); }}
        className="flex items-center justify-center text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors p-1"
        title="Watch your agents' browser"
        aria-label="Watch your agents' browser"
      >
        <Chrome size={18} />
      </button>
      <button
        onClick={onResetLayout}
        className="flex items-center justify-center text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors p-1"
        title="Reset workspace layout"
        aria-label="Reset workspace layout"
      >
        <RotateCcw size={18} />
      </button>
      <div style={{ flex: 1 }} />
      <button
        onClick={() => openSupport()}
        className="flex items-center justify-center text-[var(--midnight-muted)] hover:text-[var(--midnight)] transition-colors p-1 mr-1"
        title="Get help / report a problem"
        aria-label="Get help"
      >
        <HelpCircle size={18} />
      </button>
      <UserButton
        appearance={{
          elements: {
            avatarBox: "h-8 w-8",
          },
        }}
      >
        <UserButton.MenuItems>
          <UserButton.Link label="Settings" labelIcon={<CreditCard className="h-4 w-4" />} href="/settings" />
        </UserButton.MenuItems>
      </UserButton>
    </div>
  );
}
