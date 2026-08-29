"use client";

import {
  LayoutDashboard,
  Bot,
  MessageSquare,
  Radio,
  BarChart3,
  Plug,
  Blocks,
  Wrench,
  Moon,
  FileCheck,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";

interface ControlSidebarProps {
  activePanel?: string;
  onPanelChange?: (panel: string) => void;
}

// The global control-panel list, labels, icons, and admin gating. Plain keys
// route through ControlPanelRouter. Exported alongside ADMIN_ONLY_PANELS /
// useIsWorkspaceAdmin for any other surface that needs the same panel set.
export const NAV_ITEMS = [
  { key: "overview", label: "Overview", icon: LayoutDashboard },
  { key: "agents", label: "Agents", icon: Bot },
  { key: "skills", label: "Skills", icon: Wrench },
  { key: "sessions", label: "Sessions", icon: MessageSquare },
  { key: "channels", label: "Channels", icon: Radio },
  { key: "usage", label: "Usage", icon: BarChart3 },
  { key: "llm", label: "Model & Credits", icon: Plug },
  { key: "integrations", label: "Integrations", icon: Blocks },
  { key: "dreams", label: "Dreams", icon: Moon },
  { key: "skill-proposals", label: "Skill Proposals", icon: FileCheck },
];

// Panels hidden from non-admin org members
export const ADMIN_ONLY_PANELS = new Set(["usage"]);

export function ControlSidebar({ activePanel, onPanelChange }: ControlSidebarProps) {
  const isOrgAdmin = useIsWorkspaceAdmin();

  return (
    <ScrollArea className="flex-1 px-3 py-2">
      <div className="space-y-1">
        {NAV_ITEMS.map(({ key, label, icon: Icon }) => {
          if (ADMIN_ONLY_PANELS.has(key) && !isOrgAdmin) return null;
          return (
            <Button
              key={key}
              variant="ghost"
              className={cn(
                "w-full justify-start gap-2 font-normal h-auto py-1.5",
                activePanel === key
                  ? "bg-cream text-[var(--midnight)] shadow-sm"
                  : "text-[var(--midnight-muted)] hover:text-[var(--midnight)] hover:bg-cream-deep",
              )}
              onClick={() => onPanelChange?.(key)}
            >
              <Icon size={16} className="shrink-0 opacity-70" />
              <span className="truncate">{label}</span>
            </Button>
          );
        })}
      </div>
    </ScrollArea>
  );
}
