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
import { useOrganization } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

interface ControlSidebarProps {
  activePanel?: string;
  onPanelChange?: (panel: string) => void;
}

// Exported so the unified-nav System section (components/chat/SystemNav) can
// reuse the same global control-panel list, labels, icons, and admin gating
// rather than hand-duplicating them. Plain keys route through
// ControlPanelRouter identically whether emitted here or from SystemNav.
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

// Personal owners (no org membership) are treated as admins; org members are
// admins only with the org:admin role. Shared with SystemNav so both nav
// surfaces gate ADMIN_ONLY_PANELS identically.
export function useIsControlAdmin(): boolean {
  const { membership } = useOrganization();
  return !membership || membership.role === "org:admin";
}

export function ControlSidebar({ activePanel, onPanelChange }: ControlSidebarProps) {
  const isOrgAdmin = useIsControlAdmin();

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
                "w-full justify-start gap-2 font-normal transition-all h-auto py-1.5",
                activePanel === key
                  ? "bg-white text-[#1a1a1a] shadow-sm"
                  : "text-[#8a8578] hover:text-[#1a1a1a] hover:bg-white/60",
              )}
              onClick={() => onPanelChange?.(key)}
            >
              <Icon className="h-4 w-4 flex-shrink-0 opacity-70" />
              <span className="truncate">{label}</span>
            </Button>
          );
        })}
      </div>
    </ScrollArea>
  );
}
