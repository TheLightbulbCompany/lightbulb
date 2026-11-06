"use client";

import { useId, useMemo, useRef, useState, type KeyboardEvent, type ReactElement, type ReactNode } from "react";
import { SmilePlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { ALL_EMOJI, EMOJI_CATEGORIES, searchEmoji, type EmojiEntry } from "@lightbulb/shared";
import "./emoji-picker.css";

export const RECENT_EMOJI_KEY = "lightbulb.messaging.recentEmoji";
const RECENT_MAX = 8;
const COLUMNS = 8;

export function readRecentEmoji(): string[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(RECENT_EMOJI_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string").slice(0, RECENT_MAX) : [];
  } catch {
    return [];
  }
}

export function recordRecentEmoji(emoji: string): void {
  try {
    localStorage.setItem(RECENT_EMOJI_KEY, JSON.stringify([emoji, ...readRecentEmoji().filter((item) => item !== emoji)].slice(0, RECENT_MAX)));
  } catch {
    /* per-viewer convenience only */
  }
}

interface EmojiPickerProps {
  onPick: (emoji: string) => void;
  disabled?: boolean;
  triggerLabel?: string;
  /** Controlled, with `anchor` in place of the picker's own button (the composer's + menu opens it). */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  anchor?: ReactNode;
}

export function EmojiPicker({ onPick, disabled = false, triggerLabel = "Add reaction", open: openProp, onOpenChange, anchor }: EmojiPickerProps): ReactElement {
  const id = useId();
  const [ownOpen, setOwnOpen] = useState(false);
  const open = openProp ?? ownOpen;
  const setOpen = (next: boolean) => { setOwnOpen(next); onOpenChange?.(next); };
  const [query, setQuery] = useState("");
  // Read each time it opens, however it was opened (its own button, or a controlled parent).
  const recent = useMemo(() => open ? readRecentEmoji() : [], [open]);
  const grid = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const results = useMemo(() => searchEmoji(query), [query]);
  const sections: { name: string; entries: readonly EmojiEntry[] }[] = query.trim()
    ? [{ name: "Results", entries: results }]
    : [
        ...(recent.length
          ? [
              {
                name: "Recent",
                entries: recent.map((emoji) => ALL_EMOJI.find((entry) => entry.emoji === emoji) ?? { emoji, name: emoji, keywords: [] }),
              },
            ]
          : []),
        ...EMOJI_CATEGORIES,
      ];

  function pick(emoji: string) {
    recordRecentEmoji(emoji);
    onPick(emoji);
    setOpen(false);
    setQuery("");
  }

  function onGridKey(event: KeyboardEvent<HTMLDivElement>) {
    const buttons = [...(grid.current?.querySelectorAll<HTMLButtonElement>("button[data-emoji]") ?? [])];
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const move = (delta: number) => {
      event.preventDefault();
      buttons[Math.max(0, Math.min(buttons.length - 1, index + delta))]?.focus();
    };
    if (event.key === "ArrowRight") move(1);
    else if (event.key === "ArrowLeft") move(-1);
    else if (event.key === "ArrowDown") move(COLUMNS);
    else if (event.key === "ArrowUp") move(-COLUMNS);
  }

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setQuery("");
      }}
    >
      {anchor ? <PopoverAnchor asChild>{anchor}</PopoverAnchor> : <PopoverTrigger asChild>
        <Button type="button" variant="ghost" size="icon-sm" aria-label={triggerLabel} title={triggerLabel} disabled={disabled}>
          <SmilePlus aria-hidden="true" />
        </Button>
      </PopoverTrigger>}
      <PopoverContent
        align="end"
        className="room-emoji-picker"
        role="dialog"
        aria-label="Choose an emoji"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          searchRef.current?.focus();
        }}
        onEscapeKeyDown={(event) => event.stopPropagation()}
      >
        <label className="sr-only" htmlFor={`${id}-search`}>
          Search emoji
        </label>
        <input
          id={`${id}-search`}
          ref={searchRef}
          type="search"
          className="room-emoji-search"
          placeholder="Search emoji"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              grid.current?.querySelector<HTMLButtonElement>("button[data-emoji]")?.focus();
            }
          }}
        />
        <div ref={grid} className="room-emoji-grid" onKeyDown={onGridKey}>
          {sections.map((section) => (
            <section key={section.name} aria-label={section.name}>
              <h4 className="room-emoji-heading">{section.name}</h4>
              <div className="room-emoji-row">
                {section.entries.map((entry) => (
                  <button
                    key={`${section.name}:${entry.emoji}`}
                    type="button"
                    tabIndex={-1}
                    data-emoji={entry.emoji}
                    aria-label={entry.name}
                    title={entry.name}
                    onClick={() => pick(entry.emoji)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        pick(entry.emoji);
                      }
                    }}
                  >
                    {entry.emoji}
                  </button>
                ))}
              </div>
              {section.entries.length === 0 && <p className="text-ui text-muted-foreground">No matching emoji.</p>}
            </section>
          ))}
        </div>
      </PopoverContent>
    </Popover>
  );
}
