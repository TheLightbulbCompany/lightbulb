"use client";

// apps/frontend/src/components/teams/command-palette/CommandPaletteProvider.tsx

// Ported from upstream Paperclip's CommandPalette mount pattern
// (paperclip/ui/src/components/Layout.tsx) (MIT, (c) 2025 Paperclip AI).
// Owns open state + global Cmd/Ctrl+K keydown listener. Children call
// useCommandPalette() to programmatically open/close.

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { CommandPalette } from "./CommandPalette";

interface CommandPaletteContextValue {
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
}

const CommandPaletteContext = createContext<CommandPaletteContextValue | null>(null);

export function useCommandPalette(): CommandPaletteContextValue {
  const ctx = useContext(CommandPaletteContext);
  if (!ctx) {
    throw new Error("useCommandPalette must be used inside <CommandPaletteProvider>");
  }
  return ctx;
}

export function CommandPaletteProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const toggle = useCallback(() => setOpen((o) => !o), []);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Plain Cmd+K or Ctrl+K (no Shift, no Alt) toggles. Ignore when typing
      // in inputs ONLY if they're not the palette's own input — but since the
      // palette mounts as a Dialog, its input lives in a portal and the
      // focus-target check would short-circuit toggle while open. Instead
      // we ignore alt/shift modifiers and let cmd+k toggle from anywhere.
      if (!(e.metaKey || e.ctrlKey)) return;
      if (e.altKey || e.shiftKey) return;
      if (e.key.toLowerCase() !== "k") return;
      e.preventDefault();
      toggle();
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [toggle]);

  return (
    <CommandPaletteContext.Provider value={{ open, setOpen, toggle }}>
      {children}
      <CommandPalette open={open} onOpenChange={setOpen} />
    </CommandPaletteContext.Provider>
  );
}
