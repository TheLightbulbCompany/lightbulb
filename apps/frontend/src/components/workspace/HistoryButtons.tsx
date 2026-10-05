"use client";

import { useEffect, useState } from "react";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { hasBlockingShortcutDialog } from "@/components/teams/shared/lib/keyboardShortcuts";

interface NavigationLike {
  entries?: () => { url: string | null }[];
  currentEntry?: { index: number } | null;
  addEventListener?: (type: "currententrychange", listener: () => void) => void;
  removeEventListener?: (type: "currententrychange", listener: () => void) => void;
}

const navigationApi = () => (window as Window & { navigation?: NavigationLike }).navigation;

/** Back/Forward stay inside the app: the entry beside this one must be this page. */
export function historyAvailability(): { back: boolean; forward: boolean } {
  const navigation = navigationApi();
  const index = navigation?.currentEntry?.index;
  if (!navigation?.entries || index === undefined) return { back: true, forward: true };
  const entries = navigation.entries();
  const inApp = (entry?: { url: string | null }) =>
    !!entry?.url && new URL(entry.url).pathname === window.location.pathname;
  return { back: inApp(entries[index - 1]), forward: inApp(entries[index + 1]) };
}

export function HistoryButtons(): React.ReactElement {
  const [available, setAvailable] = useState({ back: true, forward: true });
  useEffect(() => {
    const update = () => setAvailable(historyAvailability());
    update();
    const navigation = navigationApi();
    navigation?.addEventListener?.("currententrychange", update);
    return () => navigation?.removeEventListener?.("currententrychange", update);
  }, []);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.shiftKey) return;
      if (!(event.metaKey || event.ctrlKey)) return;
      if (event.code !== "BracketLeft" && event.code !== "BracketRight") return;
      if (hasBlockingShortcutDialog() || document.querySelector("[role='menu']")) return;
      // Swallowed even when unavailable, so the browser's own ⌘[ cannot leave the app.
      event.preventDefault();
      const { back, forward } = historyAvailability();
      if (event.code === "BracketLeft") { if (back) window.history.back(); }
      else if (forward) window.history.forward();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div className="workspace-rail__history">
      <button type="button" aria-label="Back" title="Back (⌘[)" disabled={!available.back} onClick={() => window.history.back()}>
        <ArrowLeft size={16} aria-hidden="true" />
      </button>
      <button type="button" aria-label="Forward" title="Forward (⌘])" disabled={!available.forward} onClick={() => window.history.forward()}>
        <ArrowRight size={16} aria-hidden="true" />
      </button>
    </div>
  );
}
