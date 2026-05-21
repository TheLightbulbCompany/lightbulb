// apps/frontend/src/components/teams/inbox/hooks/useInboxKeyboardNav.ts

// Ported from upstream Paperclip's pages/Inbox.tsx inline keydown listener
// (paperclip/ui/src/pages/Inbox.tsx:1604-1820) (MIT, (c) 2025 Paperclip AI).
// Subset: j/k/ArrowDown/Up navigation, Enter open, a/y archive, r mark-read,
// u undo-archive. Drops ArrowLeft/Right group-collapse, capital-U mark-unread,
// and quick-archive arming.

import { useEffect, useRef } from "react";

import type { InboxKeyboardNavEntry } from "@/components/teams/shared/lib/inbox";
import {
  hasBlockingShortcutDialog,
  isKeyboardShortcutTextInputTarget,
  resolveInboxUndoArchiveKeyAction,
} from "@/components/teams/shared/lib/keyboardShortcuts";

export interface UseInboxKeyboardNavParams {
  /**
   * Gates navigation shortcuts: j / k / ArrowDown / ArrowUp / Enter.
   * Upstream Paperclip allows nav on every tab, so this should generally
   * be true. When false, the listener is installed but nav keys no-op.
   */
  enableNav: boolean;
  /**
   * Gates archive-related shortcuts: a / y archive, r mark-read,
   * u undo-archive. Upstream restricts these to the "mine" tab — match
   * that here. When false, those keys no-op (nav is unaffected).
   */
  enableArchive: boolean;
  /** Flat list of entries the user navigates with j/k/ArrowDown/ArrowUp. */
  navItems: InboxKeyboardNavEntry[];
  /** Currently-selected index into `navItems`. -1 means no selection. */
  selectedIndex: number;
  /** Called with the next selected index after j/k/ArrowDown/ArrowUp. */
  onSelectIndex: (idx: number) => void;
  /** Called when Enter is pressed on the currently-selected entry. */
  onOpen: (item: InboxKeyboardNavEntry) => void;
  /** Called when `a` or `y` is pressed on the currently-selected entry. */
  onArchive: (item: InboxKeyboardNavEntry) => void;
  /** Called when `r` is pressed on the currently-selected entry. */
  onMarkRead: (item: InboxKeyboardNavEntry) => void;
  /** Called when `u` is pressed AND `hasUndoableArchive` is true. */
  onUndoArchive: () => void;
  /** Whether there is an archive in the undo stack — gates the `u` shortcut. */
  hasUndoableArchive: boolean;
  /**
   * If true, all shortcuts are skipped. Defaults to whatever
   * `hasBlockingShortcutDialog(document)` reports at event time.
   */
  hasOpenDialog?: boolean;
}

/**
 * Subscribe to document-level keydown events for Inbox keyboard nav. The
 * listener installs ONCE (empty deps array) and reads the latest params via a
 * ref, mirroring upstream's `kbStateRef` / `kbActionsRef` pattern. Callers can
 * pass new closures each render without resubscribing.
 *
 * Scope (all gated by `enableNav` for nav keys / `enableArchive` for the rest):
 * - j / ArrowDown — move selection down (clamps at last item) [enableNav]
 * - k / ArrowUp — move selection up (clamps at first item) [enableNav]
 * - Enter — invoke `onOpen` with the selected item [enableNav]
 * - a / y — invoke `onArchive` with the selected item [enableArchive]
 * - r — invoke `onMarkRead` with the selected item [enableArchive]
 * - u — invoke `onUndoArchive` (gated by `hasUndoableArchive`) [enableArchive]
 *
 * Out of scope (intentionally NOT ported in v1):
 * - ArrowLeft / ArrowRight group-collapse (no nesting in v1)
 * - capital `U` mark-unread (deferred)
 * - quick-archive `y` arming on issue-detail breadcrumbs (Task 11)
 *
 * Scroll-into-view is NOT handled here. The list component (Task 10) owns
 * scroll behavior via `data-inbox-item-id` attributes — keep the concerns
 * separate.
 */
export function useInboxKeyboardNav(params: UseInboxKeyboardNavParams): void {
  const stateRef = useRef(params);
  stateRef.current = params;

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const {
        enableNav,
        enableArchive,
        navItems,
        selectedIndex,
        onSelectIndex,
        onOpen,
        onArchive,
        onMarkRead,
        onUndoArchive,
        hasUndoableArchive,
        hasOpenDialog,
      } = stateRef.current;

      if (event.defaultPrevented) return;
      if (isKeyboardShortcutTextInputTarget(event.target)) return;

      const dialogOpen = hasOpenDialog ?? hasBlockingShortcutDialog(document);

      // Undo-archive runs through the resolver helper, which does its own
      // gating (modifier check, dialog check, target check, etc.). We pass
      // `dialogOpen` so the resolver matches what the rest of this handler
      // sees. Gated by `enableArchive` since it's an archive-class shortcut.
      if (enableArchive) {
        const undoAction = resolveInboxUndoArchiveKeyAction({
          hasUndoableArchive,
          defaultPrevented: event.defaultPrevented,
          key: event.key,
          metaKey: event.metaKey,
          ctrlKey: event.ctrlKey,
          altKey: event.altKey,
          target: event.target,
          hasOpenDialog: dialogOpen,
        });
        if (undoAction === "undo_archive") {
          event.preventDefault();
          onUndoArchive();
          return;
        }
      }

      // Every other shortcut: skip if a blocking dialog is open or modifier
      // keys are held.
      if (dialogOpen) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      const itemCount = navItems.length;

      switch (event.key) {
        case "j":
        case "ArrowDown": {
          if (!enableNav) return;
          if (itemCount === 0) return;
          const next =
            selectedIndex < 0
              ? 0
              : Math.min(selectedIndex + 1, itemCount - 1);
          event.preventDefault();
          onSelectIndex(next);
          return;
        }
        case "k":
        case "ArrowUp": {
          if (!enableNav) return;
          if (itemCount === 0) return;
          const next =
            selectedIndex < 0 ? 0 : Math.max(selectedIndex - 1, 0);
          event.preventDefault();
          onSelectIndex(next);
          return;
        }
        case "Enter": {
          if (!enableNav) return;
          if (selectedIndex < 0 || selectedIndex >= itemCount) return;
          const item = navItems[selectedIndex];
          if (!item) return;
          event.preventDefault();
          onOpen(item);
          return;
        }
        case "a":
        case "y": {
          if (!enableArchive) return;
          if (selectedIndex < 0 || selectedIndex >= itemCount) return;
          const item = navItems[selectedIndex];
          if (!item) return;
          event.preventDefault();
          onArchive(item);
          return;
        }
        case "r": {
          if (!enableArchive) return;
          if (selectedIndex < 0 || selectedIndex >= itemCount) return;
          const item = navItems[selectedIndex];
          if (!item) return;
          event.preventDefault();
          onMarkRead(item);
          return;
        }
        default:
          return;
      }
    };

    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
    // Listener is installed ONCE; latest params are read via stateRef.current.
    // Putting `params` here would cause subscribe/unsubscribe on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
}
