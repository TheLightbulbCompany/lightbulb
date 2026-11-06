"use client";

import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import "./messaging.css";

const GROUPS: { name: string; rows: [string, string][] }[] = [
  {
    name: "Move around",
    rows: [
      ["⌘K", "Jump to a channel, person or agent"],
      ["⌘[", "Back"],
      ["⌘]", "Forward"],
      ["⌥↑", "Previous chat"],
      ["⌥↓", "Next chat"],
      ["⌥⇧↑", "Previous unread chat"],
      ["⌥⇧↓", "Next unread chat"],
      ["Esc", "Close the typeahead, then the thread or overlay"],
      ["⌘/", "Show this sheet"],
    ],
  },
  {
    name: "Write",
    rows: [
      ["Enter", "Send"],
      ["Shift+Enter", "New line"],
      ["↑", "Edit your last message (empty composer)"],
      ["@", "Mention a person or agent"],
      [":", "Insert an emoji"],
      ["⌘B", "Bold"],
      ["⌘I", "Italic"],
      ["⌘⇧X", "Strikethrough"],
      ["⌘⇧C", "Code block"],
    ],
  },
];

export function ShortcutSheet({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return <Dialog open={open} onOpenChange={onOpenChange}>
    <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-lg max-h-[calc(100dvh-2rem)] overflow-y-auto" showCloseButton>
      <DialogHeader><DialogTitle>Keyboard shortcuts</DialogTitle>
        <DialogDescription>On Windows and Linux, ⌘ is Ctrl and ⌥ is Alt.</DialogDescription></DialogHeader>
      {GROUPS.map((group) => <section key={group.name} aria-label={group.name}>
        <h4 className="font-semibold">{group.name}</h4>
        <dl className="room-shortcuts">{group.rows.map(([chord, description]) => <div key={chord}>
          <dt><kbd className="room-kbd">{chord}</kbd></dt><dd className="text-ui text-muted-foreground">{description}</dd>
        </div>)}</dl>
      </section>)}
    </DialogContent>
  </Dialog>;
}
