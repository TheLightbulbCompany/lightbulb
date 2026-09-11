import { describe, test, expect, beforeEach } from "vitest";
import {
  findPageSearchShortcutTarget,
  focusPageSearchShortcutTarget,
  hasBlockingShortcutDialog,
  isKeyboardShortcutTextInputTarget,
  resolveInboxUndoArchiveKeyAction,
  shouldBlurPageSearchOnEnter,
  shouldBlurPageSearchOnEscape,
} from "@/components/teams/shared/lib/keyboardShortcuts";

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("isKeyboardShortcutTextInputTarget", () => {
  test("true for <input>", () => {
    const el = document.createElement("input");
    document.body.appendChild(el);
    expect(isKeyboardShortcutTextInputTarget(el)).toBe(true);
  });

  test("true for <textarea>", () => {
    const el = document.createElement("textarea");
    document.body.appendChild(el);
    expect(isKeyboardShortcutTextInputTarget(el)).toBe(true);
  });

  test("true for <select>", () => {
    const el = document.createElement("select");
    document.body.appendChild(el);
    expect(isKeyboardShortcutTextInputTarget(el)).toBe(true);
  });

  test("true for contenteditable div", () => {
    const el = document.createElement("div");
    el.setAttribute("contenteditable", "true");
    document.body.appendChild(el);
    expect(isKeyboardShortcutTextInputTarget(el)).toBe(true);
  });

  test("true for contenteditable=plaintext-only", () => {
    const el = document.createElement("div");
    el.setAttribute("contenteditable", "plaintext-only");
    document.body.appendChild(el);
    // jsdom doesn't reflect contenteditable="plaintext-only" into
    // isContentEditable — but the selector match is what we care about here.
    expect(isKeyboardShortcutTextInputTarget(el)).toBe(true);
  });

  test("true for role=textbox", () => {
    const el = document.createElement("div");
    el.setAttribute("role", "textbox");
    document.body.appendChild(el);
    expect(isKeyboardShortcutTextInputTarget(el)).toBe(true);
  });

  test("true for role=combobox", () => {
    const el = document.createElement("div");
    el.setAttribute("role", "combobox");
    document.body.appendChild(el);
    expect(isKeyboardShortcutTextInputTarget(el)).toBe(true);
  });

  test("true for child of contenteditable region", () => {
    const wrapper = document.createElement("div");
    wrapper.setAttribute("contenteditable", "true");
    const child = document.createElement("span");
    wrapper.appendChild(child);
    document.body.appendChild(wrapper);
    expect(isKeyboardShortcutTextInputTarget(child)).toBe(true);
  });

  test("false for plain div", () => {
    const el = document.createElement("div");
    document.body.appendChild(el);
    expect(isKeyboardShortcutTextInputTarget(el)).toBe(false);
  });

  test("false for null", () => {
    expect(isKeyboardShortcutTextInputTarget(null)).toBe(false);
  });

  test("false for non-HTMLElement EventTarget", () => {
    // Plain EventTarget object is not an HTMLElement.
    const target = new EventTarget();
    expect(isKeyboardShortcutTextInputTarget(target)).toBe(false);
  });
});

describe("hasBlockingShortcutDialog", () => {
  test("false when no dialog in DOM", () => {
    expect(hasBlockingShortcutDialog()).toBe(false);
  });

  test("true when alertdialog is open", () => {
    const d = document.createElement("div");
    d.setAttribute("role", "alertdialog");
    document.body.appendChild(d);
    expect(hasBlockingShortcutDialog()).toBe(true);
  });

  test("true when dialog is open", () => {
    const d = document.createElement("div");
    d.setAttribute("role", "dialog");
    document.body.appendChild(d);
    expect(hasBlockingShortcutDialog()).toBe(true);
  });

  test("scoped to provided root", () => {
    const outer = document.createElement("div");
    outer.setAttribute("role", "dialog");
    document.body.appendChild(outer);

    const scope = document.createElement("section");
    document.body.appendChild(scope);
    // outer dialog is outside scope → false within scope
    expect(hasBlockingShortcutDialog(scope)).toBe(false);
    // but visible at document scope
    expect(hasBlockingShortcutDialog(document)).toBe(true);
  });
});

