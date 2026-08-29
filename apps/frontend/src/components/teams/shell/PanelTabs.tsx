"use client";

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

export interface PanelTabDefinition {
  value: string;
  label: string;
  count?: number;
}

/**
 * `underline` (default) — a hairline rail with a 2px --midnight edge under the
 * active tab. The panel-level idiom, and the one the dock's own tab strip
 * mirrors (see the `.dockview-theme-lightbulb` block in workspace.css), so a
 * panel's tabs and the dock's tabs read as one system.
 *
 * `pill` — a --cream-deeper track with the active segment lifted out in
 * --cream. For a small, closed set of view switches INSIDE a panel body,
 * where an underline would compete with the panel's own tab rail.
 */
export type PanelTabsVariant = "underline" | "pill";

export interface PanelTabsProps {
  tabs: PanelTabDefinition[];
  value: string;
  onValueChange: (value: string) => void;
  variant?: PanelTabsVariant;
  className?: string;
  /** Accessible name for the tablist, when the surrounding heading is not one. */
  ariaLabel?: string;
}

const LIST_STYLES: Record<PanelTabsVariant, string> = {
  underline: "h-auto justify-start gap-1 bg-transparent p-0 border-b border-[var(--edge)]",
  pill: "h-auto justify-start gap-0.5 rounded-md bg-[var(--cream-deeper)] p-0.5",
};

// Colour/background only, on the fast clock. No sliding indicator and no
// layout animation: switching tabs is core navigation and must land the frame
// it is clicked.
const TRIGGER_STYLES: Record<PanelTabsVariant, string> = {
  underline: cn(
    "rounded-none border-b-2 border-transparent bg-transparent px-3 py-2 text-ui",
    "transition-colors duration-(--dur-fast) ease-out",
    "data-[state=active]:border-[var(--midnight)] data-[state=active]:bg-transparent data-[state=active]:text-[var(--midnight)]",
    "data-[state=inactive]:text-[var(--midnight-muted)]",
  ),
  pill: cn(
    "rounded-sm border-0 bg-transparent px-3 py-1.5 text-ui",
    "transition-colors duration-(--dur-fast) ease-out",
    "data-[state=active]:bg-[var(--cream)] data-[state=active]:text-[var(--midnight)] data-[state=active]:shadow-(--shadow-hair)",
    "data-[state=inactive]:text-[var(--midnight-muted)] data-[state=inactive]:hover:text-[var(--midnight)]",
  ),
};

const COUNT_STYLES: Record<PanelTabsVariant, string> = {
  underline: "bg-[var(--edge)]",
  pill: "bg-[var(--cream-deep)]",
};

export function PanelTabs({
  tabs,
  value,
  onValueChange,
  variant = "underline",
  className,
  ariaLabel,
}: PanelTabsProps) {
  return (
    <Tabs value={value} onValueChange={onValueChange} className={className}>
      <TabsList aria-label={ariaLabel} className={LIST_STYLES[variant]}>
        {tabs.map((tab) => (
          <TabsTrigger key={tab.value} value={tab.value} className={TRIGGER_STYLES[variant]}>
            <span>{tab.label}</span>
            {typeof tab.count === "number" && tab.count > 0 && (
              <span
                className={cn(
                  "ml-2 rounded-full px-1.5 py-0.5 text-2xs font-semibold tabular-nums text-[var(--midnight)]",
                  COUNT_STYLES[variant],
                )}
              >
                {tab.count}
              </span>
            )}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}
