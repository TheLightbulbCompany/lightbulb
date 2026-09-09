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
import { Chrome, FolderOpen, HelpCircle, CreditCard, RotateCcw, Search } from "lucide-react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { capture } from "@/lib/analytics";
import { useSupport } from "@/components/support/SupportProvider";
import { useCommandPalette } from "@/components/teams/command-palette/CommandPaletteProvider";
import { openPanel } from "./workspaceController";

interface WorkspaceHeaderProps {
  // Shell-owned: resets the dock to Mission Control + clears the persisted
  // layout blob (the header knows neither the scopeId nor the builder).
  onResetLayout: () => void;
}

export function WorkspaceHeader({ onResetLayout }: WorkspaceHeaderProps): React.ReactElement {
  const { openSupport } = useSupport();
  const { setOpen: setCommandPaletteOpen } = useCommandPalette();

  return (
    // Every button below keeps its aria-label — the tooltip is the hover
    // affordance, never the accessible name.
    <div className="workspace-header">
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            onClick={() => { capture("file_browser_opened"); openPanel("file-viewer"); }}
            className="flex h-8 w-8 items-center justify-center rounded-md text-[var(--midnight-muted)] transition-colors duration-(--dur-fast) ease-out hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
            aria-label="Browse workspace files"
          >
            <FolderOpen size={16} />
          </button>
        </TooltipTrigger>
        <TooltipContent>Browse workspace files</TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            onClick={() => { capture("browser_view_opened"); openPanel("browser"); }}
            className="flex h-8 w-8 items-center justify-center rounded-md text-[var(--midnight-muted)] transition-colors duration-(--dur-fast) ease-out hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
            aria-label="Watch your agents' browser"
          >
            <Chrome size={16} />
          </button>
        </TooltipTrigger>
        <TooltipContent>Watch your agents&rsquo; browser</TooltipContent>
      </Tooltip>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            onClick={onResetLayout}
            className="flex h-8 w-8 items-center justify-center rounded-md text-[var(--midnight-muted)] transition-colors duration-(--dur-fast) ease-out hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)]"
            aria-label="Reset workspace layout"
          >
            <RotateCcw size={16} />
          </button>
        </TooltipTrigger>
        <TooltipContent>Reset workspace layout</TooltipContent>
      </Tooltip>
      <div className="workspace-header__spacer" />
      <button
        type="button"
        className="workspace-search"
        onClick={() => setCommandPaletteOpen(true)}
      >
        <Search size={15} aria-hidden="true" />
        <span>Search agents, issues, and projects</span>
        <kbd>⌘/Ctrl K</kbd>
      </button>
      <div className="workspace-header__spacer" />
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            onClick={() => openSupport()}
            className="flex h-8 w-8 items-center justify-center rounded-md text-[var(--midnight-muted)] transition-colors duration-(--dur-fast) ease-out hover:bg-[var(--cream-deep)] hover:text-[var(--midnight)] mr-1"
            aria-label="Get help"
          >
            <HelpCircle size={16} />
          </button>
        </TooltipTrigger>
        <TooltipContent>Get help / report a problem</TooltipContent>
      </Tooltip>
      <UserButton
        appearance={{
          elements: {
            avatarBox: "h-8 w-8",
          },
        }}
      >
        <UserButton.MenuItems>
          <UserButton.Link label="Settings" labelIcon={<CreditCard size={16} />} href="/settings" />
        </UserButton.MenuItems>
      </UserButton>
    </div>
  );
}
