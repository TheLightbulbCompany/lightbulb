// Ported from upstream Paperclip's lib/work-mode-meta.ts
// (paperclip/ui/src/lib/work-mode-meta.ts) (MIT, (c) 2025 Paperclip AI).
// Labels, icons and order are upstream's. Colours are swapped for our tokens
// (upstream's sky -> info, amber -> warn; the tokens already flip per theme, so
// the dark: variants are gone). Left out: the composer-only `container` class
// and the composer helpers (isIssueWorkMode, nextWorkMode,
// titleForPendingWorkMode), since we have no composer mode chip. `skill_test`
// is renderable but never selectable.

import {
  ClipboardList,
  FlaskConical,
  Hammer,
  MessageCircleQuestion,
  type LucideIcon,
} from "lucide-react";
import type { IssueWorkMode } from "@/components/teams/shared/types";

export type WorkModeTone = "neutral" | "ask" | "planning";

export interface WorkModeMeta {
  value: IssueWorkMode | "skill_test";
  label: string;
  icon: LucideIcon;
  tone: WorkModeTone;
  classes: {
    chip: string;
    menuItem: string;
    badge: string;
  };
}

const STANDARD_CLASSES = {
  chip: "border-border bg-muted/40 text-muted-foreground hover:bg-accent hover:text-foreground",
  menuItem: "text-foreground",
  badge: "",
};

const ASK_CLASSES = {
  chip: "border-info/60 bg-info/15 text-info hover:bg-info/25",
  menuItem: "text-info",
  badge: "border-info/40 bg-info/10 text-info",
};

const PLANNING_CLASSES = {
  chip: "border-warn/60 bg-warn/15 text-warn hover:bg-warn/25",
  menuItem: "text-warn",
  badge: "border-warn/40 bg-warn/10 text-warn",
};

/** The modes a person can pick, in upstream's order. */
export function workModeMetaList(): (WorkModeMeta & { value: IssueWorkMode })[] {
  return [
    {
      value: "standard",
      label: "Auto mode",
      icon: Hammer,
      tone: "neutral",
      classes: STANDARD_CLASSES,
    },
    {
      value: "planning",
      label: "Plan mode",
      icon: ClipboardList,
      tone: "planning",
      classes: PLANNING_CLASSES,
    },
    {
      value: "ask",
      label: "Ask mode",
      icon: MessageCircleQuestion,
      tone: "ask",
      classes: ASK_CLASSES,
    },
  ];
}

// Upstream-internal mode (a pinned skill revision under test). Shown when an
// issue carries it; never offered in a picker.
const SKILL_TEST_META: WorkModeMeta = {
  value: "skill_test",
  label: "Skill test",
  icon: FlaskConical,
  tone: "neutral",
  classes: STANDARD_CLASSES,
};

export function workModeMetaFor(mode: string | null | undefined): WorkModeMeta {
  if (mode === "skill_test") return SKILL_TEST_META;
  const modes = workModeMetaList();
  return modes.find((meta) => meta.value === mode) ?? modes[0]!;
}