describe("findPageSearchShortcutTarget / focusPageSearchShortcutTarget", () => {
  test("finds element with data-page-search attribute", () => {
    const input = document.createElement("input");
    input.setAttribute("data-page-search", "");
    document.body.appendChild(input);
    expect(findPageSearchShortcutTarget()).toBe(input);
  });

  test("returns null when no [data-page-search] in DOM", () => {
    expect(findPageSearchShortcutTarget()).toBeNull();
  });

  test("focuses element with data-page-search attribute", () => {
    const input = document.createElement("input");
    input.setAttribute("data-page-search", "");
    document.body.appendChild(input);
    expect(focusPageSearchShortcutTarget()).toBe(true);
    expect(document.activeElement).toBe(input);
  });

  test("selects existing text on input when focused", () => {
    const input = document.createElement("input");
    input.setAttribute("data-page-search", "");
    input.value = "hello";
    document.body.appendChild(input);
    focusPageSearchShortcutTarget();
    expect(input.selectionStart).toBe(0);
    expect(input.selectionEnd).toBe(5);
  });

  test("returns false when no [data-page-search] in DOM", () => {
    expect(focusPageSearchShortcutTarget()).toBe(false);
  });

  test("skips disabled inputs", () => {
    const disabled = document.createElement("input");
    disabled.setAttribute("data-page-search", "");
    disabled.disabled = true;
    document.body.appendChild(disabled);

    const enabled = document.createElement("input");
    enabled.setAttribute("data-page-search", "");
    document.body.appendChild(enabled);

    expect(findPageSearchShortcutTarget()).toBe(enabled);
  });

  test("skips elements inside aria-hidden subtree", () => {
    const wrapper = document.createElement("div");
    wrapper.setAttribute("aria-hidden", "true");
    const hidden = document.createElement("input");
    hidden.setAttribute("data-page-search", "");
    wrapper.appendChild(hidden);
    document.body.appendChild(wrapper);

    expect(findPageSearchShortcutTarget()).toBeNull();
  });
});

describe("resolveInboxUndoArchiveKeyAction", () => {
  // Mirrors upstream's destructured-arg signature exactly. The shortcut is
  // unmodified `u`, NOT Cmd+Z. Caller owns the gating context (hasUndoableArchive,
  // hasOpenDialog, target inspection).
  const baseArgs = {
    hasUndoableArchive: true,
    defaultPrevented: false,
    key: "u",
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    target: null,
    hasOpenDialog: false,
  };

  test("returns 'undo_archive' on plain `u` when armed", () => {
    expect(resolveInboxUndoArchiveKeyAction(baseArgs)).toBe("undo_archive");
  });

  test("returns 'ignore' when no archive is undoable", () => {
    expect(
      resolveInboxUndoArchiveKeyAction({ ...baseArgs, hasUndoableArchive: false })
    ).toBe("ignore");
  });

  test("returns 'ignore' when defaultPrevented", () => {
    expect(
      resolveInboxUndoArchiveKeyAction({ ...baseArgs, defaultPrevented: true })
    ).toBe("ignore");
  });

  test("returns 'ignore' on Cmd+u (any modifier rejects)", () => {
    expect(resolveInboxUndoArchiveKeyAction({ ...baseArgs, metaKey: true })).toBe("ignore");
  });

  test("returns 'ignore' on Ctrl+u", () => {
    expect(resolveInboxUndoArchiveKeyAction({ ...baseArgs, ctrlKey: true })).toBe("ignore");
  });

  test("returns 'ignore' on Alt+u", () => {
    expect(resolveInboxUndoArchiveKeyAction({ ...baseArgs, altKey: true })).toBe("ignore");
  });

  test("returns 'ignore' on a non-`u` key", () => {
    expect(resolveInboxUndoArchiveKeyAction({ ...baseArgs, key: "z" })).toBe("ignore");
  });

  test("returns 'ignore' when a blocking dialog is open", () => {
    expect(
      resolveInboxUndoArchiveKeyAction({ ...baseArgs, hasOpenDialog: true })
    ).toBe("ignore");
  });

  test("returns 'ignore' when target is a text input", () => {
    const input = document.createElement("input");
    expect(
      resolveInboxUndoArchiveKeyAction({ ...baseArgs, target: input })
    ).toBe("ignore");
  });

  test("returns 'ignore' on a modifier-only key like Shift", () => {
    expect(resolveInboxUndoArchiveKeyAction({ ...baseArgs, key: "Shift" })).toBe("ignore");
  });
});

describe("shouldBlurPageSearchOnEnter", () => {
  test("true when Enter pressed and not composing", () => {
    expect(shouldBlurPageSearchOnEnter({ key: "Enter", isComposing: false })).toBe(true);
  });

  test("false on non-Enter key", () => {
    expect(shouldBlurPageSearchOnEnter({ key: "a", isComposing: false })).toBe(false);
  });

  test("false when isComposing (IME)", () => {
    expect(shouldBlurPageSearchOnEnter({ key: "Enter", isComposing: true })).toBe(false);
  });
});

describe("shouldBlurPageSearchOnEscape", () => {
  // Mirrors upstream's two-step Esc behavior: a populated Esc clears text
  // first (browser default), then a second Esc on an empty field blurs.
  test("true when Escape pressed on an empty input", () => {
    expect(
      shouldBlurPageSearchOnEscape({ key: "Escape", isComposing: false, currentValue: "" })
    ).toBe(true);
  });

  test("false when Escape pressed on a populated input (clear-first)", () => {
    expect(
      shouldBlurPageSearchOnEscape({ key: "Escape", isComposing: false, currentValue: "fix" })
    ).toBe(false);
  });

  test("false on non-Escape key", () => {
    expect(
      shouldBlurPageSearchOnEscape({ key: "a", isComposing: false, currentValue: "" })
    ).toBe(false);
  });

  test("false when isComposing (IME)", () => {
    expect(
      shouldBlurPageSearchOnEscape({ key: "Escape", isComposing: true, currentValue: "" })
    ).toBe(false);
  });
});
