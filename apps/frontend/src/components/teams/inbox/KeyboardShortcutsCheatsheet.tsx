// Ported from upstream Paperclip's KeyboardShortcutsCheatsheet.tsx
// (paperclip/ui/src/components/KeyboardShortcutsCheatsheet.tsx) (MIT, (c) 2025 Paperclip AI).

import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

interface ShortcutEntry {
  keys: string[];
  label: string;
}

interface ShortcutSection {
  title: string;
  shortcuts: ShortcutEntry[];
}

const sections: ShortcutSection[] = [
  {
    title: "Inbox",
    shortcuts: [
      { keys: ["j"], label: "Move down" },
      { keys: ["↓"], label: "Move down" },
      { keys: ["k"], label: "Move up" },
      { keys: ["↑"], label: "Move up" },
      { keys: ["←"], label: "Collapse selected group" },
      { keys: ["→"], label: "Expand selected group" },
      { keys: ["Enter"], label: "Open selected item" },
      { keys: ["a"], label: "Archive item" },
      { keys: ["y"], label: "Archive item" },
      { keys: ["r"], label: "Mark as read" },
      { keys: ["U"], label: "Mark as unread" },
    ],
  },
  {
    title: "Global",
    shortcuts: [
      { keys: ["/"], label: "Search current page or quick search" },
      { keys: ["c"], label: "New task" },
      { keys: ["["], label: "Toggle sidebar" },
      { keys: ["]"], label: "Toggle panel" },
      { keys: ["?"], label: "Show keyboard shortcuts" },
    ],
  },
];

function KeyCap({ children }: { children: string }) {
  return (
    <kbd className="inline-flex h-6 min-w-6 items-center justify-center rounded border border-[var(--edge)] bg-[var(--cream-deeper)] px-1.5 font-mono text-xs font-medium text-[var(--ink)] shadow-[0_1px_0_1px_var(--edge)]">
      {children}
    </kbd>
  );
}

export function KeyboardShortcutsCheatsheetContent() {
  return (
    <>
      <div className="divide-y divide-[var(--edge)] border-t border-[var(--edge)]">
        {sections.map((section) => (
          <div key={section.title} className="px-5 py-3">
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--mute)]">
              {section.title}
            </h3>
            <div className="space-y-1.5">
              {section.shortcuts.map((shortcut) => (
                <div
                  key={shortcut.label + shortcut.keys.join()}
                  className="flex items-center justify-between gap-4"
                >
                  <span className="text-sm text-[var(--ink-soft)]">{shortcut.label}</span>
                  <div className="flex items-center gap-1">
                    {shortcut.keys.map((key, i) => (
                      <span key={key} className="flex items-center gap-1">
                        {i > 0 && <span className="text-xs text-[var(--mute)]">then</span>}
                        <KeyCap>{key}</KeyCap>
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div className="border-t border-[var(--edge)] px-5 py-3">
        <p className="text-xs text-[var(--mute)]">
          Press <KeyCap>Esc</KeyCap> to close &middot; Shortcuts are disabled in text fields
        </p>
      </div>
    </>
  );
}

export function KeyboardShortcutsCheatsheet({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="sm:max-w-md gap-0 p-0 overflow-hidden">
        <AlertDialogHeader className="px-5 pt-5 pb-3">
          <AlertDialogTitle className="font-editorial-serif text-xl text-[var(--ink)]">
            Keyboard shortcuts
          </AlertDialogTitle>
        </AlertDialogHeader>
        <KeyboardShortcutsCheatsheetContent />
      </AlertDialogContent>
    </AlertDialog>
  );
}
