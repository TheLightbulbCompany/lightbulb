"use client";

import {
  LayoutDashboard,
  Bot,
  Boxes,
  MessageSquare,
  Radio,
  Clock,
  BarChart3,
  Plug,
  Blocks,
} from "lucide-react";
import { useOrganization } from "@clerk/nextjs";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

interface ControlSidebarProps {
  activePanel?: string;
  onPanelChange?: (panel: string) => void;
}

const NAV_ITEMS = [
  { key: "overview", label: "Overview", icon: LayoutDashboard },
  { key: "agents", label: "Agents", icon: Bot },
  { key: "skills", label: "Capabilities", icon: Boxes },
  { key: "sessions", label: "Sessions", icon: MessageSquare },
  { key: "channels", label: "Channels", icon: Radio },
  { key: "cron", label: "Cron Jobs", icon: Clock },
  { key: "usage", label: "Usage", icon: BarChart3 },
  { key: "llm", label: "Model & Credits", icon: Plug },
  { key: "integrations", label: "Integrations", icon: Blocks },
];

// Panels hidden from non-admin org members
const ADMIN_ONLY_PANELS = new Set(["usage"]);

export function ControlSidebar({ activePanel, onPanelChange }: ControlSidebarProps) {
  const { membership } = useOrganization();
  const isOrgAdmin = !membership || membership.role === "org:admin";

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
