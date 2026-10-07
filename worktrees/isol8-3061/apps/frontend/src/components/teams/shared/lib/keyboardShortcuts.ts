// apps/frontend/src/components/teams/shared/lib/keyboardShortcuts.ts

// Ported (subset) from upstream Paperclip's lib/keyboardShortcuts.ts
// (paperclip/ui/src/lib/keyboardShortcuts.ts) (MIT, (c) 2025 Paperclip AI).

// Subset: pure DOM helpers used by the Inbox keyboard-nav hook (Task 6).
// Skipped from upstream: issue-detail chord shortcuts (g i / g c) and the
// resolveInboxQuickArchiveKeyAction / resolveIssueDetailGoKeyAction helpers —
// Isol8 doesn't yet have those routes/flows. resolveInboxUndoArchiveKeyAction
// and shouldBlurPageSearchOnEscape preserve upstream's destructured-arg
// signatures + behavior verbatim (the undo shortcut is the unmodified `u`
// key, not Cmd+Z; Esc-blurs only when the search input is empty so a populated
// Esc clears text first). Caller hooks own the gating contracts.

const KEYBOARD_SHORTCUT_TEXT_INPUT_SELECTOR = [
  "input",
  "textarea",
  "select",
  "[contenteditable='true']",
  "[contenteditable='plaintext-only']",
  "[role='textbox']",
  "[role='combobox']",
].join(", ");

const PAGE_SEARCH_SHORTCUT_SELECTOR = "[data-page-search]";

/**
 * True if the event target is a text-input-like element (input, textarea,
 * contenteditable, etc.). Used by keyboard-shortcut handlers to skip handling
 * single-letter shortcuts while the user is typing in a field.
 */
export function isKeyboardShortcutTextInputTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  return !!target.closest(KEYBOARD_SHORTCUT_TEXT_INPUT_SELECTOR);
}

/**
 * True if there is an open Radix-style modal dialog in the DOM. Keyboard
 * shortcuts should be suppressed while a dialog is in front.
 */
export function hasBlockingShortcutDialog(root: ParentNode = document): boolean {
  return !!root.querySelector("[role='alertdialog'], [role='dialog']");
}

function isVisibleShortcutTarget(element: HTMLElement): boolean {
  if (!element.isConnected) return false;
  if ("disabled" in element && typeof (element as HTMLInputElement).disabled === "boolean" && (element as HTMLInputElement).disabled) {
    return false;
  }
  if (element.closest("[hidden], [aria-hidden='true'], [inert]")) return false;
  if (element.closest("[role='dialog'][aria-modal='true']")) return false;

  // jsdom doesn't implement layout, so getComputedStyle/getClientRects are
  // unreliable there. We fall through to a "connected and not hidden" check.
  if (typeof window !== "undefined" && typeof window.getComputedStyle === "function") {
    try {
      const style = window.getComputedStyle(element);
      if (style.display === "none" || style.visibility === "hidden") return false;
    } catch {
      // ignore — jsdom edge cases
    }
  }

  return true;
}

/**
 * Find the page-search input (element annotated with `data-page-search`) in
 * the given root. Returns the first visible candidate, or null if none.
 */
export function findPageSearchShortcutTarget(root: ParentNode = document): HTMLElement | null {
  const candidates = Array.from(root.querySelectorAll<HTMLElement>(PAGE_SEARCH_SHORTCUT_SELECTOR));
  return candidates.find((candidate) => isVisibleShortcutTarget(candidate)) ?? null;
}

/**
 * Focus the page-search input. Returns true if a target was found and
 * focused, false otherwise. Selects existing text on inputs/textareas so the
 * user can immediately overwrite it.
 */
export function focusPageSearchShortcutTarget(root: ParentNode = document): boolean {
  const target = findPageSearchShortcutTarget(root);
  if (!target) return false;

  target.focus();
  if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement) {
    target.select();
  }
  return true;
}

export type InboxUndoArchiveKeyAction = "ignore" | "undo_archive";

const MODIFIER_ONLY_KEYS = new Set([
  "Shift",
  "Control",
  "Alt",
  "Meta",
  "CapsLock",
  "OS",
  "ContextMenu",
]);

function isModifierOnlyKey(key: string): boolean {
  return MODIFIER_ONLY_KEYS.has(key);
}

/**
 * Resolve whether a keydown should trigger Inbox undo-archive. Mirrors upstream
 * `paperclip/ui/src/lib/keyboardShortcuts.ts:resolveInboxUndoArchiveKeyAction`
 * verbatim: the shortcut is the unmodified `u` key (NOT Cmd+Z) and the caller
 * owns the gating context (`hasUndoableArchive`, `hasOpenDialog`, target
 * inspection).
 */
export function resolveInboxUndoArchiveKeyAction(args: {
  hasUndoableArchive: boolean;
  defaultPrevented: boolean;
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  target: EventTarget | null;
  hasOpenDialog: boolean;
}): InboxUndoArchiveKeyAction {
  const { hasUndoableArchive, defaultPrevented, key, metaKey, ctrlKey, altKey, target, hasOpenDialog } = args;
  if (!hasUndoableArchive) return "ignore";
  if (defaultPrevented) return "ignore";
  if (metaKey || ctrlKey || altKey || isModifierOnlyKey(key)) return "ignore";
  if (hasOpenDialog || isKeyboardShortcutTextInputTarget(target)) return "ignore";
  if (key === "u") return "undo_archive";
  return "ignore";
}

/**
 * True if Enter pressed while focus is on a page-search input. Caller blurs
 * the input on true. Mirrors upstream's destructured-arg signature.
 */
export function shouldBlurPageSearchOnEnter(args: {
  key: string;
  isComposing: boolean;
}): boolean {
  return args.key === "Enter" && !args.isComposing;
}

/**
 * True if Escape pressed while focus is on a page-search input AND the input
 * is empty. Mirrors upstream's two-step Esc behavior: a populated Esc clears
 * text first (handled by the input itself), then a second Esc on an empty
 * field blurs.
 */
export function shouldBlurPageSearchOnEscape(args: {
  key: string;
  isComposing: boolean;
  currentValue: string;
}): boolean {
  return args.key === "Escape" && !args.isComposing && args.currentValue.length === 0;
}
