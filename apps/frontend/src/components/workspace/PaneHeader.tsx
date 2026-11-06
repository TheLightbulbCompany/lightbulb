"use client";

// The pane's one header (super-agent, UX slice 4): an optional crumb back to the list, the title (the label of the
// toggle that opened it, or the item's own title), muted meta, the item's actions, then Expand and Close. RightPane
// draws no bar of its own, so every pane item carries exactly one of these and exactly one ✕.
import type { ReactNode, Ref } from "react";
import { ChevronLeft, Maximize2, Minimize2, X } from "lucide-react";
import { closePane, setPaneFullScreen, useWorkspaceState } from "./workspaceController";

export function PaneHeader({ crumb, title, meta, actions, onClose, headingRef }: {
  /** "‹ Tasks": back to the list this item was opened from. */
  crumb?: { label: string; onBack: () => void };
  title: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
  /** What ✕ does; the shell's closePane by default (the room passes its own, which folds its column). */
  onClose?: () => void;
  headingRef?: Ref<HTMLHeadingElement>;
}): React.ReactElement {
  const { ready, fullScreen, pane } = useWorkspaceState();
  return <header className="pane-header">
    {crumb && <button type="button" className="pane-header__crumb" aria-label={`Back to ${crumb.label.toLowerCase()}`} onClick={crumb.onBack}>
      <ChevronLeft size={14} aria-hidden="true" />{crumb.label}
    </button>}
    <h3 ref={headingRef} tabIndex={-1} className="pane-header__title">{title}</h3>
    {meta && <span className="pane-header__meta">{meta}</span>}
    <span className="pane-header__actions">
      {actions}
      {/* Expand needs the shell's pane; a column rendered inline (no shell) has nothing to expand. */}
      {ready && pane && <button type="button" className="pane-header__icon workspace-pane__full" aria-label={fullScreen ? "Exit full screen" : "Full screen"}
        title={fullScreen ? "Exit full screen" : "Full screen"} onClick={() => setPaneFullScreen(!fullScreen)}>
        {fullScreen ? <Minimize2 size={16} aria-hidden="true" /> : <Maximize2 size={16} aria-hidden="true" />}
      </button>}
      <button type="button" className="pane-header__icon" data-pane-close aria-label="Close pane" title="Close" onClick={onClose ?? closePane}>
        <X size={16} aria-hidden="true" />
      </button>
    </span>
  </header>;
}
