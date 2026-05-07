"use client";

import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";

export interface PanelTabDefinition {
  value: string;
  label: string;
  count?: number;
}

export interface PanelTabsProps {
  tabs: PanelTabDefinition[];
  value: string;
  onValueChange: (value: string) => void;
  className?: string;
}

export function PanelTabs({ tabs, value, onValueChange, className }: PanelTabsProps) {
  return (
    <Tabs value={value} onValueChange={onValueChange} className={className}>
      <TabsList className="h-auto justify-start gap-1 bg-transparent p-0 border-b border-[var(--edge)]">
        {tabs.map((tab) => (
          <TabsTrigger
            key={tab.value}
            value={tab.value}
            className={cn(
              "rounded-none border-b-2 border-transparent bg-transparent px-3 py-2 text-[13px]",
              "data-[state=active]:border-[var(--forest)] data-[state=active]:bg-transparent data-[state=active]:text-[var(--ink)]",
              "data-[state=inactive]:text-[var(--mute)]",
            )}
          >
            <span>{tab.label}</span>
            {typeof tab.count === "number" && tab.count > 0 && (
              <span className="ml-2 rounded-full px-1.5 py-0.5 text-[10px] font-semibold tabular-nums bg-[var(--edge)] text-[var(--ink)]">
                {tab.count}
              </span>
            )}
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}
